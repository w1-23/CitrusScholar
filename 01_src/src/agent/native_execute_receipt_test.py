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


@pytest.mark.parametrize("args, reason", [
    ({"command": " "}, "empty_command"),
    ({"command": "echo fixture", "timeout": -1}, "native_execute_rejected"),
])
def test_pinned_sdk_preflight_reaches_durable_not_run(scope, monkeypatch, args, reason):
    from types import SimpleNamespace
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from src.agent.sandbox_backend import CitrusSandboxBackend

    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    backend = CitrusSandboxBackend(scope.owner_user_id, scope.session_id, scope.run_id)
    tool = next(tool for tool in FilesystemMiddleware(backend=backend).tools if tool.name == "execute")
    call = {"id": "sdk-refusal", "args": args}

    async def invoke():
        return await tool.coroutine(**args, runtime=SimpleNamespace(tool_call_id=call["id"]))

    result = asyncio.run(dispatch_once(scope, call, invoke))
    assert result.status == "error"
    assert result.additional_kwargs["execution_dispatched"] is False
    assert result.additional_kwargs["operation_uncertain"] is False
    assert result.artifact["_tool_outcome"] == {"status": "not_run", "reason_code": reason}
    with task_events.transaction() as conn:
        [row] = conn.execute("SELECT status FROM task_operations WHERE job_id=?", (scope.run_id,))
    assert row["status"] == "not_run"


def test_backend_exception_is_not_relabelled_not_run(scope, monkeypatch):
    from types import SimpleNamespace
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from src.agent.sandbox_backend import CitrusSandboxBackend

    def lose_observation(*args, **kwargs):
        raise OSError("not for model output")

    monkeypatch.setattr(execution_bridge, "execute", lose_observation)
    backend = CitrusSandboxBackend(scope.owner_user_id, scope.session_id, scope.run_id)
    tool = next(tool for tool in FilesystemMiddleware(backend=backend).tools if tool.name == "execute")
    call = {"id": "backend-unconfirmed", "args": {"command": "echo fixture"}}

    async def invoke():
        return await tool.coroutine(**call["args"], runtime=SimpleNamespace(tool_call_id=call["id"]))

    result = asyncio.run(dispatch_once(scope, call, invoke))
    assert result.artifact["_tool_outcome"]["status"] == "unknown"
    assert result.additional_kwargs["operation_uncertain"] is True
    assert result.additional_kwargs.get("execution_dispatched") is not False
    assert "could not be confirmed" in result.content
    assert "not for model output" not in result.content
