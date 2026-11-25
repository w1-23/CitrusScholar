"""Production Deep Agents adapter; its graph owns model/tool transitions.

Domain-free policy hooks preserve the existing metered ModelDriver, batch
scheduler, operation runner and publication boundary. No direct tool fallback.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import inspect
import os
from pathlib import Path
import time
from typing import Annotated, Any

from deepagents import DeepAgentState
from langchain.agents.middleware import AgentMiddleware, ModelResponse, hook_config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command, interrupt
from langgraph.errors import NodeCancelledError
from pydantic import Field
from typing_extensions import NotRequired

from .events import LoopCheckpoint, LoopEvent
from .model import NON_SUCCESS_TERMINALS
from .runtime import AdmissionStopped, RunResult, RunLimits
from .deep_agent_runtime import EXECUTOR_ID, MODEL_PROVIDER, NATIVE_READ_TOOLS, create_framework_agent


def _merge_signals(left, right):
    return {} if right is None else {**(left or {}), **right}


_current_tool_call = ContextVar("citrus_framework_tool_call")


class HarnessState(DeepAgentState):
    executor_id: NotRequired[str]
    operation_signals: NotRequired[Annotated[dict | None, _merge_signals]]
    working_context: NotRequired[dict]
    seed_proposal: NotRequired[list[dict]]
    active_run_id: NotRequired[str]
    model_steps: NotRequired[int]
    host_state: NotRequired[dict]
    delivery_followup_used: NotRequired[bool]
    batch_notes: NotRequired[list[str]]
    batch_status: NotRequired[str]
    unknown_operations: NotRequired[list[str]]
    execution_waits: NotRequired[list[str]]
    # A synchronously committed wake awaiting host acknowledgment. Keep it in
    # the checkpoint until all acknowledgments succeed, including after a crash.
    execution_wake: NotRequired[list[str]]
    stop_status: NotRequired[str]
    stop_reason: NotRequired[str]
    # Raw rejected responses remain in the encrypted execution checkpoint,
    # including provider-private state; they are never public tool evidence.
    rejected_responses: NotRequired[list[AIMessage]]


class _DriverModel(BaseChatModel):
    driver: Any = Field(exclude=True)
    identity: Any = Field(exclude=True)
    model_name: str = "citrus-host-main-loop"
    expected_tools: frozenset[str] = Field(default_factory=frozenset, exclude=True)

    def _get_ls_params(self, stop=None, **kwargs):
        return {**super()._get_ls_params(stop=stop, **kwargs),
                "ls_provider": MODEL_PROVIDER, "ls_model_name": self.model_name}

    @property
    def _llm_type(self):
        return "citrus-metered-provider"

    def bind_tools(self, tools, **kwargs):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        names = {schema["function"]["name"] for schema in schemas}
        added = names - self.expected_tools
        if not added <= NATIVE_READ_TOOLS:
            raise RuntimeError("unexpected_deepagents_tool_inventory")
        return self.bind(native_schemas=tuple(schema for schema in schemas
            if schema["function"]["name"] in added))

    def _generate(self, *args, **kwargs):
        raise RuntimeError("async_model_required")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            message = await self.driver(tuple(messages), self.identity, **kwargs)
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            raise NodeCancelledError("model") from exc
        if not isinstance(message, AIMessage):
            raise TypeError("model_must_return_ai_message")
        message = message.model_copy(update={"additional_kwargs": {
            **message.additional_kwargs, **self.identity.message_metadata("assistant")}})
        return ChatResult(generations=[ChatGeneration(message=message)])


@contextmanager
def execution_lock(root: Path, identity):
    """OS-released process lock: no stale lease permits overlapping execution."""
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(repr(identity).encode()).hexdigest()
    with (root / (digest + ".lock")).open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("agent_run_already_active") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("agent_run_already_active") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass
class HarnessRuntime:
    model: Any
    execute: Any
    admit: Any
    check_final: Any
    limits: RunLimits
    schemas: list[dict]
    on_event: Any = None
    checkpoint: Any = None
    prepare: Any = None
    export_host: Any = None
    restore_host: Any = None
    validate_resume: Any = None
    recover_output: bool = False
    # Optional sync/async emitter with emit_public_progress's keyword contract.
    # Kept last so existing positional adapters remain compatible.
    on_public_progress: Any = None
    # Trusted host callbacks only. Resolution reads actual queue facts; ack is
    # called only after a synchronous checkpoint commit, before another model.
    resolve_execution_waits: Any = None
    acknowledge_execution_waits: Any = None
    record_rejected: Any = None
    check_final_after_followup: Any = None
    tool_mode: Any = None
    not_run: Any = None
    check_terminal_orphan: Any = None
    seed_plan: Any = None
    seed_message: Any = None
    working_view: Any = None
    restore_tool_observation: Any = None
    authorized_question_parent: str = ""

    async def _publish_working_note(self, response, identity, model_step):
        try:
            from src.core.public_progress import emit_public_progress, public_progress_text
            note = public_progress_text(response.content, has_tool_calls=bool(response.tool_calls))
            if not note:
                return
            emitted = (self.on_public_progress or emit_public_progress)(
                note, phase="working", step_id=f"model_{model_step}",
                note_id=f"{identity.run_id}:model:{model_step}", model_step=model_step,
                tool_call_ids=[call["id"] for call in response.tool_calls], job_id=identity.run_id)
            if inspect.isawaitable(emitted):
                await emitted
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("public_progress_failed stage=tool_round kind=%s", type(exc).__name__)

    async def run(self, identity, messages, *, saver, resume=False, checkpoint_config=None,
                  agent_options=None, pinned_skills=()):
        adapter = self
        latest = {"messages": list(messages), "model_steps": 0}
        read_permits = asyncio.Semaphore(4)
        exclusive = asyncio.Lock()
        update_lock = asyncio.Lock()
        prepared = {}
        operation_blocked = False

        def related_round(state):
            return next(m for m in reversed(state["messages"]) if isinstance(m, AIMessage))

        def control(text):
            return SystemMessage(content=text, additional_kwargs=identity.message_metadata("internal_control"))

        async def observed(state, phase="ready"):
            nonlocal latest
            latest = dict(state)
            if adapter.checkpoint:
                from .runtime import _pending_calls
                await adapter.checkpoint(LoopCheckpoint(identity, tuple(state["messages"]),
                    state.get("model_steps", 0), phase, _pending_calls(state["messages"])))

        async def event(kind, state, ids=(), *, output_limit_exhausted=None):
            if adapter.on_event:
                await adapter.on_event(LoopEvent(kind, identity, state.get("model_steps", 0), tuple(ids),
                    state.get("stop_status", ""), state.get("stop_reason", ""), output_limit_exhausted))

        def host_snapshot():
            return adapter.export_host() if adapter.export_host else {}

        async def admit(history):
            admitted = adapter.admit(history, identity)
            if inspect.isawaitable(admitted):
                await admitted

        def delivery_feedback(check):
            return "还缺：\n" + "\n".join(check.observations) + "\n请补齐产物，或明确说明无法完成。"

        class Policy(AgentMiddleware):
            state_schema = HarnessState

            @hook_config(can_jump_to=["end"])
            async def abefore_model(self, state, runtime):
                await observed(state)
                signals = list((state.get("operation_signals") or {}).values())
                unknown = [item["operation_id"] for item in signals if item.get("uncertain")]
                if unknown:
                    return {"stop_status": "incomplete", "stop_reason": "operation_outcome_unknown",
                            "unknown_operations": unknown, "host_state": host_snapshot(), "jump_to": "end"}
                followup_used = state.get("delivery_followup_used", False)
                notes = [note for item in signals for note in item.get("notes", ())]
                if state.get("model_steps", 0) >= adapter.limits.max_model_steps:
                    return {"stop_status": "incomplete", "stop_reason": "model_step_limit", "jump_to": "end",
                            "execution_wake": [], "execution_waits": [], "operation_signals": None, "batch_status": ""}
                if time.monotonic() >= adapter.limits.deadline:
                    raise AdmissionStopped("deadline")
                if adapter.prepare:
                    notes.extend(await adapter.prepare(identity))
                seed = []
                if adapter.seed_plan:
                    earlier_seeds = {call["name"] for message in state["messages"]
                        if isinstance(message, AIMessage) and message.additional_kwargs.get("citrus_retrieval_seed")
                        and message.additional_kwargs.get("run_id") == identity.run_id for call in message.tool_calls}
                    plan, guidance = await adapter.seed_plan(tuple(state["messages"]))
                    seed = [call for call in plan if call["name"] not in earlier_seeds]
                    if guidance and not any(isinstance(message, SystemMessage)
                            and message.content == guidance and message.additional_kwargs.get("run_id") == identity.run_id
                            for message in state["messages"]):
                        notes.append(guidance)
                additions = [control(note) for note in notes]
                await admit(tuple([*state["messages"], *additions]))
                update = {"messages": additions, "operation_signals": None,
                          "execution_waits": [], "execution_wake": [], "batch_status": "",
                          "delivery_followup_used": followup_used,
                          "seed_proposal": seed, "model_steps": state.get("model_steps", 0)+(not seed),
                          "host_state": host_snapshot()}
                # Before-model state is synchronously checkpointed before the call.
                await observed({**state, **update, "messages": [*state["messages"], *additions]}, "model")
                if not seed:
                    await event("model_started", latest)
                return update

            async def awrap_model_call(self, request, handler):
                proposal = request.state.get("seed_proposal")
                if proposal:
                    metadata = {**identity.message_metadata("assistant"), "citrus_retrieval_seed": True}
                    response = (adapter.seed_message(proposal, metadata) if adapter.seed_message
                                else AIMessage(content="", tool_calls=proposal, additional_kwargs=metadata))
                    return ModelResponse(result=[response])
                if adapter.working_view:
                    view = adapter.working_view(request.messages, request.state.get("working_context"))
                    if view is not None:
                        request = request.override(messages=view)
                return await handler(request)

            @hook_config(can_jump_to=["model", "end"])
            async def aafter_model(self, state, runtime):
                await observed(state, "tools")
                response = state["messages"][-1]
                if not isinstance(response, AIMessage):
                    raise TypeError("model_must_return_ai_message")
                reason = response.response_metadata.get("finish_reason")
                reason = "length" if reason in {"max_tokens", "max_output_tokens"} else reason
                invalid = "invalid_tool_call" if response.invalid_tool_calls else ""
                if reason == "length" or reason in NON_SUCCESS_TERMINALS:
                    invalid = "model_" + reason
                ids = [call.get("id") for call in response.tool_calls]
                previous = {call.get("id") for message in state["messages"][:-1]
                    if isinstance(message, AIMessage)
                    and message.additional_kwargs.get("context_turn_id") == identity.user_turn_id
                    for call in message.tool_calls}
                if ids and (any(not isinstance(cid, str) or not cid.strip() or cid != cid.strip()
                                or any(ord(char) < 32 for char in cid) for cid in ids)
                            or len(set(ids)) != len(ids) or previous.intersection(ids)):
                    invalid = "invalid_or_reused_tool_call_id"
                await event("retrieval_seed" if response.additional_kwargs.get("citrus_retrieval_seed")
                            else "model_finished", state, ids, output_limit_exhausted=(
                    response.response_metadata.get("citrus_output_limit_exhausted")
                    if invalid == "model_length" else None))
                if response.response_metadata.get("citrus_stream_terminal_unconfirmed"):
                    await event("model_output_rejected", {**state, "stop_reason": "model_stream_unconfirmed"}, ids)
                    return {"messages": [RemoveMessage(id=response.id)],
                            "rejected_responses": [*state.get("rejected_responses", []), response],
                            "stop_status": "incomplete", "stop_reason": "model_stream_unconfirmed", "jump_to": "end"}
                if (adapter.recover_output and (invalid == "invalid_tool_call" or (
                        invalid == "model_length" and not response.response_metadata.get("citrus_output_limit_exhausted")))):
                    # No tool in this response has been dispatched. Preserve raw
                    # arguments privately, and pair every valid native call with
                    # an explicit not_run result before asking the SAME model.
                    cleaned = response.model_copy(deep=True)
                    cleaned.invalid_tool_calls = []
                    cleaned.additional_kwargs.pop("tool_calls", None)
                    feedback_reason = "model_length" if invalid == "model_length" else "invalid_tool_arguments"
                    feedback = ("模型输出达到单次输出上限" if invalid == "model_length" else
                                "工具参数不是完整合法的JSON，无法解析")
                    additions = [cleaned]
                    if adapter.record_rejected is not None and cleaned.tool_calls:
                        batch = await adapter.record_rejected(cleaned, feedback_reason)
                        if (batch.unknown_operations or batch.pending_question or batch.execution_waits
                                or len(batch.messages) != len(cleaned.tool_calls)):
                            raise ValueError("invalid_rejected_batch_observation")
                        for call, observation in zip(cleaned.tool_calls, batch.messages):
                            artifact = observation.artifact or {}
                            outcome = artifact.get("_tool_outcome", {})
                            if (observation.status != "error" or observation.tool_call_id != call["id"]
                                    or observation.name != call["name"]
                                    or observation.additional_kwargs.get("execution_dispatched") is not False
                                    or observation.additional_kwargs.get("operation_uncertain")
                                    or outcome.get("status") != "not_run" or outcome.get("reason_code") != feedback_reason):
                                raise ValueError("invalid_rejected_tool_observation")
                            additions.append(observation.model_copy(update={"additional_kwargs": {
                                **observation.additional_kwargs, **identity.message_metadata("tool")}}))
                    else:
                        additions.extend(ToolMessage(content=f"调用未执行：{feedback}。请按schema重新生成完整参数。",
                            tool_call_id=call["id"], name=call["name"], status="error",
                            additional_kwargs={**identity.message_metadata("tool"), "execution_dispatched": False},
                            artifact={"_tool_outcome": {"status": "not_run", "reason_code": feedback_reason}})
                            for call in cleaned.tool_calls)
                    additions.append(control(f"上一条提议被拒绝：{feedback}，所有提议调用均未执行。"
                        "请利用已保存的结果继续本轮任务：缩短说明，按需拆分长代码/文件操作，"
                        "重新生成完整参数或精简的最终答复；不要把截断文本当成已完成结果。"))
                    await event("model_output_rejected", {**state, "stop_reason": invalid}, ids)
                    return {"messages": additions, "rejected_responses": [*state.get("rejected_responses", []), response],
                            "host_state": host_snapshot(), "jump_to": "model"}
                if invalid:
                    await event("model_output_rejected", {**state, "stop_reason": invalid}, ids)
                    return {"messages": [RemoveMessage(id=response.id)],
                            "rejected_responses": [*state.get("rejected_responses", []), response],
                            "stop_status": "incomplete", "stop_reason": invalid, "jump_to": "end"}
                if not response.tool_calls:
                    followup_used = state.get("delivery_followup_used", False)
                    check_final = (adapter.check_final_after_followup if followup_used else None) or adapter.check_final
                    check = await check_final(response, tuple(state["messages"]), identity)
                    if not check.accepted:
                        if followup_used:
                            return {"stop_status": "incomplete", "stop_reason": "final_validation_failed",
                                    "host_state": host_snapshot(), "jump_to": "end"}
                        return {"messages": [control(delivery_feedback(check))], "delivery_followup_used": True,
                                "host_state": host_snapshot(), "jump_to": "model"}
                    return {"stop_status": "complete", "stop_reason": "answered", "host_state": host_snapshot()}
                # Only a complete, valid native-tool round is public progress.
                # Final candidates/review envelopes and rejected output stay private.
                if not response.additional_kwargs.get("citrus_retrieval_seed"):
                    await adapter._publish_working_note(response, identity, state.get("model_steps", 0))
                return {"seed_proposal": []}

            async def awrap_tool_call(self, request, handler):
                nonlocal operation_blocked
                from .batch_journal import persistent_message
                from .runtime import validate_execution_waits

                call = request.tool_call
                name, cid = call["name"], call["id"]
                response = related_round(request.state)
                modes = adapter.tool_mode or (lambda key: "interaction" if key == "request_user_input" else "parallel")
                interaction = next((item["id"] for item in response.tool_calls if modes(item["name"]) == "interaction"), None)
                # ToolNode schedules each call. A business lock only protects
                # nonparallel capabilities from overlapping sibling operations.
                permit = exclusive if any(modes(item["name"]) != "parallel" and
                    item["name"] not in NATIVE_READ_TOOLS for item in response.tool_calls) else read_permits
                async with permit:
                    await admit(tuple(request.state["messages"]))
                    if time.monotonic() >= adapter.limits.deadline:
                        raise AdmissionStopped("deadline")
                    async with update_lock:
                        if response.id not in prepared:
                            prepared[response.id] = await adapter.prepare(identity) if adapter.prepare else ()
                    notes = prepared[response.id]
                    reason = ("interaction_barrier" if interaction and cid != interaction else
                              "prior_operation_unknown" if operation_blocked else
                              "preparation_required" if notes else "")
                    if reason:
                        if adapter.not_run and name in model.expected_tools:
                            message = await adapter.not_run(call, reason)
                        else:
                            message = ToolMessage(content="Tool was not executed: " + reason,
                                name=name, tool_call_id=cid, status="error",
                                additional_kwargs={"execution_dispatched": False},
                                artifact={"_tool_outcome": {"status": "not_run", "reason_code": reason}})
                        signal = {"notes": list(notes) if cid == response.tool_calls[0]["id"] else []}
                    else:
                        await observed(request.state, "tools")
                        await event("batch_started", request.state, (cid,))
                        tool_result = {}
                        token = _current_tool_call.set((call, tool_result))
                        try:
                            message = await handler(request)
                        finally:
                            _current_tool_call.reset(token)
                        signal = tool_result
                        if signal.get("uncertain"):
                            operation_blocked = True
                        if signal.get("pending_question") and name != "request_user_input":
                            raise ValueError("non_interaction_tool_cannot_suspend_turn")
                        await event("batch_finished", request.state, (cid,))
                    if (not isinstance(message, ToolMessage) or message.tool_call_id != cid
                            or message.name not in (None, name)):
                        raise ValueError("operation_observation_pairing_mismatch")
                    validate_execution_waits(tuple(signal.get("execution_waits", ())))
                    message = persistent_message(message.model_copy(update={"name": name,
                        "additional_kwargs": {**message.additional_kwargs,
                                              **identity.message_metadata("tool")}}))
                    if name == "draw_svg" and adapter.export_host:
                        from src.core.figures import export_recovery
                        signal["figure_recovery"] = export_recovery(identity)
                    if not signal:
                        return message
                    return Command(update={"messages": [message], "operation_signals": {cid: signal}})

        async def invoke_tool(**kwargs):
            call, signal = _current_tool_call.get()
            # Authorization, real tool validation, operation claim and result
            # storage are business boundaries around this single ToolNode call.
            message, details = await adapter.execute(call, identity)
            signal.update(details)
            return message

        class Suspension(AgentMiddleware):
            state_schema = HarnessState

            async def abefore_model(self, state, runtime):
                signals = list((state.get("operation_signals") or {}).values())
                if any(item.get("uncertain") for item in signals):
                    return None
                asking = any(item.get("pending_question") for item in signals)
                waits = tuple(dict.fromkeys(wait for item in signals for wait in item.get("execution_waits", ())))
                if not asking and not waits:
                    return None
                from .runtime import validate_execution_waits, _pending_calls
                validate_execution_waits(waits)
                if _pending_calls(state["messages"]) or (asking and waits):
                    raise ValueError("execution_suspension_requires_paired_messages")
                wake = interrupt({"kind": "user_input" if asking else "execution_wait", "wait_ids": list(waits)})
                if asking or not isinstance(wake, dict) or set(wake) != {"completion_notes"}:
                    raise ValueError("invalid_execution_resume")
                notes = tuple(wake["completion_notes"])
                if not notes or any(type(note) is not str for note in notes) or sum(map(len, notes)) > 65536:
                    raise ValueError("invalid_execution_completion_observations")
                return {"messages": [control(note) for note in notes], "operation_signals": None,
                        "execution_wake": list(waits), "host_state": host_snapshot()}

        tools = [StructuredTool(name=s["function"]["name"], description=s["function"]["description"],
                                args_schema=s["function"]["parameters"], coroutine=invoke_tool) for s in self.schemas]
        model = _DriverModel(driver=self.model, identity=identity,
                             expected_tools=frozenset(tool.name for tool in tools))
        graph = create_framework_agent(model, tools, [Suspension(), Policy()], saver, HarnessState,
                                       **(agent_options or {}))
        config = {"configurable": checkpoint_config or {"thread_id": identity.run_id},
                  "recursion_limit": max(64, self.limits.max_model_steps * 6 + 20)}
        snapshot = await graph.aget_state(config)
        from .runtime import _pending_calls, validate_execution_waits

        async def acknowledge_wake(state):
            waits = validate_execution_waits(tuple(state["execution_wake"]))
            if not adapter.acknowledge_execution_waits or _pending_calls(state["messages"]):
                raise ValueError("execution_wake_requires_paired_messages")
            committed = await graph.aget_state(config)
            if committed.values.get("execution_wake") != list(waits):
                raise ValueError("execution_wake_commit_required")
            reference = committed.config["configurable"]["checkpoint_id"]
            try:
                await adapter.acknowledge_execution_waits(identity, waits, reference)
            except Exception as exc:
                # Keep the committed receipt and preserve the exact checkpoint
                # reference if an acknowledgement cannot presently be written.
                import logging
                logging.getLogger(__name__).warning("execution_wake_ack_deferred: %s", type(exc).__name__)
                return None
            changes = {"execution_wake": [], "execution_waits": [],
                       "operation_signals": None, "batch_status": "", "host_state": host_snapshot()}
            if time.monotonic() >= self.limits.deadline:
                changes.update(stop_status="incomplete", stop_reason="deadline")
            # Establish a settled head while no model node can run. If Web
            # dies after ack but before this commit, it rereads the same ID.
            await graph.aupdate_state(config, changes, as_node="Suspension.before_model")
            return {**state, **changes}

        def deferred_wake(state):
            return self._result({**state, "stop_status": "suspended", "stop_reason": "execution_pending",
                                 "execution_waits": state["execution_wake"]})

        prior = snapshot.values
        continuing_thread = bool(prior and not resume and prior.get("active_run_id") != identity.run_id)
        quarantined = False
        if continuing_thread:
            closed = not snapshot.next and prior.get("stop_status") in {"complete", "incomplete", "cancelled"}
            before_model = set(snapshot.next) <= {"model", "Policy.before_model", "Suspension.before_model"}
            orphan = await adapter.check_terminal_orphan(prior) if adapter.check_terminal_orphan else False
            quarantined = orphan == "quarantined"
            orphan_closed = bool(before_model and orphan)
            answered_question = (adapter.authorized_question_parent == prior.get("active_run_id")
                                 and len(snapshot.interrupts) == 1 and snapshot.interrupts[0].value == {
                                     "kind": "user_input", "wait_ids": []})
            if (prior.get("executor_id") != EXECUTOR_ID or (not quarantined and (
                    not (closed or orphan_closed or answered_question)
                    or has_unsettled_work(prior, ignore_question=answered_question)
                    or (snapshot.interrupts and not answered_question)))):
                raise AdmissionStopped("native_thread_requires_reconciliation")
        initial = None
        if prior and not continuing_thread:
            if not resume:
                raise ValueError("existing_turn_requires_explicit_resume")
            if prior.get("executor_id") != EXECUTOR_ID:
                return self._result({**prior, "stop_status": "incomplete",
                    "stop_reason": "executor_checkpoint_incompatible"})
            if prior.get("active_run_id") != identity.run_id:
                raise ValueError("execution_checkpoint_identity_mismatch")
            if adapter.restore_host:
                adapter.restore_host(prior["host_state"])
                # ToolNode can commit before the next complete Host snapshot.
                # Draw calls can produce joint snapshots, so read the latest
                # count/attempt watermark without depending on tool ordering.
                from src.core.figures import reserve_legacy_indices, restore_recovery
                figures = [item["figure_recovery"] for item in (prior.get("operation_signals") or {}).values()
                           if "figure_recovery" in item]
                if figures:
                    restore_recovery(identity, max(figures, key=lambda item: (item["attempts"], len(item["entries"]))))
                elif "figures" not in prior["host_state"]:
                    reserve_legacy_indices(m.content for m in prior["messages"] if isinstance(m, ToolMessage)
                        and m.name == "draw_svg" and m.additional_kwargs.get("run_id") == identity.run_id
                        and m.additional_kwargs.get("host_publication"))
                # The suspension occurs after ToolNode committed canonical
                # observations. Rehydrate outstanding publication without I/O replay.
                if adapter.restore_tool_observation:
                    proposed = {call["id"]: call for message in prior["messages"] if isinstance(message, AIMessage)
                                and message.additional_kwargs.get("run_id") == identity.run_id
                                for call in message.tool_calls}
                    for message in prior["messages"]:
                        if (isinstance(message, ToolMessage)
                                and message.additional_kwargs.get("run_id") == identity.run_id
                                and message.additional_kwargs.get("host_publication")):
                            adapter.restore_tool_observation(message, proposed.get(message.tool_call_id))
            if _pending_calls(prior["messages"]):
                from .recovery import committed_tool_receipts
                if (not adapter.restore_tool_observation or set(snapshot.next) != {"tools"}
                        or snapshot.interrupts):
                    raise AdmissionStopped("recovery_requires_reconciliation")
                recovered = await committed_tool_receipts(identity, prior["messages"])
                pairs, recovered_figures = [], []
                for message, call in recovered:
                    private = dict(message.additional_kwargs)
                    if figure := private.pop("host_figure_recovery", None):
                        recovered_figures.append(figure)
                    message = message.model_copy(update={"additional_kwargs": private})
                    adapter.restore_tool_observation(message, call)
                    pairs.append(message)
                if recovered_figures:
                    from src.core.figures import restore_recovery
                    restore_recovery(identity, max(recovered_figures,
                        key=lambda item: (item["attempts"], len(item["entries"]))))
                if self.validate_resume:
                    self.validate_resume()
                # Publish the matching, authorized observations as the already
                # completed tools node. No callable tool enters this path.
                await graph.aupdate_state(config, {"messages": pairs,
                    "host_state": host_snapshot()}, as_node="tools")
                snapshot = await graph.aget_state(config)
                prior = snapshot.values
            await observed(prior)
            if self.validate_resume:
                self.validate_resume()
            if (_pending_calls(prior["messages"]) or set(snapshot.next) - {
                    "model", "Policy.before_model", "Suspension.before_model"}):
                raise AdmissionStopped("recovery_requires_reconciliation")
            if snapshot.interrupts:
                waiting = _interruption(snapshot)
                await observed(waiting)
                if waiting["stop_status"] != "suspended" or not adapter.resolve_execution_waits:
                    return self._result(waiting)
                waits = tuple(waiting["execution_waits"])
                notes = await adapter.resolve_execution_waits(identity, waits)
                if notes is None:
                    return self._result(waiting)
                initial = Command(resume={snapshot.interrupts[0].id: {"completion_notes": list(notes)}})
            elif prior.get("execution_wake"):
                settled = await acknowledge_wake(prior)
                if settled is None:
                    return deferred_wake(prior)
                if settled.get("stop_status"):
                    await observed(settled, "stopped")
                    await event("run_finished", settled)
                    return self._result(settled)
                await observed(settled)
            elif prior.get("stop_status"):
                # An already accepted answer can be delivered without another
                # model call. Supplementary requirements need a fresh model step.
                notes = (await adapter.prepare(identity) if adapter.prepare and
                         prior["stop_reason"] == "answered" else ())
                if not notes:
                    return self._result(prior)
                initial = {"messages": [control(note) for note in notes], "stop_status": "",
                           "stop_reason": "", "host_state": host_snapshot()}
        else:
            if resume:
                raise ValueError("execution_checkpoint_missing")
            additions = list(messages)
            if quarantined:
                # Keep the old protected checkpoint for diagnosis, but replace
                # the live message head with trusted history and the new turn.
                additions.insert(0, RemoveMessage(id=REMOVE_ALL_MESSAGES))
            elif continuing_thread:
                previous_request = (prior.get("host_state") or {}).get("original_request_state") or {}
                previous_id = previous_request.get("request_id")
                anchor = next((index for index in range(len(messages) - 2, -1, -1)
                               if previous_id and isinstance(messages[index], AIMessage)
                               and not messages[index].tool_calls
                               and messages[index].additional_kwargs.get("context_trusted") is True
                               and messages[index].additional_kwargs.get("context_origin") == "assistant"
                               and messages[index].additional_kwargs.get("request_id") == previous_id),
                              len(messages) - 2)
                additions = [message for message in messages[anchor + 1:]
                             if message.additional_kwargs.get("context_origin") != "historical_evidence"]
            initial = {"messages": additions, "model_steps": 0, "host_state": host_snapshot(),
                "executor_id": EXECUTOR_ID, "active_run_id": identity.run_id, "pinned_skills": list(pinned_skills),
                "skills_metadata": None, "delivery_followup_used": False, "operation_signals": None,
                "unknown_operations": [], "execution_waits": [], "execution_wake": [], "batch_status": "",
                "stop_status": "", "stop_reason": "", "rejected_responses": [], "seed_proposal": []}
            if quarantined:
                initial.update(working_context={}, batch_notes=[])
        await event("run_started", latest)
        if isinstance(initial, Command):
            # Only the Suspension middleware can run through this breakpoint.
            # Commit the paired observation before acknowledging its durable
            # notification, independently of any remaining model budget.
            await graph.ainvoke(initial, config, durability="sync",
                                interrupt_after=["Suspension.before_model"])
            committed = (await graph.aget_state(config)).values
            if not committed.get("execution_wake"):
                raise ValueError("execution_wake_commit_required")
            settled = await acknowledge_wake(committed)
            if settled is None:
                return deferred_wake(committed)
            if settled.get("stop_status"):
                await observed(settled, "stopped")
                await event("run_finished", settled)
                return self._result(settled)
            await observed(settled)
            initial = None
        timer = asyncio.timeout(max(0, self.limits.deadline-time.monotonic())
                                if self.limits.deadline != float("inf") else None)
        try:
            async with timer:
                state = await graph.ainvoke(initial, config, durability="sync")
                if state.get("__interrupt__"):
                    state = _interruption(await graph.aget_state(config))
            if timer.expired():
                state = {**latest, "stop_status": "incomplete", "stop_reason": "deadline"}
        except AdmissionStopped as exc:
            state = {**latest, "stop_status": "incomplete", "stop_reason": exc.reason}
        except (asyncio.CancelledError, NodeCancelledError):
            state = {**latest, "stop_status": "cancelled", "stop_reason": "cancelled"}
        except TimeoutError:
            if not timer.expired():
                raise
            state = {**latest, "stop_status": "incomplete", "stop_reason": "deadline"}
        state = {**state, "host_state": host_snapshot()}
        if (state.get("stop_status") in {"incomplete", "cancelled"}
                and state.get("stop_reason") in {"cancelled", "deadline"}):
            await graph.aupdate_state(config, {key: state[key] for key in
                ("stop_status", "stop_reason", "host_state")})
        await observed(state, "stopped")
        await event("run_finished", state)
        return self._result(state)

    @staticmethod
    def _result(state):
        from .runtime import _pending_calls
        status = state.get("stop_status", "incomplete")
        messages = tuple(state.get("messages", []))
        return RunResult(status, state.get("stop_reason", "execution_stopped"), messages,
            state.get("model_steps", 0), messages[-1] if status == "complete" else None,
            tuple(state.get("unknown_operations", [])), _pending_calls(messages),
            tuple(state.get("execution_waits", [])))


def _interruption(snapshot):
    from .runtime import _pending_calls, validate_execution_waits

    state = snapshot.values
    if len(snapshot.interrupts) != 1 or _pending_calls(state["messages"]):
        raise ValueError("invalid_framework_interrupt")
    value = snapshot.interrupts[0].value
    if type(value) is not dict or set(value) != {"kind", "wait_ids"}:
        raise ValueError("invalid_framework_interrupt")
    waits = validate_execution_waits(tuple(value["wait_ids"]))
    if value["kind"] == "execution_wait" and waits:
        return {**state, "stop_status": "suspended", "stop_reason": "execution_pending",
                "execution_waits": list(waits)}
    if value["kind"] == "user_input" and not waits:
        return {**state, "stop_status": "waiting", "stop_reason": "user_input_required"}
    raise ValueError("invalid_framework_interrupt")


def has_unsettled_work(values, *, ignore_question=False):
    from .runtime import _pending_calls

    return bool(values.get("batch_status") in ({"unknown", "execution"} if ignore_question else {"unknown", "waiting", "execution"})
        or values.get("unknown_operations") or values.get("execution_waits") or values.get("execution_wake")
        or (not ignore_question and (values.get("host_state") or {}).get("pending_question"))
        or any(item.get("uncertain") or (not ignore_question and item.get("pending_question")) or item.get("execution_waits")
               for item in (values.get("operation_signals") or {}).values())
        or _pending_calls(values.get("messages", ())))


@asynccontextmanager
async def open_terminal_graph(root, identity, provider_scope, *, context_scope=""):
    """Open the storage-only graph used by the separate terminal maintenance worker.

    Caller validates the exact settled checkpoint and holds the session/OS
    execution lock while reading or applying a compare-and-swap update.
    """
    from .checkpoint_store import framework_checkpoint_path, framework_checkpoint_scope, open_framework_checkpoint
    from .recovery import _checkpoint_reader

    if not framework_checkpoint_path(root, identity, provider_scope, context_scope=context_scope).is_file():
        raise FileNotFoundError("framework_checkpoint_missing")
    config = {"configurable": framework_checkpoint_scope(identity, provider_scope, context_scope=context_scope)}
    async with open_framework_checkpoint(root, identity, provider_scope, context_scope=context_scope) as saver:
        async def check_committed(snapshot):
            stored = await saver.aget_tuple(snapshot.config)
            if stored is None or snapshot.values.get("executor_id") != EXECUTOR_ID:
                return False
            channels = stored.checkpoint.get("channel_values") or {}
            return (channels.get("active_run_id") == identity.run_id
                    and channels.get("stop_status") == "complete"
                    and not any(name.startswith("branch:to:") for name in channels)
                    and not any(channel in {"__interrupt__", "__pregel_tasks", "__error__"}
                                for _, channel, _ in stored.pending_writes or ())
                    and not has_unsettled_work(snapshot.values))

        yield _checkpoint_reader(saver, maintenance=True), config, check_committed
