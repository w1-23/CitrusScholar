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


@pytest.mark.parametrize("scenario", ["valid", "duplicate", "wrong_owner", "queue_failure", "private"])
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
    events, notes = [], []
    def publish_progress(event, data):
        assert event == 'assistant_progress'
        notes.append(project_public_event(event, data))
    monkeypatch.setattr(progress_bus, "emit_progress", publish_progress)

    def publish(stage, **data):
        if scenario == "queue_failure":
            raise OSError("synthetic storage failure")
        events.append(project_public_event("status", {"stage": stage, **data}))
    monkeypatch.setattr(progress_bus, "emit_status", publish)
    public_title = "核对两种实验方法"
    todos = [{"content": "private-plan-text", "status": "pending"},
             {"content": "公开步骤：" + public_title if scenario != "private" else "Private active text",
              "status": "in_progress"}]
    calls = [{"id": "todo-1", "name": NATIVE_PLAN_TOOL, "args": {"todos": todos}}]
    if scenario == "duplicate":
        calls.append({**calls[0], "id": "todo-2"})

    counter = 0

    async def model(history, current, *, native_schemas):
        nonlocal counter
        native = next(s["function"] for s in native_schemas if s["function"]["name"] == NATIVE_PLAN_TOOL)
        assert "公开步骤：" in native["description"]
        assert "update_task_plan" not in {schema["function"]["name"] for schema in native_schemas}
        counter += 1
        if counter == 1:
            return AIMessage(content="Unpublished model draft", tool_calls=calls,
                             additional_kwargs={"reasoning_content": "Unpublished reasoning"})
        if counter == 2:
            bound = [m for m in history if isinstance(m, ToolMessage)]
            assert len(bound) == len(calls)
            assert all(m.status == ("error" if scenario == "duplicate" else "success") for m in bound)
            if scenario == "valid":
                assert [note["text"] for note in notes] == [public_title]
                return AIMessage(content="", tool_calls=[{**calls[0], "id": "todo-repeated"}])
        if scenario == "valid" and counter == 3:
            return AIMessage(content="", tool_calls=[{**calls[0], "id": "todo-finished",
                "args": {"todos": [{**item, "status": "completed"} for item in todos]}}])
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
        assert result.status == "complete" and result.messages[-1].content == "Done"
        assert counter == (4 if scenario == "valid" else 2)
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
    expected_todos = [] if scenario == "duplicate" else todos
    if scenario == "valid":
        expected_todos = [{**item, "status": "completed"} for item in todos]
    assert stored_todos == expected_todos
    assert len(events) == (3 if scenario == "valid" else 1 if scenario == "private" else 0)
    assert [note["status"] for note in notes] == (
        ["in_progress", "inactive"] if scenario == "valid" else ["in_progress"] if scenario == "queue_failure" else [])
    if notes:
        assert notes[0]["text"] == public_title and notes[0]["job_id"] == identity.run_id
        if notes[-1]["status"] == "inactive":
            assert "text" not in notes[-1]
    assert not any(value in json.dumps(notes) for value in ("private-plan-text", "Unpublished", "ToolMessage"))
    if events:
        assert events[0]["plan_id"] == identity.run_id
        assert events[0]["steps"][1]["status"] == "in_progress"
        assert set(events[0]["steps"][1]) == {"id", "status"}
        assert "private-plan-text" not in json.dumps(events)
    assert "private-plan-text" not in repr(_settled_trace(result.messages, None, result))
    proof = completion_from_answer("Done", [
        {"name": NATIVE_PLAN_TOOL, "tool_call_id": "todo-1", "status": "complete"},
    ])
    assert proof.goals[0].evidence_call_ids == []


@pytest.mark.parametrize("unsafe", [
    "Private unmarked planning", "公开步骤：<think>internal</think>", "公开步骤：Read /tmp/trace",
    "公开步骤：Send to jane@example.test", "公开步骤：我需要先确认私有数据", "公开步骤：Inspect 123456789",
    "公开步骤：Read confidential\nresult", "公开步骤：" + "核" * 81, "公开步骤：{\"content\": \"raw\"}",
    "公开步骤：Inspect alpha methods", "公开步骤：Alpha methods",
])
def test_public_todo_title_rejects_private_or_copied_text(unsafe):
    from src.core.public_progress import public_todo_title

    assert public_todo_title([{"content": unsafe, "status": "in_progress"}], protected_text=(
        "Inspect alpha methods", "Please compare alpha methods now")) == ""
    assert public_todo_title([{"content": "公开步骤：核对两个实验方法", "status": "in_progress"}]) == "核对两个实验方法"


def test_official_custom_stream_drops_untyped_payload_without_changing_final_value(monkeypatch):
    from langgraph.config import get_stream_writer
    from langgraph.graph import START, END, StateGraph
    from typing_extensions import TypedDict
    from src.core import progress_bus
    from src.core.public_progress import invoke_with_public_progress

    class State(TypedDict):
        status: str

    async def node(state):
        get_stream_writer()({"origin": "langgraph", "source": "write_todos", "visibility": "public",
            "status": "in_progress", "job_id": "job", "note_id": "todo:" + "a" * 64,
            "text": "Untrusted synthetic content"})
        return {"status": "complete"}

    graph = (StateGraph(State).add_node("node", node).add_edge(START, "node").add_edge("node", END)
             .compile(checkpointer=InMemorySaver()))
    monkeypatch.setattr(progress_bus, "emit_progress", lambda *_: pytest.fail("untyped custom payload escaped"))
    result = asyncio.run(invoke_with_public_progress(graph, {"status": "pending"},
        {"configurable": {"thread_id": "job"}}, context=None, job_id="job"))
    assert result.value == {"status": "complete"} and not result.interrupts
