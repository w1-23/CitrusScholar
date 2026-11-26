"""Native `task` subagent: explicit, file-only, metered on the shared budget."""
import asyncio
import time

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from src.agent.deep_agent_runtime import FILE_MIDDLEWARE_TOOLS
from src.agent.identity import ExecutionIdentity
from src.agent.subagent_runtime import MeteredSubagentModel, general_purpose_subagent
from src.core import jobs, task_events, tracing


class _StaticModel(BaseChatModel):
    calls: list = Field(default_factory=list)
    reply: str = "subagent summary"

    @property
    def _llm_type(self):
        return "fixture-subagent"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise AssertionError("asynchronous fixture required")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(tuple(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


@pytest.fixture
def identity(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "ledger.sqlite3")
    claim = jobs.claim_job("subagent-session", "request", owner_user_id=7, payload={})
    tracing.set_user_id(7)
    tracing.set_session_id("subagent-session")
    tracing.set_job_id(claim.job_id)
    return ExecutionIdentity(7, "subagent-session", "request", claim.job_id)


def _attempts(job_id):
    with task_events.transaction() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT purpose,status,usage FROM task_model_attempts WHERE job_id=?", (job_id,))]


def test_metered_subagent_model_charges_the_same_job_budget(identity):
    task_events.init_budget(identity.run_id, time.time() + 60, max_calls=4, max_tokens=100_000)
    inner = _StaticModel()
    proxy = MeteredSubagentModel(inner=inner, identity=identity)

    async def scenario():
        reply = await proxy.ainvoke([HumanMessage("summarize the workspace")])
        return reply

    reply = asyncio.run(scenario())
    assert reply.content == "subagent summary"
    assert len(inner.calls) == 1
    purposes = {row["purpose"] for row in _attempts(identity.run_id)}
    assert purposes == {"subagent_loop"}
    with task_events.transaction() as conn:
        calls = conn.execute("SELECT calls FROM task_budgets WHERE job_id=?", (identity.run_id,)).fetchone()[0]
    assert calls == 1


def test_budget_exhaustion_stops_the_subagent_model(identity):
    task_events.init_budget(identity.run_id, time.time() + 60, max_calls=1, max_tokens=100_000)
    proxy = MeteredSubagentModel(inner=_StaticModel(), identity=identity)

    async def scenario():
        await proxy.ainvoke([HumanMessage("first")])
        with pytest.raises(task_events.TaskConflict):
            await proxy.ainvoke([HumanMessage("second")])

    asyncio.run(scenario())


def test_subagent_spec_is_file_only_and_restricted(identity):
    from deepagents.middleware.filesystem import FilesystemMiddleware

    spec = general_purpose_subagent(model=_StaticModel(), identity=identity, backend=object(),
                                    permissions=[], file_tools=FILE_MIDDLEWARE_TOOLS)
    assert spec["name"] == "general-purpose"
    # No host capability tools are handed to the subagent.
    assert list(spec["tools"]) == []
    filesystem = spec["middleware"][0]
    assert isinstance(filesystem, FilesystemMiddleware)
    expected = tuple(name for name in FILE_MIDDLEWARE_TOOLS if name != "execute")
    assert sorted(tool.name for tool in filesystem.tools) == sorted(expected)
    assert "execute" not in {tool.name for tool in filesystem.tools}
    assert isinstance(spec["model"], MeteredSubagentModel)


def test_graph_exposes_task_only_with_an_explicit_subagent(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import ToolNode
    from src.agent.deep_agent_runtime import create_framework_agent
    from src.agent.framework_test_support import ScriptedModel
    from src.agent.harness import HarnessState

    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "ledger.sqlite3")
    claim = jobs.claim_job("graph-session", "request", owner_user_id=7, payload={})
    identity = ExecutionIdentity(7, "graph-session", "request", claim.job_id)

    async def script(*args, **kwargs):
        raise AssertionError("model must not be called")

    def names(graph):
        return {name for node in graph.get_graph().nodes.values() if isinstance(node.data, ToolNode)
                for name in node.data.tools_by_name}

    with_task = create_framework_agent(
        ScriptedModel(script=script), [], [], InMemorySaver(), HarnessState,
        workspace_scope=(7, "graph-session", claim.job_id), identity=identity)
    without_task = create_framework_agent(
        ScriptedModel(script=script), [], [], InMemorySaver(), HarnessState,
        workspace_scope=(7, "graph-session", claim.job_id), identity=identity, task_subagent=False)
    assert "task" in names(with_task)
    assert "task" not in names(without_task)
    # The sandbox backend always supplies the native execute tool; availability
    # is a runtime not_run, not a disappearing schema.
    assert "execute" in names(with_task)



def test_real_task_round_trip_uses_the_same_ledger_and_emits_one_paired_result(identity, monkeypatch):
    from langchain_core.messages import ToolMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langgraph.checkpoint.memory import InMemorySaver
    from src.agent.harness import HarnessRuntime
    from src.agent.runtime import FinalCheck, RunLimits
    from src.core import progress_bus
    from src.core.request_context import MainModelBudget

    frames = []
    monkeypatch.setattr(progress_bus, "emit_progress", lambda kind, event: frames.append((kind, event)))

    class TaskModel(_StaticModel):
        def bind_tools(self, tools, **kwargs):
            return self.bind(tools=[convert_to_openai_tool(item) for item in tools], **kwargs)

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls.append((messages, kwargs))
            last = messages[-1]
            if isinstance(last, ToolMessage):
                assert last.name == "task" and last.tool_call_id == "delegate-1"
                assert last.content == "verified child summary"
                assert last.artifact is None  # private SDK namespace never becomes a receipt
                reply = AIMessage(content="final answer")
            elif "child-only instruction" in last.content:
                exposed = {tool["function"]["name"] for tool in kwargs["tools"]}
                assert {"read_file", "write_file"} <= exposed
                assert not {"task", "execute", "code_execute", "publish_shared_corpus"} & exposed
                assert kwargs["max_tokens"] == 512
                reply = AIMessage(content="verified child summary")
            else:
                reply = AIMessage(content="", tool_calls=[{"id": "delegate-1", "name": "task",
                    "args": {"subagent_type": "general-purpose", "description": "child-only instruction"}}])
            return ChatResult(generations=[ChatGeneration(message=reply)])

    async def unexpected_dispatch(*args):
        raise AssertionError("native task reached host tool dispatch")

    async def admit(*args):
        pass

    async def accept(*args):
        return FinalCheck()

    task_events.init_budget(identity.run_id, time.time() + 60, max_calls=6, max_tokens=100_000)
    model = TaskModel()
    runtime = HarnessRuntime(model, unexpected_dispatch, admit, accept, RunLimits(), [])
    budget = MainModelBudget(512, 30_000, "fixture", 22_000, 30_000, 0)
    user = HumanMessage(content="Parent request", additional_kwargs=identity.message_metadata("user"))
    result = asyncio.run(runtime.run(identity, [user], saver=InMemorySaver(),
                                     agent_options={"model_budget": budget}))
    assert result.status == "complete"
    assert len(model.calls) == 3
    assert [row["purpose"] for row in _attempts(identity.run_id)] == ["subagent_loop"]
    lifecycle = [event for kind, event in frames if kind == "assistant_progress"
                 and event.get("kind", "").startswith("subagent_")]
    assert [event["kind"] for event in lifecycle] == ["subagent_started", "subagent_completed"]
    assert lifecycle[0]["subagent_id"] == lifecycle[1]["subagent_id"]
