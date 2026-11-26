"""A native execute call cannot be redispatched across a recovery boundary."""
import asyncio

import pytest
from langchain_core.messages import ToolMessage

from src.agent.native_execute_receipt import dispatch_once
from src.agent.runtime import AdmissionStopped
from src.agent.workspace_backend_test import scope  # shared DB-backed fixture
from src.core import task_events
from src.code_execution import execution_bridge


@pytest.fixture(autouse=True)
def ready_worker(monkeypatch):
    monkeypatch.setattr(execution_bridge, "sandbox_ready", lambda: True)


def test_shell_dispatch_records_exit_without_storing_command_and_never_repeats(scope):
    call = {"id": "call-one", "args": {"command": "private input"}}
    count = 0

    async def invoke():
        nonlocal count
        count += 1
        return ToolMessage(content="worker output", tool_call_id=call["id"], artifact={"exit_code": 0})

    async def run():
        receipt = await dispatch_once(scope, call, invoke)
        assert receipt.artifact["_tool_outcome"]["facts"] == {"exit_code": 0}
        with pytest.raises(AdmissionStopped, match="recovery_requires_reconciliation"):
            await dispatch_once(scope, call, invoke)

    asyncio.run(run())
    assert count == 1
    with task_events.transaction() as conn:
        [row] = conn.execute("SELECT status,inputs FROM task_operations WHERE job_id=?", (scope.run_id,))
    assert row["status"] == "complete"
    assert "private input" not in row["inputs"]


def test_missing_worker_evidence_does_not_become_success(scope):
    call = {"id": "call-two", "args": {"command": "fixture"}}

    async def invoke():
        return ToolMessage(content="possibly ran", tool_call_id=call["id"])

    receipt = asyncio.run(dispatch_once(scope, call, invoke))
    assert receipt.additional_kwargs["operation_uncertain"] is True
    assert receipt.artifact["_tool_outcome"]["status"] == "unknown"


def test_unavailable_worker_is_not_run(scope, monkeypatch):
    monkeypatch.setattr(execution_bridge, "sandbox_ready", lambda: False)

    async def invoke():
        raise AssertionError("disabled execute cannot dispatch")

    call = {"id": "call-three", "args": {"command": "fixture"}}
    receipt = asyncio.run(dispatch_once(scope, call, invoke))
    assert receipt.additional_kwargs["execution_dispatched"] is False
    assert receipt.artifact["_tool_outcome"]["status"] == "not_run"
