"""Durable dispatch fence for SDK execute. No automatic replay of shell work."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

from langchain_core.messages import ToolMessage

from src.core import task_events
from .batch_journal import persistent_message
from .identity import ExecutionIdentity
from .runtime import AdmissionStopped


@dataclass
class BackendEvidence:
    # asyncio.to_thread in the pinned SDK copies the context, sharing this
    # call-owned object with the awaiting receipt guard.
    identity: ExecutionIdentity
    operation_id: str
    entered: bool = False
    not_run_reason: str = ""
    worker_execution_id: str = ""
    _lock: Lock = field(default_factory=Lock, repr=False)

    def enter(self, owner, session, run_id):
        scope = self.identity
        with self._lock:
            if self.entered:
                return "native_execute_unconfirmed"
            if (owner, session, run_id) != (scope.owner_user_id, scope.session_id, scope.run_id):
                self.not_run_reason = "execution_scope_unavailable"
                return self.not_run_reason
            self.entered = True
            self.not_run_reason = ""
        return ""

    def accept(self, execution_id):
        self.worker_execution_id = execution_id


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

    # Reserve before another readiness check so a replay cannot disguise
    # an older dispatch as a fresh, known-not-run operation.
    try:
        ready = sandbox_ready()
    except Exception:
        ready = False
    if not ready:
        result = _rejected(call["id"], "code_worker_unavailable", "Isolated Worker unavailable.")
        result.additional_kwargs.update(operation_id=operation, operation_uncertain=False)
        await asyncio.to_thread(task_events.finish_operation, operation, lease, result)
        return result

    evidence = BackendEvidence(identity, operation)
    token = backend_evidence.set(evidence)
    try:
        try:
            result = await invoke()
        except AdmissionStopped:
            raise
        except Exception:
            if not evidence.entered:
                raise
            # The SDK may lose its reply after the backend ran. Keep the
            # original durable operation and never invoke the command again.
            result = ToolMessage(content="The command's execution status could not be confirmed.",
                                 tool_call_id=call["id"], status="error")
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
    facts = {"exit_code": code} if type(code) is int else {}
    if evidence.worker_execution_id:
        facts["worker_execution_id"] = evidence.worker_execution_id
    if facts:
        receipt["_tool_outcome"]["facts"] = facts
    metadata = {**result.additional_kwargs, "operation_id": operation,
                "operation_uncertain": status == "unknown"}
    if status == "not_run":
        metadata["execution_dispatched"] = False
    result = persistent_message(result.model_copy(update={"name": "execute",
        "artifact": receipt, "additional_kwargs": metadata,
        "status": "error" if status in {"not_run", "unknown"} else result.status}))
    await asyncio.to_thread(task_events.finish_operation, operation, lease, result)
    return result
