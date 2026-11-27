"""Exercise the real SDK compactor against the versioned Workspace and ledger."""
import asyncio

from deepagents.backends import CompositeBackend
from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from src.agent.context_compaction import MeteredContextCompaction
from src.agent.subagent_runtime_test import _StaticModel
from src.agent.workspace_backend_test import backend_for, scope  # fixture
from src.core import task_events
from src.core.request_context import MainModelBudget


def test_native_compaction_keeps_user_and_original_messages_and_charges_summary(scope):
    files = backend_for(scope)
    backend = CompositeBackend(default=files, routes={}, artifacts_root="/workspace")
    model = _StaticModel(reply="a short historical summary")
    budget = MainModelBudget(512, 30_000, "fixture", 4000, 30_000, 0)
    compaction = MeteredContextCompaction(model, identity=scope, budget=budget, backend=backend)
    old = [message for i in range(16) for message in (
        HumanMessage(id=f"question-{i}", content=f"Prior question {i} " + "historical context " * 80),
        AIMessage(id=f"answer-{i}", content=f"Prior answer {i} " + "past observations " * 80))]
    current = HumanMessage(id="current", content="Preserve this exact instruction.",
                           additional_kwargs=scope.message_metadata("user"))
    messages = [*old, current]
    observed = []

    async def send(request):
        observed.extend(request.messages)
        return ModelResponse(result=[AIMessage(content="final")])

    request = ModelRequest(model=model, messages=messages, state={"messages": messages}, tools=[])
    response = asyncio.run(compaction.awrap_model_call(request, send))
    assert observed[-1] == current and len(observed) < len(messages)
    assert "a short historical summary" in observed[0].content
    assert request.state["messages"] == [*old, current]
    summary = response.command.update[SUMMARIZATION_EVENT_KEY]
    assert summary["cutoff_index"] > 0
    stored, ref = files.storage.text(summary["file_path"])
    assert "Prior question 0" in stored and "Preserve this exact instruction." not in stored
    assert len(ref["sha256"]) == 64
    with task_events.transaction() as conn:
        [row] = conn.execute("SELECT purpose FROM task_model_attempts WHERE job_id=?", (scope.run_id,))
    assert row["purpose"] == "conversation_summary"


def test_cutoff_cannot_evict_earliest_active_instruction(scope):
    model = _StaticModel()
    budget = MainModelBudget(512, 30_000, "fixture", 4000, 30_000, 0)
    compaction = MeteredContextCompaction(model, identity=scope, budget=budget, backend=backend_for(scope))
    current = HumanMessage(content="Keep me", additional_kwargs=scope.message_metadata("user"))
    assert compaction._determine_cutoff_index([current, *[AIMessage(content="x")] * 50]) == 0


def test_real_sdk_offloads_tool_text_and_reuses_the_same_versioned_view(scope):
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents import create_agent
    from langchain_core.messages import ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    data = "begin fixture\n" + "0123456789 sample line\n" * 5000 + "end fixture"
    files = backend_for(scope)
    backend = CompositeBackend(default=files, routes={}, artifacts_root="/workspace")

    @tool
    def fixture_report() -> str:
        """Return a known large text observation."""
        return data

    class OffloadModel(_StaticModel):
        async def _agenerate(self, messages, **kwargs):
            if any(isinstance(m, ToolMessage) for m in messages):
                receipt = messages[-1]
                assert len(receipt.content) < 10_000
                assert "/workspace/large_tool_results/call-fixture" in receipt.content
                reply = AIMessage(content="stored")
            else:
                reply = AIMessage(content="", tool_calls=[{
                    "name": "fixture_report", "args": {}, "id": "call-fixture"}])
            return ChatResult(generations=[ChatGeneration(message=reply)])

    middleware = FilesystemMiddleware(backend=backend, tools=["read_file"],
        tool_token_limit_before_evict=20000, human_message_token_limit_before_evict=None)
    graph = create_agent(model=OffloadModel(), tools=[fixture_report], middleware=[middleware])
    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="read fixture")]}))
    assert result["messages"][-1].content == "stored"
    path = "/workspace/large_tool_results/call-fixture"
    raw, ref = files.storage.text(path)
    assert raw == data and ref["version"] == 1 and len(ref["sha256"]) == 64
    assert files.read(path, 0, 1).file_data["content"] == "begin fixture\n"
