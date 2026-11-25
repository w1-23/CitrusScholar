"""Official todo execution, owner-bound publication and trace privacy."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.agent.harness import HarnessRuntime
from src.agent.framework_test_support import ScriptedModel
from src.agent.host import _settled_trace
from src.agent.identity import ExecutionIdentity
from src.agent.plan_tools import NATIVE_PLAN_TOOL
from src.agent.runtime import FinalCheck, RunLimits


@pytest.mark.parametrize("scenario", ["valid", "duplicate", "wrong_owner", "queue_failure"])
def test_native_todo_public_projection(monkeypatch, scenario):
    from src.core import jobs, progress_bus, tracing
    from src.core.public_delivery import project_public_event
    from src.core.turn_outcome import completion_from_answer

    identity = ExecutionIdentity(42, "session", "turn", "job")
    owner = 7 if scenario == "wrong_owner" else identity.owner_user_id
    for key, value in (("user_id", owner), ("session_id", identity.session_id),
                       ("request_id", identity.user_turn_id), ("job_id", identity.run_id)):
        monkeypatch.setattr(tracing, f"get_{key}", lambda value=value: value)
    monkeypatch.setattr(jobs, "get_job", lambda job_id: {
        "owner_user_id": identity.owner_user_id, "session_id": identity.session_id,
        "request_id": identity.user_turn_id, "status": "running",
    })
    events = []
    def publish(stage, **data):
        if scenario == "queue_failure":
            raise OSError("synthetic storage failure")
        events.append(project_public_event("status", {"stage": stage, **data}))
    monkeypatch.setattr(progress_bus, "emit_status", publish)
    todos = [{"content": "private-plan-text", "status": "in_progress"}]
    calls = [{"id": "todo-1", "name": NATIVE_PLAN_TOOL, "args": {"todos": todos}}]
    if scenario == "duplicate":
        calls.append({**calls[0], "id": "todo-2"})

    counter = 0

    async def model(history, current, *, native_schemas):
        nonlocal counter
        assert NATIVE_PLAN_TOOL in {schema["function"]["name"] for schema in native_schemas}
        assert "update_task_plan" not in {schema["function"]["name"] for schema in native_schemas}
        counter += 1
        if counter == 1:
            return AIMessage(content="", tool_calls=calls)
        if counter == 2:
            bound = [m for m in history if isinstance(m, ToolMessage)]
            assert len(bound) == len(calls)
            assert all(m.status == ("error" if scenario == "duplicate" else "success") for m in bound)
        return AIMessage(content="Done")

    async def final_check(*args):
        return FinalCheck()

    async def not_host(*args):
        pytest.fail("Native todo was sent to the business registry")

    async def run():
        saver = InMemorySaver()
        runtime = HarnessRuntime(ScriptedModel(script=model), not_host, lambda *_: None, final_check, RunLimits(), [])
        config = {"thread_id": "test-native-todo"}
        result = await runtime.run(identity, [HumanMessage(content="Question")], saver=saver,
                                   checkpoint_config=config)
        stored = await saver.aget_tuple({"configurable": config})
        assert result.status == "complete"
        if scenario == "valid":
            later = ExecutionIdentity(identity.owner_user_id, identity.session_id, "turn-2", "job-2")
            monkeypatch.setattr(tracing, "get_request_id", lambda: later.user_turn_id)
            monkeypatch.setattr(tracing, "get_job_id", lambda: later.run_id)
            following = await runtime.run(later, [HumanMessage(content="Another question")],
                                          saver=saver, checkpoint_config=config)
            following_state = await saver.aget_tuple({"configurable": config})
            assert following.status == "complete"
            assert following_state.checkpoint["channel_values"]["todos"] == []
        return result, stored.checkpoint["channel_values"].get("todos")

    result, stored_todos = asyncio.run(run())
    assert stored_todos == ([] if scenario == "duplicate" else todos)
    assert len(events) == (1 if scenario == "valid" else 0)
    if events:
        assert events[0]["plan_id"] == identity.run_id
        assert events[0]["steps"][0]["title"] == "任务步骤 1"
        assert events[0]["steps"][0]["status"] == "in_progress"
        assert "private-plan-text" not in json.dumps(events)
    assert "private-plan-text" not in repr(_settled_trace(result.messages, None, result))
    proof = completion_from_answer("Done", [
        {"name": NATIVE_PLAN_TOOL, "tool_call_id": "todo-1", "status": "complete"},
    ])
    assert proof.goals[0].evidence_call_ids == []
