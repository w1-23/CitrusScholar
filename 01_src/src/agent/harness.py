"""Production Deep Agents adapter; its graph owns model/tool transitions.

Domain-free policy hooks preserve the existing metered ModelDriver, batch
scheduler, operation runner and publication boundary. No direct tool fallback.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import inspect
import os
from pathlib import Path
import time
from typing import Annotated, Any

from deepagents import DeepAgentState
from langchain.agents.middleware import AgentMiddleware, ModelRefusalMiddleware, hook_config
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt
from langgraph.errors import NodeCancelledError
from typing_extensions import NotRequired

from .events import LoopCheckpoint, LoopEvent
from .model import NON_SUCCESS_TERMINALS
from .plan_tools import NATIVE_PLAN_TOOL, publish_native_plan
from .runtime import AdmissionStopped, RunResult, RunLimits
from .deep_agent_runtime import EXECUTOR_ID, NATIVE_READ_TOOLS, create_framework_agent


def _merge_signals(left, right):
    return {} if right is None else {**(left or {}), **right}


@dataclass(frozen=True)
class _RunContext:
    """Server-supplied identity injected by ToolNode, never a tool argument."""
    identity: Any


class HarnessState(DeepAgentState):
    executor_id: NotRequired[str]
    operation_signals: NotRequired[Annotated[dict | None, _merge_signals]]
    # Legacy checkpoint channel; inspect for unresolved proposals, never write it.
    seed_proposal: NotRequired[list[dict]]
    active_run_id: NotRequired[str]
    model_steps: NotRequired[int]
    host_state: NotRequired[dict]
    delivery_followup_used: NotRequired[bool]
    batch_status: NotRequired[str]
    unknown_operations: NotRequired[list[str]]
    execution_waits: NotRequired[list[str]]
    # A synchronously committed wake awaiting host acknowledgment. Keep it in
    # the checkpoint until all acknowledgments succeed, including after a crash.
    execution_wake: NotRequired[list[str]]
    user_input_wake: NotRequired[dict]
    stop_status: NotRequired[str]
    stop_reason: NotRequired[str]
    # Raw rejected responses remain in the encrypted execution checkpoint,
    # including provider-private state; they are never public tool evidence.
    rejected_responses: NotRequired[list[AIMessage]]


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
    # Trusted host callbacks only. Resolution reads actual queue facts; ack is
    # called only after a synchronous checkpoint commit, before another model.
    resolve_execution_waits: Any = None
    acknowledge_execution_waits: Any = None
    validate_parked_snapshot: Any = None
    record_rejected: Any = None
    check_final_after_followup: Any = None
    tool_mode: Any = None
    not_run: Any = None
    check_terminal_orphan: Any = None
    restore_tool_observation: Any = None
    user_input: dict | None = None
    bind_user_input: Any = None
    accept_user_input: Any = None
    acknowledge_user_input: Any = None
    check_retired_question: Any = None
    model_call: Any = None
    diagnostic_checkpoint: str = ""

    async def run(self, identity, messages, *, saver, resume=False, checkpoint_config=None,
                  agent_options=None, pinned_skills=()):
        adapter = self
        latest = {"messages": list(messages), "model_steps": 0}
        read_permits = asyncio.Semaphore(4)
        exclusive = asyncio.Lock()
        update_lock = asyncio.Lock()
        prepared = {}
        published_todos = set()
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
                additions = [control(note) for note in notes]
                await admit(tuple([*state["messages"], *additions]))
                update = {"messages": additions, "operation_signals": None,
                          "execution_waits": [], "execution_wake": [], "batch_status": "",
                          "delivery_followup_used": followup_used,
                          "model_steps": state.get("model_steps", 0) + 1,
                          "host_state": host_snapshot()}
                if not state.get("model_steps"):
                    update["todos"] = []  # Start with a new plan at each admitted turn.
                # Before-model state is synchronously checkpointed before the call.
                await observed({**state, **update, "messages": [*state["messages"], *additions]}, "model")
                await event("model_started", latest)
                return update

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
                if not invalid.startswith("model_") and ids and (any(not isinstance(cid, str) or not cid.strip() or cid != cid.strip()
                                or any(ord(char) < 32 for char in cid) for cid in ids)
                            or len(set(ids)) != len(ids) or previous.intersection(ids)):
                    invalid = "invalid_or_reused_tool_call_id"
                await event("model_finished", state, ids, output_limit_exhausted=(
                    response.response_metadata.get("citrus_output_limit_exhausted")
                    if invalid == "model_length" else None))
                if response.response_metadata.get("citrus_stream_terminal_unconfirmed"):
                    await event("model_output_rejected", {**state, "stop_reason": "model_stream_unconfirmed"}, ids)
                    return {"messages": [RemoveMessage(id=response.id)],
                            "rejected_responses": [*state.get("rejected_responses", []), response],
                            "stop_status": "incomplete", "stop_reason": "model_stream_unconfirmed", "jump_to": "end"}
                if adapter.recover_output and invalid == "invalid_tool_call":
                    # No tool in this response has been dispatched. Preserve raw
                    # arguments privately, and pair every valid native call with
                    # an explicit not_run result before asking the SAME model.
                    cleaned = response.model_copy(deep=True)
                    cleaned.invalid_tool_calls = []
                    cleaned.additional_kwargs.pop("tool_calls", None)
                    feedback_reason = "invalid_tool_arguments"
                    feedback = "工具参数不是完整合法的JSON，无法解析"
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
                # Tool-round draft text stays inside the execution graph.
                return None

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
                    todos = None
                    if reason:
                        if adapter.not_run and name in host_tools:
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
                        result = await handler(request)
                        signal = {}
                        if name in host_tools:
                            update = result.update if isinstance(result, Command) else None
                            if (not isinstance(update, dict) or set(update) != {"messages", "operation_signals"}
                                    or not isinstance(update["messages"], list) or len(update["messages"]) != 1
                                    or not isinstance(update["operation_signals"], dict)
                                    or set(update["operation_signals"]) != {cid}
                                    or not isinstance(update["operation_signals"][cid], dict)):
                                raise ValueError("operation_observation_command_invalid")
                            message, signal = update["messages"][0], update["operation_signals"][cid]
                        elif name == NATIVE_PLAN_TOOL and isinstance(result, Command):
                            update = result.update
                            if (not isinstance(update, dict) or set(update) != {"messages", "todos"}
                                    or not isinstance(update["todos"], list)
                                    or not isinstance(update["messages"], list) or len(update["messages"]) != 1):
                                raise ValueError("native_todo_command_invalid")
                            message, todos = update["messages"][0], update["todos"]
                        else:
                            message = result
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
                    if todos is not None:
                        return Command(update={"todos": todos, "messages": [message]})
                    if not signal:
                        return message
                    return Command(update={"messages": [message], "operation_signals": {cid: signal}})

        def framework_tool(schema):
            name = schema["name"]

            async def invoke(runtime: ToolRuntime[_RunContext], **kwargs):
                if not isinstance(runtime.context, _RunContext) or runtime.context.identity != identity:
                    raise ValueError("operation_identity_mismatch")
                cid = runtime.tool_call_id
                calls = [call for call in related_round(runtime.state).tool_calls if call.get("id") == cid]
                if len(calls) != 1 or calls[0].get("name") != name or calls[0].get("args") != kwargs:
                    raise ValueError("operation_invocation_pairing_mismatch")
                message, details = await adapter.execute(calls[0], runtime.context.identity)
                return Command(update={"messages": [message], "operation_signals": {cid: details}})

            return StructuredTool(name=name, description=schema["description"],
                                  args_schema=schema["parameters"], coroutine=invoke)

        class Suspension(AgentMiddleware):
            state_schema = HarnessState

            async def abefore_model(self, state, runtime):
                signals = list((state.get("operation_signals") or {}).values())
                if any(item.get("uncertain") for item in signals):
                    return None
                questions = [item["pending_question"] for item in signals if item.get("pending_question")]
                waits = tuple(dict.fromkeys(wait for item in signals for wait in item.get("execution_waits", ())))
                if not questions and not waits:
                    publish_native_plan(state, identity, published_todos)
                    return None
                from .runtime import validate_execution_waits, _pending_calls
                validate_execution_waits(waits)
                if _pending_calls(state["messages"]) or (questions and waits) or len(questions) > 1:
                    raise ValueError("execution_suspension_requires_paired_messages")
                if questions:
                    from src.core.user_questions import QuestionResume
                    question = questions[0]
                    wake = QuestionResume.model_validate(interrupt({"kind": "user_input", **question},
                        response_schema=QuestionResume.model_json_schema()))
                    if (wake.question_id, wake.version) != (question["question_id"], question["version"]):
                        raise ValueError("user_input_resume_identity_mismatch")
                    if adapter.accept_user_input:
                        adapter.accept_user_input(wake)
                    reply = HumanMessage(content="本轮澄清答复：" +
                        wake.model_dump_json(include={"selected_option_ids", "selected_labels", "text"}),
                        additional_kwargs={**identity.message_metadata("internal_control"), "clarification_reply": True})
                    return {"messages": [reply],
                        "operation_signals": None, "batch_status": "", "user_input_wake": wake.model_dump(),
                        "host_state": host_snapshot()}
                wake = interrupt({"kind": "execution_wait", "wait_ids": list(waits)})
                if not isinstance(wake, dict) or set(wake) != {"completion_notes"}:
                    raise ValueError("invalid_execution_resume")
                notes = tuple(wake["completion_notes"])
                if not notes or any(type(note) is not str for note in notes) or sum(map(len, notes)) > 65536:
                    raise ValueError("invalid_execution_completion_observations")
                return {"messages": [control(note) for note in notes], "operation_signals": None,
                        "execution_wake": list(waits), "host_state": host_snapshot()}

        class ProviderPolicy(ModelRefusalMiddleware):
            # Replace the named stock hook so Filesystem and Skills modify the
            # request before admission. Preserve the stock refusal handling.
            @property
            def name(self):
                return "ModelRefusalMiddleware"

            async def awrap_model_call(self, request, handler):
                async def stamped(req):
                    req = req.override(model=adapter.model)
                    try:
                        result = (await adapter.model_call(req, handler, identity, host_tools)
                                  if adapter.model_call else await handler(req))
                    except asyncio.CancelledError as exc:
                        task = asyncio.current_task()
                        if task is not None and task.cancelling():
                            raise
                        raise NodeCancelledError("model") from exc
                    if len(result.result) != 1 or not isinstance(result.result[0], AIMessage):
                        raise TypeError("model_must_return_ai_message")
                    message = result.result[0]
                    message = message.model_copy(update={"additional_kwargs": {
                        **message.additional_kwargs, **identity.message_metadata("assistant")}})
                    return replace(result, result=[message])

                return await super().awrap_model_call(request, stamped)

        tools = [framework_tool(item["function"]) for item in self.schemas]
        host_tools = frozenset(tool.name for tool in tools)
        run_context = _RunContext(identity)
        graph = create_framework_agent(self.model, tools, [Suspension(), Policy(), ProviderPolicy()], saver,
                                       HarnessState, context_schema=_RunContext, **(agent_options or {}))
        config = {"configurable": checkpoint_config or {"thread_id": identity.run_id},
                  "recursion_limit": max(64, self.limits.max_model_steps * 6 + 20)}
        snapshot = await graph.aget_state(config)
        self.diagnostic_checkpoint = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id", "")
        topology = graph.get_graph()
        tool_nodes = tuple(name for name, node in topology.nodes.items() if isinstance(node.data, ToolNode))
        # The native before-model chain can include conditional middleware
        # (e.g. jump to end). Keep only paths reachable from entry without
        # crossing the model and which still reach the model destination.
        before_model_entry = {topology.first_node().id}
        while later := {edge.target for edge in topology.edges if edge.source in before_model_entry
                       and edge.source != "model" and edge.target not in before_model_entry
                       and edge.target not in tool_nodes}:
            before_model_entry.update(later)
        pre_model_nodes = {"model"}
        while parents := {edge.source for edge in topology.edges if edge.target in pre_model_nodes
                          and edge.source in before_model_entry and edge.source not in pre_model_nodes}:
            pre_model_nodes.update(parents)
        from .runtime import _pending_calls, validate_execution_waits

        async def result_for(state):
            result = self._result(state)
            if result.status == "suspended" and adapter.validate_parked_snapshot:
                adapter.validate_parked_snapshot(await graph.aget_state(config), result)
            elif result.status == "waiting" and adapter.bind_user_input:
                parked = await graph.aget_state(config)
                pause = parked.interrupts[0]
                await adapter.bind_user_input(pause.value, pause.id,
                    parked.config["configurable"]["checkpoint_id"])
            return result

        async def acknowledge_user_input(committed):
            value = committed.values.get("user_input_wake")
            if value != adapter.user_input["value"]:
                raise ValueError("user_input_wake_commit_required")
            if adapter.acknowledge_user_input:
                await adapter.acknowledge_user_input(value, committed.config["configurable"]["checkpoint_id"])

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
            # The durable wake marker is only written by the node just resumed.
            # LangGraph infers the unique previous writer from the actual head.
            await graph.aupdate_state(config, changes)
            return {**state, **changes}

        async def deferred_wake(state):
            return await result_for({**state, "stop_status": "suspended", "stop_reason": "execution_pending",
                                     "execution_waits": state["execution_wake"]})

        prior = snapshot.values
        if _unsettled_legacy_seed(prior):
            raise AdmissionStopped("recovery_requires_reconciliation")
        continuing_thread = bool(prior and not resume and prior.get("active_run_id") != identity.run_id)
        quarantined = False
        if continuing_thread:
            closed = not snapshot.next and prior.get("stop_status") in {"complete", "incomplete", "cancelled"}
            before_model = set(snapshot.next) <= pre_model_nodes
            orphan = await adapter.check_terminal_orphan(prior) if adapter.check_terminal_orphan else False
            quarantined = orphan == "quarantined"
            orphan_closed = bool(before_model and orphan)
            retired = (adapter.check_retired_question and len(snapshot.interrupts) == 1
                       and isinstance(snapshot.interrupts[0].value, dict)
                       and snapshot.interrupts[0].value.get("kind") == "user_input"
                       and await adapter.check_retired_question(prior.get("active_run_id"))
                       and not has_unsettled_work(prior, ignore_question=True))
            quarantined = quarantined or retired
            if (prior.get("executor_id") != EXECUTOR_ID or (not quarantined and (
                    not (closed or orphan_closed) or has_unsettled_work(prior) or snapshot.interrupts))):
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
                if (not adapter.restore_tool_observation or len(tool_nodes) != 1
                        or snapshot.next != tool_nodes or snapshot.interrupts):
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
                    "host_state": host_snapshot()}, as_node=tool_nodes[0])
                snapshot = await graph.aget_state(config)
                prior = snapshot.values
            await observed(prior)
            if self.validate_resume:
                self.validate_resume()
            if _pending_calls(prior["messages"]) or set(snapshot.next) - pre_model_nodes:
                raise AdmissionStopped("recovery_requires_reconciliation")
            if snapshot.interrupts:
                waiting = _interruption(snapshot)
                await observed(waiting)
                if waiting["stop_status"] == "waiting":
                    if adapter.user_input is None:
                        return await result_for(waiting)
                    from .recovery import verify_question_interrupt
                    verify_question_interrupt(snapshot, adapter.user_input)
                    initial = Command(resume={snapshot.interrupts[0].id: adapter.user_input["value"]})
                else:
                    if not adapter.resolve_execution_waits:
                        return await result_for(waiting)
                    waits = tuple(waiting["execution_waits"])
                    notes = await adapter.resolve_execution_waits(identity, waits)
                    if notes is None:
                        return await result_for(waiting)
                    initial = Command(resume={snapshot.interrupts[0].id: {"completion_notes": list(notes)}})
            elif adapter.user_input is not None:
                await acknowledge_user_input(snapshot)
            elif prior.get("execution_wake"):
                settled = await acknowledge_wake(prior)
                if settled is None:
                    return await deferred_wake(prior)
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
                "unknown_operations": [], "execution_waits": [], "execution_wake": [], "user_input_wake": {}, "batch_status": "",
                "stop_status": "", "stop_reason": "", "rejected_responses": []}
        await event("run_started", latest)
        if isinstance(initial, Command):
            # Only the Suspension middleware can run through this breakpoint.
            # Commit the paired observation before acknowledging its durable
            # notification, independently of any remaining model budget.
            match = [task.name for task in snapshot.tasks if task.interrupts == snapshot.interrupts]
            if len(match) != 1 or tuple(snapshot.next) != tuple(match):
                raise AdmissionStopped("recovery_requires_reconciliation")
            await graph.ainvoke(initial, config, durability="sync", context=run_context,
                                interrupt_after=match)
            committed_snapshot = await graph.aget_state(config)
            committed = committed_snapshot.values
            if adapter.user_input is not None:
                await acknowledge_user_input(committed_snapshot)
                await observed(committed)
            else:
                if not committed.get("execution_wake"):
                    raise ValueError("execution_wake_commit_required")
                settled = await acknowledge_wake(committed)
                if settled is None:
                    return await deferred_wake(committed)
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
                result = await graph.ainvoke(initial, config, durability="sync", context=run_context, version="v2")
                state = _interruption(await graph.aget_state(config)) if result.interrupts else result.value
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
        return await result_for(state)

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
    if type(value) is not dict:
        raise ValueError("invalid_framework_interrupt")
    if value.get("kind") == "user_input" and (set(value) == {"kind", "question_id", "version"}
            or value == {"kind": "user_input", "wait_ids": []}):
        return {**state, "stop_status": "waiting", "stop_reason": "user_input_required"}
    if value.get("kind") == "execution_wait" and set(value) == {"kind", "wait_ids"}:
        waits = validate_execution_waits(tuple(value["wait_ids"]))
        if waits:
            return {**state, "stop_status": "suspended", "stop_reason": "execution_pending",
                    "execution_waits": list(waits)}
    raise ValueError("invalid_framework_interrupt")


def _unsettled_legacy_seed(state):
    """A retired synthetic tool proposal requires an existing paired result."""
    messages = state.get("messages", ())
    settled = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    staged = state.get("seed_proposal") or ()
    if any(not isinstance(call, dict) or call.get("id") not in settled for call in staged):
        return True
    return any(call.get("id") not in settled for message in messages if isinstance(message, AIMessage)
               and message.additional_kwargs.get("citrus_retrieval_seed") is True for call in message.tool_calls)


def has_unsettled_work(values, *, ignore_question=False):
    from .runtime import _pending_calls

    return bool(values.get("batch_status") in ({"unknown", "execution"} if ignore_question else {"unknown", "waiting", "execution"})
        or values.get("unknown_operations") or values.get("execution_waits") or values.get("execution_wake")
        or (not ignore_question and (values.get("host_state") or {}).get("pending_question"))
        or any(item.get("uncertain") or (not ignore_question and item.get("pending_question")) or item.get("execution_waits")
               for item in (values.get("operation_signals") or {}).values())
        or _pending_calls(values.get("messages", ())))
