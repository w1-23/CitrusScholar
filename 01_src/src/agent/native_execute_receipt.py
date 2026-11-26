"""Durable dispatch fence for SDK execute. No automatic replay of shell work."""
import asyncio

from langchain_core.messages import ToolMessage

from src.core import task_events
from .batch_journal import persistent_message
from .runtime import AdmissionStopped


async def dispatch_once(identity, call, invoke):
    """Fence a native call before the SDK can submit it to the isolated Worker."""
    from src.code_execution.execution_bridge import sandbox_ready

    if not sandbox_ready():
        return ToolMessage(content="Isolated Worker unavailable. This command was not run.",
            name="execute", tool_call_id=call["id"], status="error",
            additional_kwargs={"execution_dispatched": False},
            artifact={"_tool_outcome": {"status": "not_run", "reason_code": "code_worker_unavailable"}})
    args_digest = task_events.input_hash(call["args"])
    try:
        operation, lease = await asyncio.to_thread(task_events.start_operation,
            identity.run_id, identity.owner_user_id, "execute", {"args_sha256": args_digest},
            tool_call_id=call["id"], logical_key="native_execute:" + args_digest,
            lease_seconds=1000)
    except task_events.TaskConflict as exc:
        raise AdmissionStopped(str(exc)) from exc

    result = await invoke()
    if not isinstance(result, ToolMessage) or result.tool_call_id != call["id"]:
        raise ValueError("native_execute_observation_mismatch")
    receipt = dict(result.artifact) if isinstance(result.artifact, dict) else {}
    code = receipt.get("exit_code")
    # SDK absence of an exit code cannot distinguish an early refusal from
    # disrupted execution. Never infer successful work from a string result.
    status = "complete" if type(code) is int and code == 0 else (
        "failed" if type(code) is int else "unknown")
    reason = "" if status == "complete" else (
        "native_execute_exit" if status == "failed" else "native_execute_unconfirmed")
    receipt["_tool_outcome"] = {"status": status, "reason_code": reason}
    if type(code) is int:
        receipt["_tool_outcome"]["facts"] = {"exit_code": code}
    metadata = {**result.additional_kwargs, "operation_id": operation,
                "operation_uncertain": status == "unknown"}
    result = persistent_message(result.model_copy(update={"name": "execute",
        "artifact": receipt, "additional_kwargs": metadata}))
    await asyncio.to_thread(task_events.finish_operation, operation, lease, result)
    return result
