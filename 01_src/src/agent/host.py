"""Production supervisor adapter. Deep Agents owns the single model/action graph.

The surrounding graph remains a load/save compatibility shell. Presets supply
capabilities and local methods; they do not supply a second task controller.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import logging
import os
from pathlib import Path
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .execution import ToolExecution
from .capabilities import CapabilityUnavailable
from .host_policy import allowed_presets, system_prompt
from .host_tools import HostTools
from .identity import ExecutionIdentity
from .model import ModelDriver
from .preset_catalog import build_preset_catalog
from .runtime import AdmissionStopped, BatchObservation, FinalCheck, RunLimits, RunResult
from .harness import HarnessRuntime, execution_lock
from .skill_provenance import runtime_provenance

logger = logging.getLogger(__name__)


async def general_agent_node(state: dict) -> dict:
    from src.core import tracing
    from src.core.request_context import ContextEpoch, _epoch
    from src.core.execution_ledger import ExecutionLedger, bind_execution_ledger, reset_execution_ledger
    from src.tools.interaction import bind_question_context, reset_question_context
    from src.graph.session_nodes import _release_candidate_pool
    identity = ExecutionIdentity(int(state.get("user_id") or 0), str(state.get("session_id") or ""),
        str(state.get("request_id") or ""), tracing.get_job_id())
    state = dict(state)
    state["job_id"] = identity.run_id
    ledger = ExecutionLedger(session_id=identity.session_id, request_id=identity.user_turn_id)
    ledger_token = bind_execution_ledger(ledger)
    epoch_token = _epoch.set(ContextEpoch(
        history_load_status=str(state.get("history_load_status") or "unknown"),
        history_load_reason=str(state.get("history_load_reason") or "")))
    question_token = bind_question_context({})
    # Scientific task scope is loaded only if its capability is actually used.
    output = None
    preserve_pool = False
    try:
        from src.core.capability_policy import bind_policy, policy_for_state
        policy = policy_for_state(state)
        state["capability_snapshot"] = policy.snapshot()
        with bind_policy(policy):
            output = await _run(state, identity, ledger)
        if output.get("execution_suspension"):
            from .execution_outcome import validate_execution_suspension
            validate_execution_suspension(output["execution_suspension"], identity)
            preserve_pool = True
        return output
    finally:
        from src.browse.service import release as release_browser
        try:
            await release_browser(identity.owner_user_id, identity.session_id, identity.run_id)
        except (OSError, TimeoutError):
            logger.warning("Browser cleanup did not confirm exit for job %s", identity.run_id)
        reset_question_context(question_token)
        _epoch.reset(epoch_token)
        reset_execution_ledger(ledger_token)
        if preserve_pool:
            from src.retrieval.candidate_pool import clear_current_candidate_pool
            clear_current_candidate_pool()
        else:
            _release_candidate_pool(state)


async def _run(state, identity, ledger):
    from src.config import settings
    from src.core.capability_policy import digest, policy_for_state
    from src.core import figures, task_events, tracing
    from src.core.cancel import is_cancelled
    from src.core.context_manager import assemble_supervisor_messages, build_human_message, build_loaded_context
    from src.core.llm_pool import get_llm
    from src.core.request_context import _epoch, resolve_main_model_budget
    from src.graph import agent_operations as ops
    from src.graph.model_driver import _model_timeout, request_deadline_from_settings
    from src.graph.request_plan import executor_authorizations, explicit_note_target, merge_task_instructions

    from src.config import PROJECT_ROOT
    from src.core.provider_protocol import uses_deepseek_protocol

    started = time.monotonic()
    transport = settings.model_transport()
    provider_scope = (transport.base_url, transport.model)
    state_root = PROJECT_ROOT / "state"
    resume = state.get("agent_resume") is True
    policy = policy_for_state(state)
    context_scope = policy.history_key
    if not resume and context_scope:
        from src.core.jobs import bind_native_context_scope
        if not await asyncio.to_thread(bind_native_context_scope, identity, context_scope):
            raise task_events.TaskConflict("recovery_identity_mismatch")
    from .installed_skills import (TurnSkills, list_installed, model_directory,
                                   task_release, validate_scenario_selection)

    state["script_budget_scope"] = identity.user_turn_id
    release = task_release(state, resume=resume)
    workbook_confirmation = ""
    installed_rows = list_installed(release) if release is not None else []
    skill_root = model_directory(release) if release is not None else None
    deadline = float(state.get("request_deadline") or request_deadline_from_settings())
    figures.begin_request()
    catalog = build_preset_catalog()
    from .context_tools import register_context_reader
    register_context_reader(catalog)
    from .code_tools import register_code_tools, CODE_TOOL_NAMES
    register_code_tools(catalog)
    from .fusion_tools import (CAPABILITY_UNAVAILABLE, FUSION_TOOL_NAMES, REPLACED_TOOLS, SKILL_IDS,
                               external_fusion_required, fusion_capability_notice, fusion_capability_state,
                               fusion_delivery_scope, fusion_retires_legacy_tools, installed_fusion,
                               register_fusion_tools, verified_report_route)
    from src.graph.task_contract import fusion_delivery_for_turn
    from .nature_skill_tools import NATURE_TOOL_NAMES, installed_nature, register_nature_tools
    external_release = installed_fusion(release, installed_rows)
    external_nature = installed_nature(release, installed_rows)
    legacy_receipt_pins = frozenset(os.getenv("CITRUS_PRE_FUSION_INVENTORY_SHA256", "").split())
    legacy_resume = (resume and release is not None and not (release / "fusion").exists()
                     and state["skill_release_inventory_sha256"] in legacy_receipt_pins)
    external_required = external_fusion_required() and not legacy_resume
    previous_report_query = ""
    report_routes = {}

    def report_followup():
        if not external_release or state.get("attachment_refs") or tables:
            return False
        text = str(state.get("query") or "")
        key = (text, previous_report_query)
        if key not in report_routes:
            contract = fusion_delivery_for_turn(text)
            report_routes[key] = verified_report_route(identity, release, contract, text, previous_report_query)
        return report_routes[key]
    if external_release:
        register_fusion_tools(catalog)
    if external_nature:
        register_nature_tools(catalog)
    host = HostTools(identity, state, skill_release=release)
    host.assert_context()
    pool = ops._bind_request_candidate_pool(state) if policy.allows("local_rag") else None
    if not policy.allows("local_rag"):
        state.pop("history_evidence_block", None)
        state.pop("candidate_pool_notice", None)
    # A table attachment is a runtime fact; no scientific module is needed to
    # offer the catalogue or answer an unrelated question.
    try:
        from src.research.context import get_request_tables
        tables = get_request_tables()
    except ImportError:
        tables = []
    # One atomic capability decision per refresh: the new Fusion surface is only
    # enabled when release + Worker + an authorized Skill are all ready. The old
    # analyzers are withdrawn only in that same transition.
    fusion_capability = {"state": "capability_unavailable", "reason": ""}

    def refresh_allowed():
        # A ToolNode task may populate the verified candidate handoff in its
        # own context. Resolve readiness from the current authenticated scope.
        live_pool = pool
        if live_pool is None and policy.allows("local_rag"):
            from src.retrieval.candidate_handoff import scoped_candidate_pool
            try:
                live_pool = scoped_candidate_pool()
            except (LookupError, PermissionError):
                pass
        host.allowed_names = allowed_presets(state, has_tables=bool(tables), has_pool=live_pool is not None)
        host.allowed_names.add("read_context_evidence")
        from src.graph.retrieval_router import retrieval_suppressed
        if retrieval_suppressed(str(state.get("query") or "")):
            from src.graph.tool_availability import retrieval_tool_names
            host.allowed_names.difference_update(retrieval_tool_names())
        from src.code_execution.presence import worker_available
        from src.config import PROJECT_ROOT as runtime_root
        worker_ready = worker_available(runtime_root / "state" / "code_executions.sqlite3")
        capability, fusion_names, capability_reason = fusion_capability_state(
            release=release, installed_rows=installed_rows, worker_ready=worker_ready,
            allowed_skills=[name for name in SKILL_IDS if policy.allows("skill:" + name)],
            external_release=external_release, external_required=external_required,
            report_followup=report_followup())
        fusion_capability.update(state=capability, reason=capability_reason,
                                 notice=fusion_capability_notice(capability, capability_reason))
        if fusion_retires_legacy_tools(capability) or capability == CAPABILITY_UNAVAILABLE:
            host.allowed_names.difference_update(REPLACED_TOOLS)
        host.allowed_names.update(fusion_names)
        if os.getenv("CITRUS_CODE_EXECUTION") == "offline-development":
            host.allowed_names.update(CODE_TOOL_NAMES)
            if not worker_ready:
                host.allowed_names.difference_update({"code_execute", "code_environment_install"})
            elif external_nature and policy.allows("skill:nature-figure"):
                host.allowed_names.update(NATURE_TOOL_NAMES)
        if report_followup():
            # A report continuation cannot rerun the audit, invoke code, or
            # advertise another source of findings if exact evidence is absent.
            host.allowed_names.intersection_update({"resolve_external_report", "read_tool_result", "request_user_input"})
        current_query = str(state.get("query") or "")
        tracing.set_request_authorizations(executor_authorizations(current_query), note_target=explicit_note_target(current_query))
        return set(host.allowed_names)
    refresh_allowed()
    # Host readiness remains independent of method loading; new generic tools
    # registered by the application must not inherit a scientific allowlist.
    loaded = build_loaded_context(state, mode="general_agent", format_hint=state.get("format_hint"))
    human = build_human_message(loaded, retrieval_context=str(state.get("candidate_pool_notice") or "") if pool else "")
    try:
        selected_metadata = await asyncio.to_thread(
            validate_scenario_selection, state.get("selected_skills"), catalog=installed_rows)
    except (OSError, ValueError, KeyError):
        # The API validates its own selection contract. A package becoming
        # unavailable afterwards must not disable this request's other tools.
        selected_metadata = []
        human.content = str(human.content) + "\n所选历史技能不是当前场景发行物；可继续使用其他已授权能力，不得声称已加载该方法。"
    selected_metadata = [row for row in selected_metadata if policy.allows("skill:" + row["id"])]
    host.turn_skills = TurnSkills.from_selection(release, selected_metadata)
    pinned_skills = [row["id"] for row in selected_metadata]
    human.additional_kwargs.update(identity.message_metadata("user"), user_text=str(state.get("query") or ""),
        attachment_refs=state.get("attachment_refs") or [], selected_skills=selected_metadata)
    if any(ref.get("source") == "previous_turn" for ref in state.get("attachment_refs") or []):
        human.content = str(human.content) + "\n本轮只读输入来自上一轮附件，已校验文件哈希。"
    if state.get("attachment_context"):
        human.content = str(human.content) + "\n\n" + str(state["attachment_context"])
    images = state.get("image_attachments") or []
    if images:
        if ops._main_model_supports_image_input():
            human.content = [{"type": "text", "text": str(human.content)}] + [
                {"type": "image_url", "image_url": {"url": image["url"]}}
                for image in images if image.get("url")]
        else:
            human.content = str(human.content) + "\n当前主模型不支持图片输入；不可声称已看到原图。"
    from src.code_execution.execution_bridge import sandbox_ready as native_execute_ready
    from .runtime_capability_table import build as build_runtime_capabilities

    read_only_followup = report_followup()
    execute_ready = not read_only_followup and native_execute_ready()
    task_enabled = not read_only_followup and settings.AGENT_ENABLE_TASK_SUBAGENT
    allowed = set(host.allowed_names)
    args_by_name = {row["function"]["name"]: tuple(row["function"]["parameters"].get("properties", {}))
                    for row in catalog.schemas(allowed)}
    runtime_capabilities = build_runtime_capabilities(
        available_names=allowed | ({"execute"} if execute_ready else set()),
        authorized_skills=[name for name in SKILL_IDS if policy.allows("skill:" + name)],
        fusion_state=fusion_capability["state"], tool_arguments=args_by_name,
        sandbox_ready=execute_ready, read_only_report=read_only_followup, release=release)
    prompt = await asyncio.to_thread(system_prompt, allowed)
    prompt += ("\n原生文件工具 ls/glob/grep/read_file 可读取本轮获授权的文件、技能和只读资源。")
    if not read_only_followup:
        prompt += (" write_file/edit_file/delete 仅作用于 /workspace 或 /outputs。"
                   "编辑前先读取当前版本；/outputs 只是暂存，正式交付仍须宿主产物发布回执。")
    if fusion_capability.get("notice"):
        prompt += fusion_capability["notice"]
    prompt += "\n" + runtime_capabilities.prompt_fragment()
    if "local_hybrid_search" in host.allowed_names:
        try:
            from src.retrieval.corpus_registry import load_corpus_registry
            prompt += "\n已登记的本地资料类别（选择工具时使用真实 family id）：\n" + load_corpus_registry().describe_families()
        except (OSError, ValueError, ImportError):
            prompt += "\n本地资料类别目录暂不可用；不得猜测 family id。"
    # Keep the brief public introduction after the internal tool directories.
    intro_capabilities = [description for names, description in (
        ({"browse_web"}, "浏览公开网页"),
        ({"local_hybrid_search", "deepseek_search", "deepseek_fetch", "deepseek_fetch_paper"}, "查找资料"),
        ({"inspect_uploaded_table"}, "分析附件"),
        ({"code_execute"}, "运行离线代码"),
    ) if names & host.allowed_names]
    intro_example = "我是 CitrusScholar，一名通用智能助手。我可以回答日常和专业问题"
    if intro_capabilities:
        intro_example += "，也可以" + "、".join(intro_capabilities)
    prompt += ("\n如果用户只问身份或能力，用一两句自然的话回答，不加标题、列表、工具名称、"
               "资料库类别或追问。可以参考这样的长度和用词：" + intro_example + "。")
    messages, trace_start = assemble_supervisor_messages(prompt, state, human)
    messages = ops._sanitize_history_tool_protocol(messages)
    history = messages
    query = str(state.get("query") or "")
    # Only authenticated human text may ground a follow-up topic.
    earlier = [str(m.additional_kwargs.get("user_text") or m.content) for m in history
               if isinstance(m, HumanMessage)
               and m.additional_kwargs.get("context_origin") == "user"
               and m.additional_kwargs.get("context_trusted") is True
               and m.additional_kwargs.get("run_id") != identity.run_id]
    previous_report_query = earlier[-1] if earlier else ""

    if report_followup():
        read_only = "resolve_external_report" in refresh_allowed()
        prompt += ("\n请用 resolve_external_report 读取已接受的审查报告，按真实检查范围答复。"
                   if read_only else "\n现有审查报告无法读取。说明限制，不得推断位置或重做。")
    messages = messages[1:]
    trace_start -= 1
    if not transport.api_key:
        raise RuntimeError("Main model credential is not configured for this endpoint")
    budget = resolve_main_model_budget(transport, settings)
    model = get_llm(model=transport.model, api_key=transport.api_key,
        base_url=transport.base_url, provider_headers=transport.headers, temperature=settings.TEMPERATURE_MAIN,
        max_tokens=budget.max_output_tokens, timeout=_model_timeout())
    # Provisional answers are not streamed as accepted answers. The final
    # accepted bytes are published once, then passed to session_save unchanged.
    driver = ModelDriver(catalog, budget=budget)

    from src.retrieval.turn_dedup import RetrievalLedger
    retrieval_ledger = RetrievalLedger()
    latest = None
    # Selection remains pinned metadata. The main loop reads applicable method
    # pages as needed; no mandatory preload can block unrelated capabilities.
    update_version = 0
    from src.graph.task_contract import contract_for_turn
    requirements = contract_for_turn(state["query"], has_tables=bool(tables)).deliverables
    # Private, identity-bound checkpoint state. Raw final envelopes stay in the
    # graph; only the extracted, validated answer crosses the user boundary.
    final_review = {}
    from src.core.turn_outcome import (
        completion_digest, completion_errors, reviewed_turn_outcome,
    )
    from .recovery import capture_original_request_state
    from .deep_agent_runtime import EXECUTOR_ID, NATIVE_FILE_TOOLS, NATIVE_READ_TOOLS

    summary_enabled = policy.allows("summarization") and state.get("context_compression") is not False
    if not resume or not state.get("capability_runtime"):
        state["capability_runtime"] = {
            "schema": 1,
            "executor": EXECUTOR_ID,
            "model": provider_scope[1], "provider_protocol": "deepseek" if uses_deepseek_protocol(provider_scope[1], provider_scope[0]) else "openai",
            "search_provider": settings.RESOLVED_WEB_SEARCH_PROVIDER,
            "summary_enabled": summary_enabled,
            "native_execute_ready": execute_ready,
            "native_task_enabled": task_enabled,
            "runtime_capabilities": runtime_capabilities.as_dict(),
            "skills": [{key: row[key] for key in ("id", "version", "sha256")}
                       for row in installed_rows if policy.allows("skill:" + row["id"])],
            "skill_release_inventory_sha256": state["skill_release_inventory_sha256"],
            "source_commit": os.getenv("CITRUS_SOURCE_COMMIT", ""),
            "selected_skills": selected_metadata,
            "attachments": [{"file_id": ref["file_id"], "sha256": ref["sha256"]}
                            for ref in (state.get("attachment_refs") or [])
                            if isinstance(ref, dict) and ref.get("file_id") and ref.get("sha256")],
            "host_prompt_sha256": digest(prompt),
            "host_tool_schema_sha256": digest(catalog.schemas(host.allowed_names)),
            "native_read_tools": sorted(NATIVE_READ_TOOLS),
            "native_file_tools": sorted(NATIVE_FILE_TOOLS),
            "budget": {"max_output_tokens": budget.max_output_tokens, "model_window": budget.model_window,
                       "input_limit": budget.input_limit(), "output_limit_source": budget.output_limit_source,
                       "provider_id": transport.provider_id, "provider_model": transport.model,
                       "max_model_calls": settings.AGENT_MAX_MODEL_CALLS,
                       "max_model_tokens": settings.AGENT_MAX_MODEL_TOKENS,
                       "working_max_input_tokens": settings.CONTEXT_WORKING_MAX_INPUT_TOKENS,
                       "working_soft_tokens": settings.CONTEXT_WORKING_SOFT_TOKENS},
            "retrieval": {"rrf_k": settings.RRF_K, "candidate_window": settings.CANDIDATE_WINDOW,
                          "top_k_vector": settings.TOP_K_VECTOR, "query_cache": bool(settings.RAG_CACHE_ENABLED)},
            "skill_runtime": runtime_provenance(),
        }
    original_request_state = capture_original_request_state(state)

    asset_lists = ("dataset_refs", "source_refs", "script_refs", "run_refs")
    asset_fields = (*asset_lists, "plot_intent")

    def export_host():
        working = host.working_state()
        assets = {key: deepcopy(working[key]) for key in asset_lists}
        assets["plot_intent"] = deepcopy(working["plot_intent"])
        return {"identity": asdict(identity),
                "original_request_state": original_request_state, "trace_start": trace_start,
                "working_assets": assets,
                "figures": figures.export_recovery(identity),
                "script_budget_scope": state["script_budget_scope"],
                "query": state["query"], "previous_report_query": previous_report_query,
                "update_version": update_version, "final_review": deepcopy(final_review),
                "artifact_requirements": deepcopy(requirements),
                "records": host.records,
                "main_results": host.main_results, "web_results": host.web_results,
                "pending_question": host.pending_question, "workbook_confirmation": workbook_confirmation,
                "messages": list(host.messages.values()), "dispatched": host.dispatched}

    def restore_host(saved):
        nonlocal update_version, original_request_state, trace_start, requirements, previous_report_query
        nonlocal workbook_confirmation
        original = saved["original_request_state"]
        binding = {"user_id": identity.owner_user_id, "session_id": identity.session_id,
                   "request_id": identity.user_turn_id, "job_id": identity.run_id}
        if (saved.get("identity", asdict(identity)) != asdict(identity)
                or any(original.get(key) != value for key, value in binding.items())):
            raise task_events.TaskConflict("identity_conflict")
        figures.restore_recovery(identity, saved.get("figures"))
        previous_report_query = str(saved.get("previous_report_query") or "")
        assets = saved.get("working_assets")
        if assets is not None and (not isinstance(assets, dict)
                or not set(assets).issubset(asset_fields)
                or not {"plot_intent", *asset_lists}.issubset(assets)
                or not isinstance(assets["plot_intent"], dict)
                or any(not isinstance(assets[key], list)
                       or any(not isinstance(ref, dict) for ref in assets[key])
                       for key in asset_lists)):
            raise task_events.TaskConflict("recovery_working_assets_invalid")
        scope = (saved.get("script_budget_scope") or original.get("script_budget_scope")
                 or (original.get("context_working_state") or {}).get("script_budget_scope")
                 or identity.user_turn_id)
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            raise task_events.TaskConflict("script_budget_identity_conflict")
        original_request_state = original
        state["script_budget_scope"] = scope
        if assets is not None:
            # Restore only host-owned assets. Query/update watermarks and live
            # supplementary instructions retain their existing recovery path.
            for key in asset_lists:
                merged = deepcopy(assets[key])
                for ref in state.get(key) or []:
                    if ref not in merged:
                        merged.append(deepcopy(ref))
                state[key] = merged
            state["plot_intent"] = {**deepcopy(state.get("plot_intent") or {}),
                                    **deepcopy(assets["plot_intent"])}
        else:
            # Compatibility with earlier checkpoints: rebuild references from
            # authenticated published messages, without executing their tools.
            ops._capture_interaction_refs(state, saved.get("messages", []))
        trace_start = int(saved["trace_start"])
        state["query"] = saved["query"]
        update_version = int(saved.get("update_version", 0))
        requirements = deepcopy(saved["artifact_requirements"]) if "artifact_requirements" in saved else (
            contract_for_turn(state["query"], has_tables=bool(tables)).deliverables)
        final_review.clear()
        final_review.update(deepcopy(saved.get("final_review") or {}))
        if final_review and (final_review.get("request_version") != update_version
                or final_review.get("request_hash") != completion_digest(state["query"])):
            final_review.clear()
        host.records[:] = saved.get("records", [])
        host.main_results[:] = saved.get("main_results", [])
        host.web_results[:] = saved.get("web_results", [])
        host.pending_question = saved.get("pending_question")
        workbook_confirmation = saved.get("workbook_confirmation", "")
        host.messages = {m.tool_call_id: m for m in saved.get("messages", [])}
        host.dispatched = dict(saved.get("dispatched", {}))
        tracing.set_original_query(state["query"])
        refresh_allowed()

    async def checkpoint(value):
        nonlocal latest
        latest = value  # error projection; authoritative state is the encrypted graph saver

    async def event(value):
        payload = asdict(value)
        task_events.append_event(identity.run_id, "agent_loop", payload)

    async def admit(history, _):
        if is_cancelled():
            raise asyncio.CancelledError()
        host.assert_context()

    async def prepare(_):
        nonlocal update_version, requirements
        updates = task_events.updates_after(identity.run_id, update_version)
        if not updates:
            return ()
        state["query"] = merge_task_instructions(str(state.get("query") or ""), updates)
        update_version = max(row["version"] for row in updates)
        requirements = contract_for_turn(state["query"], has_tables=bool(tables)).deliverables
        final_review.clear()  # A previous candidate cannot accept new requirements.
        tracing.set_original_query(state["query"])
        refresh_allowed()
        return ("用户在本轮补充的要求（保留已完成事实，调整未完成工作）：\n" +
                "\n".join(row["instruction"] for row in updates),)

    async def rejected(response, reason):
        observations = []
        for call in response.tool_calls:
            if call["id"] in host.messages:
                observations.append(host.messages[call["id"]])
                continue
            observation = ToolMessage(content=reason, tool_call_id=call["id"], name=call["name"],
                status="error", additional_kwargs={"execution_dispatched": False,
                    "operation_uncertain": False},
                artifact={"_tool_outcome": {"status": "not_run", "reason_code": reason}})
            observations.append(await host.publish(call, ToolExecution(observation), identity))
        return BatchObservation(tuple(observations))

    async def not_run(call, reason):
        return await host.publish(call, ToolExecution(ToolMessage(
            content="Tool was not executed: " + reason, tool_call_id=call["id"], name=call["name"],
            status="error", additional_kwargs={"execution_dispatched": False},
            artifact={"_tool_outcome": {"status": "not_run", "reason_code": reason}})), identity)

    async def execute(call, _):
        if is_cancelled():
            raise asyncio.CancelledError()
        from contextlib import nullcontext
        try:
            tool = catalog.resolve(call["name"], refresh_allowed())
        except CapabilityUnavailable as exc:
            return await not_run(call, exc.reason), {}
        from .batch_journal import require_unclaimed_call
        from src.tools.argument_check import validate_tool_arguments
        await asyncio.to_thread(require_unclaimed_call, identity, call)
        error, normalized_args = validate_tool_arguments(tool, call["name"], call["args"])
        if not error and call["name"] == "request_user_input":
            from src.core.user_questions import validate_question_refs
            try:
                await asyncio.to_thread(validate_question_refs, normalized_args["options"],
                                        identity.owner_user_id, host.question_context())
            except (LookupError, ValueError):
                return await not_run(call, "invalid_tool_arguments"), {}
        if not error:
            prior = retrieval_ledger.claim(call["name"], normalized_args, call["id"])
            if prior:
                # A duplicate is a distinct provider call paired with an
                # explicit non-dispatch result, never copied source evidence.
                duplicate = {**call, "_reused_from_tool_call_id": prior}
                observation = ToolMessage(content="This retrieval was already requested in this turn. Use its existing observation.",
                    tool_call_id=call["id"], name=call["name"],
                    additional_kwargs={"execution_dispatched": False},
                    artifact={"_tool_outcome": {"status": "skipped", "reason_code": "duplicate_request"}})
                return await host.publish(duplicate, ToolExecution(observation), identity), {}
        scope = nullcontext()
        if call["name"] in {"research_workspace", "run_research_figure_script"}:
            from src.research.script_runtime import bind_task_scope, reset_task_scope
            from contextlib import contextmanager

            @contextmanager
            def script_scope():
                token = bind_task_scope(state["script_budget_scope"])
                try:
                    yield
                finally:
                    reset_task_scope(token)
            scope = script_scope()
        query = str(state.get("query") or "")
        with scope, fusion_delivery_scope(fusion_delivery_for_turn(query, has_tables=bool(tables)),
                                          query=query, previous_query=previous_report_query, workbook_confirmation=workbook_confirmation):
            execution = await host.invoke(tool, call, identity)
        message = await host.publish(call, execution, identity)
        if not error:
            record = message.additional_kwargs.get("host_publication") or {}
            retrieval_ledger.settle(call["name"], normalized_args, call["id"],
                                    status=record.get("status", ""), reason=record.get("reason", ""),
                                    uncertain=execution.uncertain)
        return message, {"uncertain": execution.uncertain, "operation_id": execution.operation_id,
                         "pending_question": {"question_id": host.pending_question["id"],
                              "version": host.pending_question["version"]} if execution.pending_question else None,
                         "execution_waits": list(execution.execution_waits)}

    async def check_final(answer, history, _, *, allow_delivery_gaps=False):
        notes = await prepare(identity)
        if notes:
            return FinalCheck(False, notes)
        from src.core.public_delivery import public_answer_text
        text = public_answer_text(answer.content)
        if not text.strip() or ops._DSML_PROTOCOL_RE.search(text):
            return FinalCheck(False, ("尚未形成有效答复。需要工具时使用原生工具调用；不要用工具协议文本冒充答案。",))
        from src.core.turn_outcome import claims_file_delivery, completion_from_answer, repairable_proposal_reason
        statement = completion_from_answer(text, host.records)
        if any(row.get('name') == 'browse_web' for row in host.records):
            from src.core.progress_bus import emit_progress
            emit_progress('browse_phase', {'phase': 'organizing'})
        public_answer = statement.answer
        if settings.RESEARCH_ROUTE_NOTICE_ENABLED:
            from .research_routes import dual_source_notice
            notice = dual_source_notice(state["query"], host.records)
            if notice and notice not in public_answer:
                public_answer += "\n\n" + notice
                statement = statement.model_copy(update={"answer": public_answer})
        delivery_gaps, artifact_contract = [], None
        if requirements:
            from src.core.task_result import effective_tool_records
            from src.core.turn_outcome import artifact_goal_evidence
            from src.research.tools import validate_artifact_links
            effective = effective_tool_records(host.records)
            links = list(dict.fromkeys(item["link"] for row in effective
                for item in row.get("artifact_validations", []) if item.get("valid") and item.get("link")))
            verified = await asyncio.to_thread(validate_artifact_links, links, session_id=identity.session_id,
                         owner_user_id=identity.owner_user_id, request_id=host._result_request_id()) if links else []
            verified_links = sorted(item["link"] for item in verified if item.get("valid"))
            goals, delivery_gaps = artifact_goal_evidence(requirements, effective, set(verified_links))
            artifact_contract = {"schema_version": 1, "request_version": update_version,
                                 "requirements": requirements, "verified_links": verified_links}
            delivery_links = list(dict.fromkeys(link for goal in goals for link in goal.artifact_links
                                                if link not in public_answer))
            if delivery_links:
                public_answer += "\n\n已核验的交付文件\n" + "\n".join(
                    f"- [下载文件 {index}]({link})" for index, link in enumerate(delivery_links, start=1))
            if (delivery_gaps and not verified_links and not allow_delivery_gaps
                    and claims_file_delivery(statement.answer)):
                return FinalCheck(False, tuple(gap["detail"] for gap in delivery_gaps))
            if delivery_gaps:
                public_answer += "\n\n" + "\n".join(gap["detail"] for gap in delivery_gaps)
            statement = statement.model_copy(update={"answer": public_answer,
                "status": "partial" if delivery_gaps else statement.status,
                "goals": [*statement.goals, *goals], "remaining": [gap["detail"] for gap in delivery_gaps]})
        if "[[FIGURE:" in public_answer:
            public_answer = figures.expand(public_answer)
            statement = statement.model_copy(update={"answer": public_answer})
        native_calls = [call for message in history if isinstance(message, AIMessage)
                        for call in message.tool_calls]
        positions = {call["id"]: index for index, call in enumerate(native_calls)}
        calls = {call["id"]: call for call in native_calls}
        # A rejected proposal is not an executed obligation. The agent may fix
        # parameters OR select a different appropriate tool. Bind that repair to
        # later successful goal evidence only after proving zero dispatch.
        from src.core.turn_outcome import ProposalRepair

        goal_refs = {cid for goal in statement.goals for cid in goal.evidence_call_ids}
        superseded = [row["tool_call_id"] for row in host.records
            if update_version > 0 and row.get("status") == "not_run"
            and (row.get("reason") or row.get("reason_code")) == "preparation_required"
            and row.get("tool_call_id") in host.messages
            and row["tool_call_id"] not in host.dispatched
            and not host.messages[row["tool_call_id"]].additional_kwargs.get("execution_dispatched")
            and not host.messages[row["tool_call_id"]].additional_kwargs.get("operation_uncertain")]
        repairs = list(statement.proposal_repairs)
        claimed_repairs = {claim.rejected_call_id for claim in repairs}
        if statement.status == "completed" and not statement.remaining:
            for row in host.records:
                cid = row.get("tool_call_id")
                observation = host.messages.get(cid)
                if (cid in superseded or cid in claimed_repairs or row.get("status") != "not_run"
                        or not repairable_proposal_reason(str(row.get("reason") or row.get("reason_code") or ""))
                        or (str(row.get("reason") or row.get("reason_code") or "").startswith("load_failed:")
                            and catalog.execution_mode(str(row.get("name") or "")) != "parallel")
                        or not host.proven_no_dispatch(cid)):
                    continue
                corrected = next((r.get("tool_call_id") for r in host.records
                    if r.get("status") == "complete" and r.get("tool_call_id") in goal_refs
                    and positions.get(cid, float("inf")) < positions.get(r.get("tool_call_id"), -1)
                    and _same_repair_target(calls.get(cid), calls.get(r.get("tool_call_id")),
                                            records=host.records)), None)
                if corrected:
                    repairs.append(ProposalRepair(rejected_call_id=cid, corrected_call_id=corrected,
                        reason="提案在执行前被拒绝；本轮随后使用成功操作满足了所声明的目标。"))
            statement = statement.model_copy(update={"proposal_repairs": repairs})
        verified_repairs = []
        for claim in statement.proposal_repairs:
            observation = host.messages.get(claim.rejected_call_id)
            rejected = next((row for row in host.records if row.get("tool_call_id") == claim.rejected_call_id), {})
            if (observation is None or observation.additional_kwargs.get("operation_uncertain")
                    or (str(rejected.get("reason") or rejected.get("reason_code") or "").startswith("load_failed:")
                        and catalog.execution_mode(str(rejected.get("name") or "")) != "parallel")):
                continue
            no_dispatch = host.proven_no_dispatch(claim.rejected_call_id)
            if (no_dispatch and positions.get(claim.rejected_call_id, float("inf")) <
                    positions.get(claim.corrected_call_id, -1)
                    and _same_repair_target(calls.get(claim.rejected_call_id), calls.get(claim.corrected_call_id),
                                            records=host.records)):
                verified_repairs.append(claim.rejected_call_id)
        # Exploratory read receipts describe individual attempts, not additional
        # user obligations. Once the agent cites successful evidence for its
        # goals, resolve eligible source-only partials from the actual journal.
        # Do not require it to regenerate the entire answer merely to enumerate
        # every optional 404; keep the raw attempts and their notices unchanged.
        from src.core.task_result import effective_tool_records, incidental_partial_candidate
        from src.core.turn_outcome import IncidentalClaim
        from src.tools.registry import is_readonly_call
        rows = effective_tool_records(host.records)
        referenced = {cid for goal in statement.goals for cid in goal.evidence_call_ids}
        replacement_ids = [row["tool_call_id"] for row in rows
            if row.get("tool_call_id") in referenced and row.get("status") == "complete"
            and (row.get("name") or row.get("tool_name")) not in {"load_skill", "read_skill"}
            and row["tool_call_id"] in calls
            and is_readonly_call(calls[row["tool_call_id"]]["name"],
                                 calls[row["tool_call_id"]].get("args", {}))]
        if statement.status == "completed" and not statement.remaining and replacement_ids:
            claims = list(statement.incidental_partials)
            claimed = {claim.tool_call_id for claim in claims}
            for row in rows:
                cid = row.get("tool_call_id")
                call = calls.get(cid)
                if (cid not in claimed and call and incidental_partial_candidate(row)
                        and is_readonly_call(call["name"], call.get("args", {}))):
                    claims.append(IncidentalClaim(tool_call_id=cid,
                        reason="辅助来源读取未完整返回；目标已引用成功的只读证据，原始执行限制保留。",
                        replacement_call_ids=replacement_ids))
            statement = statement.model_copy(update={"incidental_partials": claims})
        errors = list(completion_errors(statement, host.records,
            state.get("missing_requirements") or (), verified_repairs=verified_repairs,
            superseded_proposals=superseded))
        if ops._DSML_PROTOCOL_RE.search(public_answer) or not public_answer.strip():
            errors.append("invalid_public_answer")
        if ops._sanitize_answer_artifact_links(public_answer) != public_answer:
            errors.append("unverified_answer_artifact_links")
        # The receipt filter alone does not establish that a partial was a read.
        # Check the actual native call, never a model-declared operation mode.
        for claim in statement.incidental_partials:
            call = calls.get(claim.tool_call_id)
            if not call or not is_readonly_call(call["name"], call.get("args", {})):
                errors.append("incidental_operation_not_verified_readonly:" + claim.tool_call_id)
        if errors:
            task_events.append_event(identity.run_id, "agent_control", {
                "reason_code": "execution_blocker", "codes": errors})
            # An invalid link/protocol still needs correction. An execution gap
            # is delivered honestly as partial instead of forcing another search
            # merely to rewrite an internal completion declaration.
            if any(code in {"invalid_public_answer", "unverified_answer_artifact_links"} for code in errors):
                return FinalCheck(False, ("答复未通过执行/交付校验：" + "; ".join(errors) +
                    "。沿用现有结果修正；不能假称未完成的操作已完成。",))
            limits = "\n\n执行限制：部分操作或必需范围未完成，不能视为整项任务已完成。"
            public_answer += limits
            statement = statement.model_copy(update={"answer": public_answer, "status": "partial",
                "remaining": [*statement.remaining, *errors],
                "goals": [goal for goal in statement.goals if goal.evidence_call_ids],
                "proposal_repairs": [claim for claim in statement.proposal_repairs
                                     if claim.rejected_call_id in verified_repairs]})
        from src.core.turn_sources import answer_citations
        public_answer, ref_data = await asyncio.to_thread(answer_citations, public_answer,
            session_id=identity.session_id, request_id=host._result_request_id(),
            user={"user_id": identity.owner_user_id, "role": tracing.get_user_role()})
        statement = statement.model_copy(update={"answer": public_answer})
        if not task_events.fence_final_answer(identity, update_version):
            return FinalCheck(False, await prepare(identity))
        outcome = reviewed_turn_outcome(statement, host.records, identity=asdict(identity),
            request_version=update_version, request_hash=completion_digest(state["query"]),
            verified_repairs=verified_repairs, superseded_proposals=superseded,
            artifact_contract=artifact_contract, delivery_gaps=delivery_gaps)
        host.publish_resolutions(outcome)
        final_review.update(phase="accepted", answer=public_answer, outcome=outcome.as_dict(), references=ref_data,
            request_version=update_version, request_hash=completion_digest(state["query"]))
        return FinalCheck()

    from .batch_journal import BatchObservationNotStored, RecoveryRequiresReconciliation
    from .checkpoint_store import (CheckpointProtectionError, checkpoint_diagnostic,
                                   framework_checkpoint_path, framework_checkpoint_scope, open_framework_checkpoint)
    from .recovery import is_terminal_orphan, validate_legacy_native_head

    async def metered_model(request, handler, current_identity, host_tools):
        model_allowed = set(refresh_allowed())
        historical_readback = host if "read_tool_result" in model_allowed else None
        edit_history = policy.allows("summarization") and state.get("context_compression") is not False
        if not host.has_result_readback():
            model_allowed.discard("read_tool_result")
        # before_model's host_state watermark is already synchronously committed.
        task_events.acknowledge_updates(identity.run_id, update_version)
        from .installed_skills import project_rqa_history
        history = project_rqa_history(request.messages, current_identity, host.turn_skills)
        return await driver(request.override(messages=list(history)), handler, current_identity,
                            allowed=model_allowed, host_tools=host_tools,
                            historical_readback=historical_readback, edit_history=edit_history)

    def restore_tool_observation(message, call):
        if message.tool_call_id not in host.messages:
            host.restore_observation(message)
        if (message.additional_kwargs.get("execution_dispatched")
                and not message.additional_kwargs.get("operation_uncertain")):
            if call and call["name"] == message.name:
                host.validate_observation(message)
                record = message.additional_kwargs.get("host_publication") or {}
                retrieval_ledger.restore(message.name, call["args"], message.tool_call_id)
                retrieval_ledger.settle(message.name, call["args"], message.tool_call_id,
                                        status=record.get("status", ""), reason=record.get("reason", ""))

    def validate_resume():
        host.assert_context()
        for message in host.messages.values():
            host.validate_observation(message)

    # Construct the storage-backed boundary only when Harness actually wakes a
    # suspended execution. Ordinary requests do not need workspace/account stores.
    wait_boundary = None

    def execution_wait_boundary():
        nonlocal wait_boundary
        if wait_boundary is None:
            from src.code_execution.service import ExecutionService
            from .execution_waits import ExecutionWaitBoundary
            wait_boundary = ExecutionWaitBoundary(ExecutionService(identity))
        return wait_boundary

    async def resolve_execution_waits(current_identity, wait_ids):
        return await execution_wait_boundary().resolve(current_identity, wait_ids)

    async def acknowledge_execution_waits(current_identity, wait_ids, checkpoint_ref):
        await execution_wait_boundary().acknowledge(current_identity, wait_ids, checkpoint_ref)

    async def check_final_after_followup(answer, history, identity):
        return await check_final(answer, history, identity, allow_delivery_gaps=True)

    parked_execution = None

    def verify_parked_snapshot(snapshot, result):
        from .execution_outcome import validate_execution_suspension
        from .runtime import _pending_calls
        nonlocal parked_execution
        values = snapshot.values
        signals = list((values.get("operation_signals") or {}).values())
        wake = values.get("execution_wake") or []
        pending_waits = list(dict.fromkeys(wait for item in signals for wait in item.get("execution_waits", ())))
        waits = wake or pending_waits
        if ((values.get("host_state") or {}).get("identity") != asdict(identity)
                or values.get("active_run_id") != identity.run_id
                or waits != list(result.execution_waits) or (wake and pending_waits)
                or any(item.get("uncertain") for item in signals)
                or host.pending_question or result.unknown_operations or result.pending_tool_calls
                or _pending_calls(values.get("messages", ()))):
            raise ValueError("execution_suspension_checkpoint_invalid")
        reference = snapshot.config.get("configurable", {}).get("checkpoint_id")
        parked_execution = validate_execution_suspension({"identity": asdict(identity),
            "checkpoint_ref": reference, "wait_ids": waits}, identity)

    from src.core import user_questions
    from .recovery import QuestionWakeDeferred

    async def bind_user_input(value, interrupt_id, checkpoint_ref):
        if not host.pending_question or value.get("question_id") != host.pending_question["id"]:
            raise ValueError("pending_question_interrupt_mismatch")
        host.pending_question = await asyncio.to_thread(user_questions.save_question_context,
            identity.run_id, host.question_context(), question_id=value["question_id"],
            interrupt_id=interrupt_id, checkpoint_ref=checkpoint_ref)

    def accept_user_input(value):
        nonlocal workbook_confirmation
        if (not host.pending_question or (value.question_id, value.version) !=
                (host.pending_question["id"], host.pending_question["version"])):
            raise ValueError("user_input_resume_identity_mismatch")
        workbook_confirmation = value.text
        host.pending_question = None

    async def acknowledge_user_input(value, checkpoint_ref):
        import sqlite3
        try:
            await asyncio.to_thread(user_questions.acknowledge_question_resume, identity, value, checkpoint_ref)
        except (OSError, sqlite3.Error, task_events.TaskConflict) as exc:
            raise QuestionWakeDeferred("question_wake_ack_deferred") from exc

    from .native_execute_receipt import dispatch_once as guard_native_execute
    from .native_approval import wait_for_approval_decisions

    approval_tools = (("commit_document_index",) if settings.PERMISSION_NATIVE_SHARED_INDEX
                      and "commit_document_index" in allowed else ())

    async def wait_native_approval(who, keys):
        await wait_for_approval_decisions(who, keys, timeout=settings.PERMISSION_WAIT_SEC)

    runtime = HarnessRuntime(model, execute, admit, check_final,
        RunLimits(max_model_steps=settings.AGENT_MAX_MODEL_CALLS, deadline=deadline),
        schemas=catalog.schemas({row["name"] for row in catalog.describe()}),
        on_event=event, checkpoint=checkpoint, prepare=prepare,
        export_host=export_host, restore_host=restore_host, validate_resume=validate_resume,
        recover_output=True, resolve_execution_waits=resolve_execution_waits,
        acknowledge_execution_waits=acknowledge_execution_waits, validate_parked_snapshot=verify_parked_snapshot,
        record_rejected=rejected,
        check_final_after_followup=check_final_after_followup, tool_mode=catalog.execution_mode,
        not_run=not_run, check_terminal_orphan=lambda values: is_terminal_orphan(identity, values),
        restore_tool_observation=restore_tool_observation, model_call=metered_model,
        can_recover_output=lambda: task_events.budget_headroom(identity.run_id),
        guard_native_execute=guard_native_execute, native_approval_tools=approval_tools,
        wait_native_approval=wait_native_approval if approval_tools else None,
        approval_restart_checkpoint=state.get("agent_approval_restart_checkpoint", ""),
        user_input=state.get("agent_question_resume"), bind_user_input=bind_user_input,
        accept_user_input=accept_user_input, acknowledge_user_input=acknowledge_user_input,
        check_retired_question=lambda run_id: asyncio.to_thread(user_questions.retired_question, identity, run_id))
    checkpoint_options = {"context_scope": context_scope}
    checkpoint_config = framework_checkpoint_scope(identity, provider_scope, **checkpoint_options)
    try:
        with execution_lock(state_root / "agent_locks", (identity.owner_user_id, identity.session_id)):
            exists = framework_checkpoint_path(state_root, identity, provider_scope, **checkpoint_options).is_file()
            if resume and not exists:
                raise AdmissionStopped("executor_checkpoint_incompatible")
            if not resume and not exists:
                await validate_legacy_native_head(state_root, identity, provider_scope, **checkpoint_options)
            async with open_framework_checkpoint(state_root, identity, provider_scope, **checkpoint_options) as saver:
                result = await runtime.run(identity, messages, saver=saver, resume=resume,
                    checkpoint_config=checkpoint_config, agent_options={"skill_root": skill_root,
                        "system_prompt": prompt, "capability_policy": policy, "turn_skills": host.turn_skills,
                        "model_budget": budget, "summary_enabled": summary_enabled, "execute_ready": execute_ready,
                        "task_subagent": task_enabled, "workspace_read_only": read_only_followup},
                    pinned_skills=pinned_skills)
                if result.status == "suspended" and result.reason == "execution_pending":
                    if parked_execution is None:
                        raise ValueError("execution_suspension_checkpoint_invalid")
                    return {"job_id": identity.run_id, "execution_suspension": parked_execution}
    except QuestionWakeDeferred:
        raise
    except Exception as exc:
        # Never restart the loop or retry a possibly dispatched operation here.
        # Log frame locations, never exception text/locals that can contain
        # provider credentials, queries or tool payloads.
        from src.graph.diagnostics import graph_failure
        logger.warning("General agent stopped %s", graph_failure(exc, graph="native_agent",
            request_id=identity.user_turn_id, job_id=identity.run_id,
            checkpoint=runtime.diagnostic_checkpoint))
        reason = "execution_error:" + type(exc).__name__
        if isinstance(exc, AdmissionStopped):
            reason = exc.reason
        elif isinstance(exc, (BatchObservationNotStored, RecoveryRequiresReconciliation)):
            reason = ("recovery_requires_reconciliation"
                      if isinstance(exc, RecoveryRequiresReconciliation) or exc.effects_uncertain
                      else exc.reason)
        elif isinstance(exc, CheckpointProtectionError):
            diagnostic = checkpoint_diagnostic(exc)
            logger.warning("Agent checkpoint rejected reason=%s structure=%s schema_version=%s",
                           diagnostic["reason"], diagnostic["structure"], diagnostic["schema_version"])
            reason = "checkpoint_recovery_incomplete"
        elif isinstance(exc, task_events.TaskConflict):
            code = str(exc).split(" (", 1)[0]
            reason = code if code in {"model_budget_exhausted:deadline", "model_budget_exhausted:calls",
                "model_budget_exhausted:tokens", "context_budget_exhausted", "resource_busy",
                "durable_operation_context_required", "identity_conflict", "recovery_requires_reconciliation"} else "resource_or_execution_conflict"
        result = RunResult("incomplete", reason,
            latest.messages if latest else tuple(messages), latest.model_steps if latest else 0,
            pending_tool_calls=latest.pending_tool_calls if latest else ())
    trace_start = next((index for index, message in enumerate(result.messages)
        if message.additional_kwargs.get("context_origin") == "user"
        and message.additional_kwargs.get("run_id") == identity.run_id), len(result.messages))
    return await _finish(state, identity, ledger, host, result, trace_start, started,
                         final_review=final_review, requirements=requirements)


def _same_audit_target(original, replacement):
    import json
    from src.research.tools import validate_audit_request

    def normalized(args):
        checks = args.get("checks_json") or {}
        if isinstance(checks, str):
            checks = json.loads(checks)
        if not isinstance(checks, dict):
            raise ValueError("invalid audit checks")
        checks = dict(checks)
        if checks.get("requested_checks") and not (checks.get("audit_plan") or checks.get("custom_findings")):
            checks.pop("profile", None)
        parsed = validate_audit_request(**(args | {"checks_json": checks}))
        if not (parsed["file_ids"] or parsed["file_indexes"]):
            raise ValueError("unspecified audit source")
        parsed["checks"] = {key: value for key, value in parsed["checks"].items() if key != "regions"}
        return parsed, args.get("sheet_name") or "", args.get("annotations_json") or "[]"

    try:
        return normalized(original) == normalized(replacement)
    except (TypeError, ValueError):
        return False


def _same_repair_target(rejected, corrected, *, records=()):
    if not rejected or not corrected or rejected["name"] != corrected["name"]:
        return False
    name = rejected["name"]
    original, replacement = rejected.get("args") or {}, corrected.get("args") or {}
    if name == "audit_uploaded_data":
        return _same_audit_target(original, replacement)
    if name == "code_bind_input":
        # The file identity, not the proposed mount spelling, is the target.
        keys = ("workspace_id", "task_id", "file_id", "sha256")
        return all(original.get(key) and original[key] == replacement.get(key) for key in keys)
    if name == "code_write":
        # Relative and absolute spellings of one workspace file are the same
        # target; a pre-dispatch rejection is repaired by the later write.
        return all(original.get(key) and original[key] == replacement.get(key)
                   for key in ("workspace_id", "task_id"))
    if name == "run_research_figure_script":
        parent = original.get("repair_from_run_id")
        if not parent or replacement.get("repair_from_run_id") not in (None, "", parent):
            return False
        from src.core.task_result import _operation_identity
        from src.graph.export_contract import artifact_matches_requirement

        previous = next((row for row in records if row.get("name") == name
                         and row.get("status") in {"failed", "partial"}
                         and (row.get("op_receipt") or {}).get("run_id") == parent), None)
        completed = next((row for row in records if row.get("tool_call_id") == corrected.get("id")
                          and row.get("name") == name and row.get("status") == "complete"), None)
        previous_identity = _operation_identity(previous) if previous else None
        figure_requirement = {"kinds": ["figure"], "formats": ["svg", "png", "pdf"]}
        return bool(previous_identity and previous_identity[1].startswith("script_repair:")
                    and completed and _operation_identity(completed) == previous_identity
                    and any(item.get("request_matches") is True and item.get("link")
                            and artifact_matches_requirement(item, figure_requirement)
                            for item in completed.get("artifact_validations", ())))
    if name == "research_workspace":
        previous = original.get("script_ref") or {}
        current = replacement.get("script_ref") or {}
        return (original.get("operation") in {"save", "patch"}
                and replacement.get("operation") in {"save", "patch"}
                and bool(previous.get("id")) and previous["id"] == current.get("id")
                and previous.get("kind") == current.get("kind"))
    if name in {"workspace_write", "workspace_edit"}:
        return bool(original.get("path")) and original["path"] == replacement.get("path")
    from src.tools.registry import is_readonly_call
    return (is_readonly_call(name, original) and is_readonly_call(name, replacement)
            and bool(original) and all(key in replacement and value == replacement[key]
                                       for key, value in original.items()))


def _settled_trace(messages, host, result):
    """Keep actual call IDs and observed outcomes when stopping mid-batch."""
    from .plan_tools import NATIVE_PLAN_TOOL

    by_id = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    trace = []
    for message in messages:
        if isinstance(message, ToolMessage):
            continue  # paired immediately after its issuing assistant below
        if isinstance(message, AIMessage) and any(call["name"] == NATIVE_PLAN_TOOL for call in message.tool_calls):
            calls = [call for call in message.tool_calls if call["name"] != NATIVE_PLAN_TOOL]
            if not calls:
                continue
            # Private todo entry text belongs only to the live checkpoint.
            extra = {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}
            message = message.model_copy(update={"tool_calls": calls, "additional_kwargs": extra})
        trace.append(message)
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            cid = call.get("id")
            if not cid:
                continue
            observed = by_id.get(cid) or host.messages.get(cid)
            if observed is None:
                dispatched = cid in host.dispatched
                from src.tools.registry import is_readonly_call
                uncertain = dispatched and not is_readonly_call(call["name"], call.get("args", {}))
                status = "unknown" if uncertain else "cancelled" if dispatched else "not_run"
                observed = ToolMessage(
                    content=("操作已调度，但结果尚未确认；不得重放，须先对账。" if uncertain else
                             "本轮已停止，此调用没有可采用的完成结果。" if dispatched else "本轮已停止，此调用未执行。"),
                    tool_call_id=cid, name=call["name"], status="error",
                    artifact={"_tool_outcome": {"status": status, "reason_code": result.reason}},
                    additional_kwargs=host.identity.message_metadata("tool"))
                from uuid import uuid4
                from src.graph.agent_operations import _emit_completed_tool_step
                _emit_completed_tool_step(call, observed, host.records, logical_key="host:" + uuid4().hex)
            trace.append(observed)
    return trace


def _verified_turn_delivery(host):
    import sqlite3
    from src.core.task_events import TaskConflict
    from src.session.files import list_turn_files, get_account_file, _sha256_file

    failed = 0
    links, verified = [], []
    try:
        identity = host.identity
        request_id = host._result_request_id()
        rows = list_turn_files(identity.session_id, identity.owner_user_id, [request_id]).get(request_id, [])
    except (OSError, sqlite3.Error, TaskConflict):
        return links, {"status": "unavailable", "verified_file_ids": verified, "unavailable_count": None}
    for entry in rows:
        if entry.get("origin") != "generated":
            continue
        try:
            owned = get_account_file(entry["file_id"], identity.owner_user_id, touch=False)
            path = Path(owned["path"]) if owned else None
            valid = path is not None and path.stat().st_size > 0 and _sha256_file(path) == owned.get("sha256")
        except (OSError, sqlite3.Error):
            valid = False
        if not valid:
            failed += 1
            continue
        name = str(owned["filename"]).replace("[", "\\[").replace("]", "\\]")
        links.append(f"- [{name}]({owned['download_url']})")
        verified.append(entry["file_id"])
    status = (("partial" if verified else "unavailable") if failed else ("available" if verified else "none"))
    return links, {"status": status, "verified_file_ids": verified, "unavailable_count": failed}


def _verified_length_delivery(host, requirements):
    from src.core.turn_outcome import verified_delivery_after_length
    from src.research.tools import validate_artifact_links

    links = {item["link"] for row in host.records for item in row.get("artifact_validations", ())
             if item.get("link") and item.get("valid") and item.get("request_matches") is True}
    identity = host.identity
    results = validate_artifact_links(links, session_id=identity.session_id,
        owner_user_id=identity.owner_user_id, request_id=host._result_request_id()) if links else []
    verified = {item["link"] for item in results if item.get("valid") and item.get("request_matches") is True
                and item.get("reason_code") == "verified"}
    return verified_delivery_after_length(requirements, host.records, verified)


async def _finish(state, identity, ledger, host, result, trace_start, started, *, final_review=None, requirements=()):
    from langchain_core.messages import get_notification_metadata
    from src.core import figures, task_events
    from src.core.progress_bus import emit_answer_start, emit_encoded, emit_text
    from src.core.task_result import build_task_result
    from src.core.turn_outcome import TurnOutcome, completion_digest, reviewed_completion_valid
    from src.graph.agent_operations import _safe_persisted_trace, _sanitize_answer_artifact_links
    trace = _settled_trace([message for message in result.messages[trace_start:]
                            if get_notification_metadata(message) is None
                            and not message.additional_kwargs.get("clarification_reply")], host, result)
    # Candidates and raw JSON envelopes belong only to the protected graph.
    # Session persistence appends the exact accepted public answer once.
    trace = [m for m in trace if not (isinstance(m, AIMessage) and not m.tool_calls)]
    reviewed = (final_review or {}).get("outcome")
    if result.status == "complete":
        proof = (reviewed or {}).get("completion") or {}
        if (not reviewed_completion_valid(reviewed, host.records,
                answer=(final_review or {}).get("answer"))
                or _sanitize_answer_artifact_links((final_review or {}).get("answer", "")) !=
                   (final_review or {}).get("answer", "")
                or proof.get("identity") != asdict(identity)
                or proof.get("request_hash") != completion_digest(state["query"])
                or task_events.updates_after(identity.run_id, proof.get("request_version", 0))):
            reviewed = None
            result = RunResult("incomplete", "completion_proof_invalid", result.messages,
                               result.model_steps)
    uncertain = bool(result.unknown_operations or result.reason in {
        "operation_outcome_unknown", "recovery_requires_reconciliation"}
        or any(isinstance(message, ToolMessage) and (
            message.additional_kwargs.get("operation_uncertain") or
            ((message.artifact or {}).get("_tool_outcome") or {}).get("status") == "unknown")
            for message in trace))
    if result.status == "complete":
        answer = final_review["answer"]
        status = reviewed["status"]
    elif result.status == "waiting" and host.pending_question:
        answer = str(host.pending_question["prompt"])
        status = "awaiting_input"
    elif result.status == "cancelled":
        answer = "本轮已停止，已完成的操作与文件已保留。"
        if uncertain:
            answer += " 未确认的写入须先对账。"
        status = "cancelled"
    else:
        notices = {
            "batch_observation_not_stored": "执行结果与模型回执分开保存；批次回执未能提交，未重跑操作。",
            "recovery_requires_reconciliation": "批次状态尚不能安全恢复。",
            "model_length": "模型服务提前结束了输出，未完成的提议没有执行。",
            "model_stream_unconfirmed": "未收到可确认的模型流结束信号，未执行该次返回的工具提议。",
            "model_budget_exhausted:tokens": "本轮累计模型用量预算不足以继续请求。",
            "model_budget_exhausted:calls": "本轮模型调用次数已达上限。",
            "model_budget_exhausted:deadline": "本轮任务时间预算已用完。",
            "deadline": "本轮任务时间预算已用完。",
            "model_step_limit": "本轮模型调用次数已达上限。",
            "context_budget_exhausted": "当前模型上下文无法安全容纳下一次请求。",
            "context_budget_exhausted:tool_observations": "工具结果和已读取的证据超出本轮发送预算，请缩小读取范围后再试。",
            "checkpoint_recovery_incomplete": "无法安全恢复本轮执行状态，已登记的成果仍可查看。",
            "native_thread_requires_reconciliation": "此会话的上一轮尚未确认收尾，请先恢复原任务。",
            "model_budget_reserved:finalization": "余量已为最终答复保留，不再发起中间调用。",
        }
        answer = "本轮尚未完成。" + notices.get(result.reason, "执行在完成全部要求前停止。")
        answer += " 已取得的结果和原始证据已保留。"
        if uncertain:
            answer += " 部分已执行操作结果尚未确认，须先对账，不能直接重放。"
        elif result.pending_tool_calls and result.reason != "batch_observation_not_stored":
            answer += " 尚未派发的提议标记为未执行，无需为它们对账。"
        if host.records:
            from collections import Counter
            counts = Counter(str(record.get("status") or "unknown") for record in host.records)
            labels = {"complete": "完成", "partial": "部分完成", "not_run": "未执行",
                      "failed": "失败", "unknown": "结果未知", "no_results": "无结果"}
            answer += "\n\n实际操作：" + "、".join(f"{labels.get(key, key)} {value} 项" for key, value in counts.items()) + "。"
        answer += f"\n\n停止原因：`{result.reason}`。"
        status = "partial" if host.messages else "failed"
    budget_blocked = result.reason == "context_budget_exhausted:tool_observations"
    if budget_blocked:
        from src.core.tool_result_view import last_readback_range
        notice = {"stage": "model_admission", "status": "budget_blocked", "reason_code": result.reason,
                  "message": "工具证据超出本轮上下文预算。请只读取所需字段，或用较短的一页继续。"}
        page = last_readback_range(list(host.messages.values()))
        if page:
            notice["last_readback_page"] = page
            if page["offset_basis"] == "stored_text":
                answer += (f"\n\n可沿用最近一次回执的引用，从第 {page['retry']['start_char']} 个字符起"
                           f"读取最多 {page['retry']['max_chars']} 个字符。")
        emit_encoded("context_degraded", notice)
    length_gaps = ()
    if result.reason == "model_length":
        verified = await asyncio.to_thread(_verified_length_delivery, host, requirements)
        if verified is not None:
            figure_summary, length_gaps = verified
            answer = answer + "\n\n" + figure_summary if uncertain else figure_summary
    delivery = {"status": "not_checked", "verified_file_ids": [], "unavailable_count": 0}
    if result.status != "complete" and status in {"partial", "failed", "cancelled"}:
        links, delivery = await asyncio.to_thread(_verified_turn_delivery, host)
        if links and (result.reason != "model_length" or verified is None or length_gaps):
            answer += "\n\n已生成并核对文件完整性的产物（不表示整项任务已完成或已通过 QA）：\n" + "\n".join(links)
        if delivery["status"] in {"partial", "unavailable"}:
            answer += "\n\n部分或全部已登记文件当前无法核验；未提供未经核验的链接。"
        emit_encoded("artifact_delivery", delivery)
    ledger.close(status="unknown" if uncertain else
                 "cancelled" if status == "cancelled" else "failed", reason_code=result.reason)
    reason_code = "budget_blocked" if budget_blocked else result.reason
    outcome = TurnOutcome(**reviewed) if result.status == "complete" else TurnOutcome(status, reason_code, result.reason)
    task_result = build_task_result(host.records, length_gaps, requested_status=status, turn_outcome=outcome)
    # Even stopped paths use the same conservative task decision as the API.
    if task_result.status != outcome.status and outcome.status not in {"cancelled", "awaiting_input"}:
        outcome = TurnOutcome(task_result.status, reason_code, result.reason)
        task_result = build_task_result(host.records, length_gaps, requested_status=outcome.status, turn_outcome=outcome)
    ref_data = (final_review or {}).get("references") if result.status == "complete" else None
    if ref_data is None:
        from src.core.tracing import get_user_role
        from src.core.turn_sources import answer_citations
        answer, ref_data = await asyncio.to_thread(answer_citations, answer,
            session_id=identity.session_id, request_id=host._result_request_id(),
            user={"user_id": identity.owner_user_id, "role": get_user_role()})
    task_events.release_finalization(identity.run_id)
    emit_answer_start()
    emit_text(answer)
    elapsed = (time.monotonic() - started) * 1000
    capability_snapshot = state.get("capability_snapshot")
    capability_runtime = dict(state.get("capability_runtime") or {})
    local_generations = {generation for item in host.main_results if isinstance(item, dict)
                         if (generation := (item.get("read_entry") or {}).get("generation") or item.get("generation"))}
    capability_runtime["observed_index_generations"] = sorted(local_generations)
    return {"query": state["query"], "answer": answer, "gen_time_ms": elapsed,
        "references_data": ref_data,
        **({"capability_snapshot": capability_snapshot} if capability_snapshot is not None else {}),
        "capability_runtime": capability_runtime,
        "execution_suspension": {},
        "main_results": host.main_results, "web_results": host.web_results,
        "tools_called": list(dict.fromkeys(r["name"] for r in host.records)),
        "tool_status": list(host.records), "execution_ledger": ledger.as_dict(),
        "context_working_state": host.working_state(), "turn_trace": _safe_persisted_trace(trace),
        "turn_outcome": outcome.as_dict(), "awaiting_input": status == "awaiting_input",
        "pending_question": host.pending_question,
        "export_gate": {"status": "not_applicable", "reason": "model_owned_delivery"},
        "_trace": {"node": "supervisor", "elapsed_ms": elapsed, "stop_reason": result.reason,
            "model_steps": result.model_steps, "task_result": task_result.as_dict(),
            "artifact_delivery": delivery,
            "summary": f"{result.model_steps} model steps; {result.reason}"}}
