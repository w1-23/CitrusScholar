"""Offline proof of retry behavior at the installed LangChain boundary."""
import asyncio
from importlib.metadata import version
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, ModelRetryMiddleware, ToolRetryMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from openai import APIConnectionError

from src.agent.capabilities import CapabilityCatalog
from src.agent.framework_test_support import ScriptedModel
from src.agent.harness import HarnessRuntime, HarnessState, _RunContext
from src.agent.identity import ExecutionIdentity
from src.agent.model import ModelDriver
from src.agent.runtime import AdmissionStopped, RunLimits
from src.core import jobs, request_context, task_events, tracing


def _is_pre_send_connect_error(exc):
    # The pinned integrations wrap ConnectError twice before agent middleware.
    cause = exc.__cause__
    return (isinstance(exc, APIConnectionError) and isinstance(cause, APIConnectionError)
            and isinstance(cause.__cause__, httpx.ConnectError))


def _model_retry(**kwargs):
    return ModelRetryMiddleware(max_retries=1, jitter=False, on_failure="error",
        retry_on=_is_pre_send_connect_error,
        **kwargs)


@pytest.mark.parametrize("mode", ["single", "retry", "budget", "deadline", "inside_meter", "unknown_send"])
def test_official_model_retry_must_wrap_each_metered_send(tmp_path, monkeypatch, mode):
    assert version("langchain") == "1.5.0"
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "ledger.sqlite3")
    claim = jobs.claim_job("session", "request", owner_user_id=7, payload={})
    task_events.init_budget(claim.job_id, time.time() + 30, max_calls=1 if mode == "budget" else 2)
    identity = ExecutionIdentity(7, "session", "request", claim.job_id)
    logical = "fixture-logical-call"
    sent = []
    history = [HumanMessage("fixture", additional_kwargs=identity.message_metadata("user"))]

    async def wire(request):
        scope = request_context._observation.get()
        sent.append((scope["attempt_id"], scope["logical_call_id"], json.loads(request.content)))
        if len(sent) == 1 and mode == "unknown_send":
            raise httpx.ReadError("offline unknown-dispatch fixture", request=request)
        if len(sent) == 1:
            raise httpx.ConnectError("offline pre-send fixture", request=request)
        chunk = {"id": "fixture", "model": "fixture", "created": 1, "object": "chat.completion.chunk"}
        rows = [
            {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"},
                                   "finish_reason": "stop"}]},
            {**chunk, "choices": [], "usage": {"prompt_tokens": 60, "completion_tokens": 5, "total_tokens": 65}},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(row)}\n\n" for row in rows) + "data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    class Meter(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            return await ModelDriver(CapabilityCatalog())(request, handler, identity, allowed=(), host_tools=())

    async def exercise():
        saved = tracing.get_job_id()
        tracing.set_job_id(claim.job_id)
        group = request_context._logical_call.set(logical)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(wire),
                    event_hooks={"request": [request_context.observe_async_provider_request]}) as client:
                model = ChatOpenAI(model="fixture", base_url="https://test.invalid/v1", api_key="fixture",
                    http_async_client=client, max_tokens=64, max_retries=0, stream_usage=True)
                retry = _model_retry(initial_delay=10 if mode == "deadline" else 0)
                chain = ([Meter()] if mode == "single" else [Meter(), retry] if mode == "inside_meter"
                         else [retry, Meter()])
                graph = create_agent(model, middleware=chain)
                run = graph.ainvoke({"messages": history})
                if mode == "retry":
                    assert (await run)["messages"][-1].content == "done"
                elif mode == "budget":
                    with pytest.raises(task_events.TaskConflict, match="model_budget_exhausted:calls"):
                        await run
                elif mode == "deadline":
                    with pytest.raises(TimeoutError):
                        async with asyncio.timeout(2):
                            await run
                elif mode == "inside_meter":
                    with pytest.raises(AdmissionStopped, match="duplicate_provider_request"):
                        await run
                else:
                    with pytest.raises(APIConnectionError) as caught:
                        await run
                    assert _is_pre_send_connect_error(caught.value) == (mode == "single")
                    if mode == "unknown_send":
                        assert isinstance(caught.value.__cause__.__cause__, httpx.ReadError)
        finally:
            request_context._logical_call.reset(group)
            tracing.set_job_id(saved)

    asyncio.run(exercise())
    with task_events.transaction() as conn:
        rows = conn.execute("SELECT * FROM task_model_attempts WHERE job_id=? ORDER BY created_at", (claim.job_id,)).fetchall()
        budget = conn.execute("SELECT calls,tokens FROM task_budgets WHERE job_id=?", (claim.job_id,)).fetchone()
    assert len(sent) == len(rows) == budget["calls"] == (2 if mode == "retry" else 1)
    assert rows[0]["status"] == "failed" and json.loads(rows[0]["usage"]) == {}
    assert [row["attempt_id"] for row in rows] == [attempt for attempt, _, _ in sent]
    assert all(group == logical for _, group, _ in sent)
    assert budget["tokens"] == rows[0]["reserved_tokens"] + (65 if mode == "retry" else 0)
    if mode == "retry":
        assert sent[0][2] == sent[1][2]
        assert rows[1]["status"] == "complete" and json.loads(rows[1]["usage"])["total_tokens"] == 65
        assert isinstance(json.loads(rows[1]["observation"])["provider_first_packet_ms"], int)


def test_official_model_retry_continue_synthesizes_a_response():
    retry = ModelRetryMiddleware(max_retries=0, retry_on=(TimeoutError,))

    async def fail(_):
        raise TimeoutError("fixture transport did not produce a completion")

    result = asyncio.run(retry.awrap_model_call(None, fail))
    answer, = result.result
    assert isinstance(answer, AIMessage) and not answer.response_metadata


def test_official_tool_retry_reuses_call_and_cannot_retry_or_attest_host_receipt():
    from langchain.tools import ToolRuntime

    attempts = []

    @tool
    async def read_fixture(runtime: ToolRuntime) -> str:
        """Read synthetic state."""
        attempts.append(runtime.tool_call_id)
        if len(attempts) == 1:
            raise BlockingIOError("fixture transient")
        return "read"

    @tool
    async def write_fixture(runtime: ToolRuntime) -> Command:
        """Return a synthetic uncertain write receipt."""
        cid = runtime.tool_call_id
        attempts.append(cid)
        return Command(update={"messages": [ToolMessage("unknown", tool_call_id=cid, status="error")],
                               "operation_signals": {cid: {"operation_id": "original", "uncertain": True}}})

    retry = ToolRetryMiddleware(max_retries=1, retry_on=(BlockingIOError,), initial_delay=0,
                                jitter=False, on_failure="error")
    builder = StateGraph(HarnessState)
    builder.add_node("tools", ToolNode([read_fixture, write_fixture], awrap_tool_call=retry.awrap_tool_call))
    builder.add_edge(START, "tools").add_edge("tools", END)
    graph = builder.compile()

    async def invoke(name):
        return await graph.ainvoke({"messages": [AIMessage("", tool_calls=[{"name": name, "id": name, "args": {}}])]})

    read = asyncio.run(invoke("read_fixture"))
    unknown = asyncio.run(invoke("write_fixture"))
    assert attempts == ["read_fixture", "read_fixture", "write_fixture"]
    assert read["messages"][-1].content == "read"
    assert unknown["messages"][-1].status == "error"
    assert unknown["operation_signals"] == {"write_fixture": {"operation_id": "original", "uncertain": True}}


def test_official_call_limit_does_not_checkpoint_the_admitted_failed_step():
    identity = ExecutionIdentity(7, "session", "request", "run")
    config = {"configurable": {"thread_id": identity.run_id}}
    messages = [HumanMessage("fixture")]

    async def exercise():
        stock_saver, host_saver = InMemorySaver(), InMemorySaver()
        stock_model = AsyncMock(side_effect=RuntimeError("fixture send failed"))
        host_model = AsyncMock(side_effect=RuntimeError("fixture send failed"))
        stock = create_agent(ScriptedModel(script=stock_model), checkpointer=stock_saver, context_schema=_RunContext,
            middleware=[ModelCallLimitMiddleware(thread_limit=1, run_limit=1, exit_behavior="error")])
        host = HarnessRuntime(ScriptedModel(script=host_model), AsyncMock(), lambda *_: None,
            AsyncMock(), RunLimits(max_model_steps=1), [])
        with pytest.raises(RuntimeError, match="fixture send failed"):
            await stock.ainvoke({"messages": messages}, config, durability="sync", context=_RunContext(identity))
        with pytest.raises(RuntimeError, match="fixture send failed"):
            await host.run(identity, messages, saver=host_saver)
        stock_model.assert_awaited_once()
        host_model.assert_awaited_once()
        stock_before_failure = (await stock_saver.aget_tuple(config)).checkpoint["channel_values"]
        host_before_failure = (await host_saver.aget_tuple(config)).checkpoint["channel_values"]
        assert "run_model_call_count" not in stock_before_failure
        assert "thread_model_call_count" not in stock_before_failure
        assert host_before_failure["model_steps"] == 1

    asyncio.run(exercise())
