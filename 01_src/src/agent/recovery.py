"""Explicit same-run recovery from protected server state, never client inputs.

Admission does not replay operations or reserve another quota. The API owns the
account lease and worker lifecycle; the jobs transaction owns the dispatch CAS.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import math
from pathlib import Path
import time

from langgraph.checkpoint.base import INTERRUPT

from .identity import ExecutionIdentity
from .checkpoint_store import (
    CheckpointProtectionError, checkpoint_path, framework_checkpoint_path, framework_checkpoint_scope,
    native_checkpoint_path, native_checkpoint_scope, open_framework_checkpoint,
    open_legacy_native_reader,
)
from src.core.task_events import TaskConflict

# Only stable server-prepared request/context fields are recoverable. Runtime
# messages are already authoritative in the inner graph checkpoint.
_REQUEST_FIELDS = frozenset({
    "query", "request_id", "client_request_id", "session_id", "user_id", "job_id",
    "idempotency_key", "visualization_enabled", "context_compression", "selected_skills", "attachment_refs", "attachment_context",
    "browse_requested", "image_attachments", "has_paper_attachment", "request_tables", "mode",
    "script_budget_scope", "context_working_state", "history_summary",
    "long_term_memory", "resident_cards", "user_preferences", "account_files",
    "history_evidence_block", "history_load_status", "history_load_reason", "history_load_error", "search_suggestions", "format_hint",
    "candidate_pool", "candidate_pool_notice", "request_plan", "task_contract",
    "native_skill_release", "skill_release_inventory_sha256", "capability_snapshot", "capability_runtime",
})
# Accepted only to decode original requests from older authenticated checkpoints.
_RETIRED_REQUEST_FIELDS = frozenset({"agent_no_prefetch_hints", "parent_job_id", "question_answer",
                                     "continuation_context", "no_replay"})


def capture_original_request_state(state: dict) -> dict:
    """Called once by the host before mutable task updates are restored/applied."""
    return deepcopy({key: value for key, value in state.items() if key in _REQUEST_FIELDS
                     and (key != "browse_requested" or value is True)})


def validate_original_request_state(value, identity: ExecutionIdentity) -> dict:
    if type(value) is not dict or set(value) - (_REQUEST_FIELDS | _RETIRED_REQUEST_FIELDS):
        raise TaskConflict("recovery_original_request_missing")
    if (type(value.get("user_id")) is not int
            or value["user_id"] != identity.owner_user_id
            or value.get("session_id") != identity.session_id
            or value.get("request_id") != identity.user_turn_id
            or value.get("job_id") != identity.run_id):
        raise TaskConflict("recovery_identity_mismatch")
    if (not isinstance(value.get("query"), str) or not value["query"].strip()
            or not isinstance(value.get("client_request_id"), str)
            or not value["client_request_id"]
            or not isinstance(value.get("idempotency_key"), str)
            or not value["idempotency_key"]):
        raise TaskConflict("recovery_original_request_missing")
    return deepcopy({key: item for key, item in value.items() if key in _REQUEST_FIELDS})


def _checkpoint_reader(saver):
    from langgraph.graph import END, START, StateGraph
    from .harness import HarnessState

    # Storage-only graph. No model nodes, tools, middleware, or replay actions.
    reader = StateGraph(HarnessState)
    reader.add_edge(START, END)
    return reader.compile(checkpointer=saver)


async def is_terminal_orphan(identity, values, *, answered_question=False):
    """Use authoritative job ownership and durable operation state, not a job ID exception."""
    from src.core import jobs, task_events
    from .harness import has_unsettled_work

    active = values.get("active_run_id")
    if not isinstance(active, str) or not active or active == identity.run_id:
        return False
    original = (values.get("host_state") or {}).get("original_request_state") or {}
    expected = (identity.owner_user_id, identity.session_id, original.get("request_id"), active)
    if tuple(original.get(k) for k in ("user_id", "session_id", "request_id", "job_id")) != expected:
        return False

    def read_job():
        with task_events.transaction() as conn:
            row = conn.execute("SELECT owner_user_id,session_id,request_id,job_id,status,current_step,terminal_snapshot "
                               "FROM task_jobs WHERE job_id=?", (active,)).fetchone()
            if (not row or tuple(row[k] for k in ("owner_user_id", "session_id", "request_id", "job_id")) != expected
                    or row["status"] not in ({"awaiting_input"} if answered_question else
                        jobs.TERMINAL_STATUS - {"awaiting_input"}) or not row["terminal_snapshot"]):
                return False
            if row["current_step"] == "reconciled" and not answered_question:
                from src.core.chat_turn_queue import _segment_phase
                if _segment_phase(conn, active) in {"", "quarantined"} and jobs.durable_terminal(conn, row):
                    return "quarantined"
            return (not has_unsettled_work(values, ignore_question=answered_question)
                    and conn.execute("SELECT 1 FROM task_operations WHERE job_id=? AND owner_user_id=? "
                                     "AND status IN ('running','interrupted','unknown') LIMIT 1",
                                     (active, identity.owner_user_id)).fetchone() is None)
    return await asyncio.to_thread(read_job)


async def validate_legacy_native_head(root, identity, provider_scope, *, context_scope=""):
    """Admit only a settled predecessor. Never invoke or change the legacy graph."""
    from .deep_agent_runtime import LEGACY_NATIVE_EXECUTOR_ID
    from .harness import has_unsettled_work
    from .runtime import AdmissionStopped
    from langgraph.types import Send

    # Early native threads had no capability discriminator. Inspect them as
    # well for the same owner/session/provider, without importing their context.
    for scope in dict.fromkeys((context_scope, "")):
        options = {"context_scope": scope}
        if not native_checkpoint_path(root, identity, provider_scope, **options).is_file():
            continue
        async with open_legacy_native_reader(root, identity, provider_scope, **options) as saver:
            saved = await saver.aget_tuple({"configurable": native_checkpoint_scope(identity, provider_scope, **options)})
            if saved is None:
                continue
            values = (await _checkpoint_reader(saver).aget_state(saved.config)).values
            pending = saved.pending_writes or ()
            channels = saved.checkpoint.get("channel_values") or {}
            pending_tool = any(k.startswith("branch:to:") and "tool" in k for k in channels)
            pending_tool |= any(isinstance(v, Send) and "tool" in v.node for _, _, v in pending)
            pending_tool |= any(channel == INTERRUPT for _, channel, _ in pending)
            from src.core.user_questions import retired_question
            answered_question = (values.get("stop_status") == "waiting"
                                 and values.get("stop_reason") == "user_input_required"
                                 and await asyncio.to_thread(retired_question, identity, values.get("active_run_id")))
            if (values.get("executor_id") != LEGACY_NATIVE_EXECUTOR_ID or not values.get("messages")
                    or has_unsettled_work(values, ignore_question=answered_question) or pending_tool
                    or not await is_terminal_orphan(identity, values, answered_question=answered_question)):
                raise AdmissionStopped("native_thread_requires_reconciliation")


async def committed_tool_receipts(identity, messages):
    """Recover the exact paired observations, never call a pending tool again."""
    from langchain_core.messages import AIMessage, ToolMessage
    from src.core import task_events
    from .runtime import _pending_calls

    pending = _pending_calls(messages)
    if not pending:
        return []
    proposal = next((value for value in reversed(messages) if isinstance(value, AIMessage)), None)
    if proposal is None or proposal.additional_kwargs.get("run_id") != identity.run_id:
        raise TaskConflict("recovery_requires_reconciliation")
    calls = {call["id"]: call for call in proposal.tool_calls}

    def read():
        recovered = []
        with task_events.transaction() as conn:
            job = task_events._job(conn, identity.run_id)
            if (job["owner_user_id"], job["session_id"], job["request_id"]) != (
                    identity.owner_user_id, identity.session_id, identity.user_turn_id):
                raise TaskConflict("recovery_identity_mismatch")
            for cid in pending:
                call = calls[cid]
                rows = conn.execute("SELECT operation_id,host_observation FROM task_operations WHERE job_id=? "
                    "AND owner_user_id=? AND tool_call_id=? AND tool_name=? AND logical_key<>'' "
                    "AND result<>'' AND status NOT IN ('running','interrupted','unknown') AND host_observation<>'' LIMIT 2",
                    (identity.run_id, identity.owner_user_id, cid, call["name"])).fetchall()
                if len(rows) != 1:
                    raise TaskConflict("recovery_requires_reconciliation")
                try:
                    batch = json.loads(rows[0]["host_observation"])
                    if (batch.get("version") != 1 or batch.get("pending_question") is not False
                            or batch.get("unknown_operations") or batch.get("execution_waits")
                            or batch.get("context_notes") or len(batch["messages"]) != 1):
                        raise ValueError("unsettled receipt")
                    observed = ToolMessage(**batch["messages"][0])
                    if (observed.name != call["name"] or observed.tool_call_id != cid
                            or observed.additional_kwargs.get("operation_id") != rows[0]["operation_id"]
                            or observed.additional_kwargs.get("operation_uncertain")
                            or not observed.additional_kwargs.get("host_publication")):
                        raise ValueError("unbound receipt")
                    if observed.name == "draw_svg" and not observed.additional_kwargs.get("host_figure_recovery"):
                        raise ValueError("figure observation missing")
                except (AttributeError, KeyError, TypeError, ValueError):
                    raise TaskConflict("recovery_requires_reconciliation") from None
                metadata = {**observed.additional_kwargs, **identity.message_metadata("tool")}
                recovered.append((observed.model_copy(update={"additional_kwargs": metadata}), call))
        return recovered

    return await asyncio.to_thread(read)


@contextmanager
def _checkpoint_reconciliation():
    try:
        yield
    except CheckpointProtectionError as error:
        if str(error) == "legacy_delta_requires_reconciliation":
            raise TaskConflict("recovery_requires_reconciliation") from None
        raise


class QuestionWakeDeferred(RuntimeError):
    """The answer was committed by the graph and its durable ACK can retry."""


def verify_question_interrupt(snapshot, question_resume):
    _verify_question_interrupt(snapshot.interrupts,
        (snapshot.config or {}).get("configurable", {}).get("checkpoint_id"), question_resume)


def _verify_question_interrupt(interrupts, checkpoint_id, question_resume):
    from src.core.user_questions import QuestionResume
    value = QuestionResume.model_validate(question_resume["value"])
    expected = {"kind": "user_input", "question_id": value.question_id, "version": value.version}
    if (checkpoint_id != question_resume["checkpoint_ref"] or len(interrupts) != 1
            or interrupts[0].id != value.interrupt_id or interrupts[0].value != expected):
        raise TaskConflict("question_resume_checkpoint_conflict")


async def load_recovery_state(root: Path, identity: ExecutionIdentity,
                              provider_scope: tuple[str, str], *, execution_wait: dict | None = None,
                              discover_execution_wait=False, question_resume=None) -> dict:
    """Read the exact committed framework state. Legacy executors are never resumed."""
    from src.core.jobs import get_job
    from .deep_agent_runtime import EXECUTOR_ID
    from .harness import has_unsettled_work
    from .runtime import _pending_calls, validate_execution_waits

    job = await asyncio.to_thread(get_job, identity.run_id)
    if job and (job["owner_user_id"], job["session_id"], job["request_id"]) != (
            identity.owner_user_id, identity.session_id, identity.user_turn_id):
        raise TaskConflict("recovery_identity_mismatch")
    if job and job.get("question_id"):
        raise TaskConflict("legacy_question_execution_closed")
    options = {"context_scope": (job or {}).get("native_context_scope", "")}
    if not framework_checkpoint_path(root, identity, provider_scope, **options).is_file():
        if (checkpoint_path(root, identity, provider_scope).is_file()
                or any(native_checkpoint_path(root, identity, provider_scope, context_scope=scope).is_file()
                       for scope in {options["context_scope"], ""})):
            raise TaskConflict("executor_checkpoint_incompatible")
        raise TaskConflict("recovery_checkpoint_missing")
    with _checkpoint_reconciliation():
        async with open_framework_checkpoint(root, identity, provider_scope, **options, read_only=True) as saver:
            saved = await saver.aget_tuple({"configurable": framework_checkpoint_scope(identity, provider_scope, **options)})
            if saved is None:
                raise TaskConflict("recovery_checkpoint_missing")
            raw = saved.checkpoint.get("channel_values", {})
            if raw.get("executor_id") != EXECUTOR_ID:
                raise TaskConflict("executor_checkpoint_incompatible")
            if raw.get("active_run_id") != identity.run_id:
                raise TaskConflict("recovery_identity_mismatch")
            values = (await _checkpoint_reader(saver).aget_state(saved.config)).values
            pending_interrupts = [value for _, channel, value in saved.pending_writes or () if channel == INTERRUPT]
    if not isinstance(values, dict) or not values.get("messages"):
        raise TaskConflict("recovery_checkpoint_invalid")
    status = values.get("stop_status")
    answered = status == "complete" and values.get("stop_reason") == "answered"
    signals = list((values.get("operation_signals") or {}).values())
    has_pending = bool(_pending_calls(values["messages"]))
    unsettled = values.get("unknown_operations") or any(item.get("uncertain") for item in signals)
    pending_question = (values.get("host_state") or {}).get("pending_question")
    if unsettled or (question_resume is None and (pending_question or any(item.get("pending_question") for item in signals))):
        raise TaskConflict("recovery_requires_reconciliation")
    if discover_execution_wait:
        discovered = list(dict.fromkeys(wait for item in signals for wait in item.get("execution_waits", ())))
        if execution_wait is not None or not discovered or values.get("execution_wake"):
            raise TaskConflict("recovery_execution_wait_invalid")
        execution_wait = {"checkpoint_ref": saved.config["configurable"]["checkpoint_id"],
                          "wait_ids": discovered}
    if has_pending:
        if status or execution_wait is not None or pending_interrupts or values.get("execution_waits"):
            raise TaskConflict("recovery_requires_reconciliation")
        await committed_tool_receipts(identity, values["messages"])
    if question_resume is not None:
        from dataclasses import asdict
        if (execution_wait is not None or discover_execution_wait or has_pending or status
                or (values.get("host_state") or {}).get("identity") != asdict(identity)
                or values.get("execution_wake") or any(item.get("execution_waits") for item in signals)):
            raise TaskConflict("question_resume_checkpoint_conflict")
        if pending_interrupts:
            _verify_question_interrupt([pause for group in pending_interrupts for pause in group],
                                       saved.checkpoint["id"], question_resume)
        elif (pending_question or values.get("user_input_wake") != question_resume["value"]
              or (saved.parent_config or {}).get("configurable", {}).get("checkpoint_id") != question_resume["checkpoint_ref"]):
            raise TaskConflict("question_resume_checkpoint_conflict")
    elif execution_wait is not None:
        from dataclasses import asdict
        from .execution_outcome import validate_execution_suspension
        try:
            expected = validate_execution_suspension(
                {"identity": asdict(identity), **execution_wait}, identity)
            waits = validate_execution_waits(tuple(dict.fromkeys(wait for item in signals
                for wait in item.get("execution_waits", ()))))
        except (TypeError, ValueError):
            raise TaskConflict("recovery_execution_wait_invalid") from None
        interrupts = [interrupt for group in pending_interrupts for interrupt in group]
        wake = values.get("execution_wake")
        if (set(execution_wait) != {"checkpoint_ref", "wait_ids"}
                or (values.get("host_state") or {}).get("identity") != asdict(identity)
                or (not wake and (saved.config["configurable"]["checkpoint_id"] != expected["checkpoint_ref"]
                    or saved.checkpoint["id"] != expected["checkpoint_ref"]
                    or list(waits) != expected["wait_ids"] or len(interrupts) != 1
                    or interrupts[0].value != {"kind": "execution_wait", "wait_ids": list(waits)}))
                or (wake and (wake != expected["wait_ids"] or pending_interrupts))):
            raise TaskConflict("recovery_execution_wait_invalid")
    elif (status and not answered) or pending_interrupts or (not has_pending and has_unsettled_work(values)):
        raise TaskConflict("recovery_checkpoint_terminal")
    host_state = values.get("host_state")
    if not isinstance(host_state, dict):
        raise TaskConflict("recovery_original_request_missing")
    state = validate_original_request_state(host_state.get("original_request_state"), identity)
    from src.core.capability_policy import CapabilityPolicy
    snapshot = state.get("capability_snapshot")
    policy = CapabilityPolicy.restore(snapshot) if snapshot is not None else CapabilityPolicy()
    if options["context_scope"] != policy.history_key:
        raise TaskConflict("recovery_context_scope_mismatch")
    state["capability_snapshot"] = policy.snapshot()
    # Candidate bytes were process-local and are not present in the protected
    # snapshot. Never pretend those search handles survived a restart.
    pool_id = (state.get("candidate_pool") or {}).get("pool_id")
    if pool_id and question_resume is not None:
        state.pop("candidate_pool", None)
        state.pop("candidate_pool_notice", None)
    elif pool_id and not answered:
        from src.retrieval.candidate_pool import get_pool, prune_expired
        # The stored reference names the original pool, not whichever search
        # most recently published a pool in this run. Prune before the id lookup
        # (get_pool itself does not enforce TTL), then check all host boundaries.
        prune_expired()
        pool = get_pool(pool_id=pool_id) if execution_wait is not None else None
        if (pool is None or pool.owner_user_id != identity.owner_user_id
                or pool.session_id != identity.session_id or pool.turn_id != identity.run_id):
            raise TaskConflict("recovery_candidate_pool_unavailable")
    if question_resume is not None:
        state["agent_question_resume"] = question_resume
    if discover_execution_wait:
        state["agent_recovery_execution_wait"] = execution_wait
    state["agent_recovery_wake_committed"] = bool(execution_wait and values.get("execution_wake"))
    state["agent_resume"] = True
    state["messages"] = []  # Harness resumes with None; no new HumanMessage.
    state["answer"] = ""
    state["agent_recovery_answered"] = answered
    return state


def restore_deadline(state: dict, budget: dict) -> dict:
    """Translate the ORIGINAL wall deadline; never reuse pre-restart monotonic time."""
    wall_deadline = float(budget["deadline"])
    if math.isnan(wall_deadline) or wall_deadline == -math.inf:
        raise TaskConflict("recovery_budget_invalid")
    result = deepcopy(state)
    result["request_deadline"] = (math.inf if wall_deadline == math.inf else
                                  time.monotonic() + max(0.0, wall_deadline - time.time()))
    result["request_started_at"] = time.perf_counter()
    return result
