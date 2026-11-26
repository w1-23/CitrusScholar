"""Durable dispatch fence for SDK execute. No automatic replay of shell work."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.messages import ToolMessage

from src.core import task_events
from .batch_journal import persistent_message
from .runtime import AdmissionStopped


@dataclass
class BackendEvidence:
    # asyncio.to_thread in the pinned SDK copies the context, sharing this
    # call-owned object with the awaiting receipt guard.
    entered: bool = False
    not_run_reason: str = ""


backend_evidence: ContextVar[BackendEvidence | None] = ContextVar(
    "native_execute_backend_evidence", default=None)


def _rejected(call_id, reason, detail):
    return ToolMessage(content=f"{detail} This command was not run.",
        name="execute", tool_call_id=call_id, status="error",
        additional_kwargs={"execution_dispatched": False},
        artifact={"_tool_outcome": {"status": "not_run", "reason_code": reason}})


async def dispatch_once(identity, call, invoke):
    """Fence a native call before the SDK can submit it to the isolated Worker."""
    from src.code_execution.execution_bridge import sandbox_ready

    try:
        ready = sandbox_ready()
    except Exception:
        ready = False
    if not ready:
        return _rejected(call["id"], "code_worker_unavailable", "Isolated Worker unavailable.")
    try:
        args_digest = task_events.input_hash(call["args"])
    except (TypeError, ValueError):
        return _rejected(call["id"], "native_execute_rejected", "Invalid execute arguments.")
    try:
        operation, lease = await asyncio.to_thread(task_events.start_operation,
            identity.run_id, identity.owner_user_id, "execute", {"args_sha256": args_digest},
            tool_call_id=call["id"], logical_key="native_execute:" + args_digest,
            lease_seconds=1000)
    except task_events.TaskConflict as exc:
        raise AdmissionStopped(str(exc)) from exc

    evidence = BackendEvidence()
    token = backend_evidence.set(evidence)
    try:
        result = await invoke()
    finally:
        backend_evidence.reset(token)
    if not isinstance(result, ToolMessage) or result.tool_call_id != call["id"]:
        raise ValueError("native_execute_observation_mismatch")
    receipt = dict(result.artifact) if isinstance(result.artifact, dict) else {}
    code = receipt.get("exit_code")
    if type(code) is int:
        status, reason = ("complete", "") if code == 0 else ("failed", "native_execute_exit")
    elif evidence.not_run_reason:
        status, reason = "not_run", evidence.not_run_reason
    elif not evidence.entered and result.status == "error" and result.artifact is None:
        # The pinned SDK reserves this pairing for a validation/unsupported-
        # backend refusal. Once the backend was entered, only its proof counts.
        status, reason = "not_run", "native_execute_rejected"
    else:
        status, reason = "unknown", "native_execute_unconfirmed"
    receipt["_tool_outcome"] = {"status": status, "reason_code": reason}
    if type(code) is int:
        receipt["_tool_outcome"]["facts"] = {"exit_code": code}
    metadata = {**result.additional_kwargs, "operation_id": operation,
                "operation_uncertain": status == "unknown"}
    if status == "not_run":
        metadata["execution_dispatched"] = False
    result = persistent_message(result.model_copy(update={"name": "execute",
        "artifact": receipt, "additional_kwargs": metadata,
        "status": "error" if status in {"not_run", "unknown"} else result.status}))
    await asyncio.to_thread(task_events.finish_operation, operation, lease, result)
    return result
