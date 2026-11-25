from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
import pytest
from typing_extensions import NotRequired

from .post_turn_compaction import compact_post_turn, select_working_messages


class _State(AgentState):
    active_run_id: NotRequired[str]
    stop_status: NotRequired[str]
    working_context: NotRequired[dict]


class _Projection(AgentMiddleware):
    state_schema = _State

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(messages=select_working_messages(
            request.messages, request.state.get("working_context"),
        )))


class _Recorder(BaseChatModel):
    reply: AIMessage
    received: list = Field(default_factory=list)
    on_invoke: Callable[[], Awaitable[None]] | None = Field(default=None, exclude=True)

    @property
    def _llm_type(self):
        return "unit-fixture"

    def _generate(self, *args, **kwargs):
        raise AssertionError("fixture uses only async model calls")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        if self.on_invoke:
            await self.on_invoke()
        return ChatResult(generations=[ChatGeneration(message=self.reply.model_copy())])


async def _idle():
    return False


async def _terminal_fixture(*, stop_status="complete"):
    foreground = _Recorder(reply=AIMessage(content="Next answer"))
    graph = create_agent(foreground, [], middleware=[_Projection()], checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "turn-compaction-test"}}
    calls = [dict(id=call_id, name="fixture", args={}) for call_id in ("T1", "T2")]
    tape = [HumanMessage(content="older user " * 200, id="h0"),
            AIMessage(content="older answer " * 200, id="a0"),
            HumanMessage(content="tool user", id="h1"),
            AIMessage(content="", id="a1", tool_calls=calls),
            ToolMessage(content="first tool result", id="r1", tool_call_id="T1"),
            ToolMessage(content="second tool result", id="r2", tool_call_id="T2"),
            AIMessage(content="terminal answer", id="a2")]
    config = await graph.aupdate_state(config, {"active_run_id": "turn-1", "stop_status": stop_status,
                                               "messages": tape}, as_node="model")
    return foreground, graph, config, tape


def _summary(content="Completed summary", finish="stop", **metadata):
    if finish is not None:
        metadata["finish_reason"] = finish
    return _Recorder(reply=AIMessage(content=content, response_metadata=metadata))


def _head(config):
    return {"configurable": {"thread_id": config["configurable"]["thread_id"]}}


async def _compact(graph, config, tape, transport, *, session_lock=None, **kwargs):
    return await compact_post_turn(graph, config, summary_model=transport, trigger_tokens=500,
                                   keep_tokens=count_tokens_approximately(tape[-2:]),
                                   session_lock=session_lock or asyncio.Lock(), expected_run_id="turn-1",
                                   as_node="model", **{"is_busy": _idle, **kwargs})


def test_sdk_working_branch_preserves_tape_and_whole_tool_pairs():
    async def exercise():
        foreground, graph, config, tape = await _terminal_fixture()
        transport = _summary()
        assert not foreground.received
        assert await _compact(graph, config, tape, transport) == "compacted"
        assert len(transport.received) == 1 and not foreground.received

        head = _head(config)
        after = await graph.aget_state(head)
        assert after.values["messages"] == tape and not after.next
        working = after.values["working_context"]
        assert working["through_message_id"] == "a2"
        assert [message.id for message in working["messages"][1:]] == ["a1", "r1", "r2", "a2"]
        assert "Completed summary" in working["messages"][0].content

        assert await _compact(graph, after.config, tape, transport) == "ineligible"
        assert (await graph.aget_state(head)).config == after.config
        assert len(transport.received) == 1

        await graph.ainvoke({"messages": [HumanMessage(content="Fresh turn", id="fresh")]}, head)
        first_call, = foreground.received
        assert first_call[:-1] == working["messages"]
        assert first_call[-1].content == "Fresh turn"
        assert (await graph.aget_state(head)).values["messages"][:len(tape)] == tape
        assert len(transport.received) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize(("content", "stop", "metadata"), [
    ("partial", "length", {}), ("partial", "max_tokens", {}), (" ", "stop", {}),
    ("invalid", "unknown", {}), ("missing", None, {}),
    ("unconfirmed", "stop", {"citrus_stream_terminal_unconfirmed": True}),
])
def test_invalid_provider_completion_leaves_checkpoint_untouched(content, stop, metadata):
    async def exercise():
        foreground, graph, config, tape = await _terminal_fixture()
        transport = _summary(content, stop, **metadata)
        assert await _compact(graph, config, tape, transport) == "failed"
        latest = await graph.aget_state(_head(config))
        assert latest.config == config and latest.values["messages"] == tape
        assert "working_context" not in latest.values
        assert not foreground.received and len(transport.received) == 1
    asyncio.run(exercise())


def test_skips_busy_or_nonterminal_session_before_summary():
    async def exercise():
        _, graph, config, tape = await _terminal_fixture(stop_status="waiting")
        transport = _summary()
        assert await _compact(graph, config, tape, transport) == "ineligible"
        lock = asyncio.Lock()
        async with lock:
            assert await _compact(graph, config, tape, transport, session_lock=lock) == "busy"

        async def busy():
            assert lock.locked()
            return True

        assert await _compact(graph, config, tape, transport, session_lock=lock, is_busy=busy) == "busy"
        assert not transport.received
        assert (await graph.aget_state(_head(config))).config == config
    asyncio.run(exercise())


def test_rechecks_head_and_releases_both_locks_during_summary():
    async def exercise():
        _, graph, config, tape = await _terminal_fixture()
        lock = asyncio.Lock()
        locks = []
        newer = None

        @contextmanager
        def execution_guard():
            assert lock.locked()
            locks.append("acquire")
            try:
                yield
            finally:
                locks.append("release")

        async def complete_newer_checkpoint():
            nonlocal newer
            assert not lock.locked() and locks == ["acquire", "release"]
            newer = await graph.aupdate_state(config, {"messages": [HumanMessage(content="Advanced", id="new")]},
                                              as_node="model")

        transport = _summary()
        transport.on_invoke = complete_newer_checkpoint
        outcome = await _compact(graph, config, tape, transport, session_lock=lock, execution_guard=execution_guard)
        assert outcome == "stale" and locks == ["acquire", "release"] * 2
        latest = await graph.aget_state(_head(config))
        assert latest.config == newer and "working_context" not in latest.values
    asyncio.run(exercise())


def test_new_queue_activity_and_nonblocking_execution_guard_skip_commit():
    async def exercise():
        _, graph, config, tape = await _terminal_fixture()
        probes = []

        async def queue_opened():
            probes.append(True)
            return len(probes) > 1

        transport = _summary()
        assert await _compact(graph, config, tape, transport, is_busy=queue_opened) == "busy"
        assert len(transport.received) == 1 and len(probes) == 2

        @contextmanager
        def contended_execution_guard():
            raise RuntimeError("agent_run_already_active")
            yield

        assert await _compact(graph, config, tape, transport, execution_guard=contended_execution_guard) == "busy"
        assert len(transport.received) == 1
        assert (await graph.aget_state(_head(config))).config == config
    asyncio.run(exercise())



@pytest.mark.parametrize("unsafe_initial", [True, False])
def test_storage_validator_fences_hidden_live_graph_work(unsafe_initial):
    async def exercise():
        _, graph, config, tape = await _terminal_fixture()
        lock = asyncio.Lock()
        guard_held = False
        validations = []

        @contextmanager
        def execution_guard():
            nonlocal guard_held
            guard_held = True
            try:
                yield
            finally:
                guard_held = False

        async def check_committed(snapshot):
            assert lock.locked() and guard_held
            assert snapshot.config == config
            validations.append(snapshot)
            return len(validations) == 1 and not unsafe_initial

        transport = _summary()
        outcome = await _compact(graph, config, tape, transport, session_lock=lock,
                                 execution_guard=execution_guard, check_committed=check_committed)
        assert outcome == ("ineligible" if unsafe_initial else "stale")
        assert len(validations) == (1 if unsafe_initial else 2)
        assert len(transport.received) == (0 if unsafe_initial else 1)
        latest = await graph.aget_state(_head(config))
        assert latest.config == config and "working_context" not in latest.values
    asyncio.run(exercise())


def test_working_view_falls_back_when_anchor_cannot_be_resolved():
    tape = [HumanMessage(content="original", id="first"), AIMessage(content="new", id="second")]
    working_messages = [HumanMessage(content="summary", id="summary"),
                        AIMessage(content="last older answer", id="missing")]
    assert select_working_messages(tape, {"messages": working_messages, "through_message_id": "missing"}) == tape


def test_working_branch_survives_reopening_the_encrypted_framework(tmp_path):
    from .checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
    from .harness import HarnessRuntime, execution_lock, open_terminal_graph
    from .identity import ExecutionIdentity
    from .runtime import FinalCheck, RunLimits

    async def exercise():
        first = ExecutionIdentity(1, "compaction-session", "turn-1", "run-1")
        second = ExecutionIdentity(1, first.session_id, "turn-2", "run-2")
        scope = ("https://example.test", "summary-test-model")
        config = framework_checkpoint_scope(first, scope)
        live_calls = []

        async def model(history, identity, *, native_schemas):
            live_calls.append([str(message.content) for message in history])
            assert all(tool["function"]["name"] != "compact_conversation" for tool in native_schemas)
            return AIMessage(content="Answer", response_metadata={"finish_reason": "stop"})

        async def accept(*_):
            return FinalCheck()

        async def execute(*_):
            raise AssertionError("compaction must not dispatch a business tool")

        runtime = HarnessRuntime(model, execute, accept, accept, RunLimits(), [],
                                 working_view=select_working_messages)

        async def run(identity, content):
            async with open_framework_checkpoint(tmp_path, identity, scope) as saver:
                message = HumanMessage(content=content, additional_kwargs=identity.message_metadata("user"))
                result = await runtime.run(identity, [message], saver=saver, checkpoint_config=config)
                assert result.status == "complete"

        await run(first, "large old raw turn " * 500)
        async with open_terminal_graph(tmp_path, first, scope) as (graph, config_head, committed):
            initial = await graph.aget_state(config_head)
            assert await committed(initial) and "working_context" not in initial.values
            original = initial.values["messages"]
            transport = _summary()
            outcome = await compact_post_turn(
                graph, config_head, summary_model=transport, trigger_tokens=500, keep_tokens=110,
                session_lock=asyncio.Lock(), is_busy=_idle, expected_run_id=first.run_id,
                check_committed=committed, execution_guard=lambda: execution_lock(
                    tmp_path / "agent_locks", (first.owner_user_id, first.session_id)),
            )
            assert outcome == "compacted"
            assert len(transport.received) == len(live_calls) == 1

        await run(second, "New raw turn")
        assert len(live_calls) == 2 and len(transport.received) == 1
        assert any("Completed summary" in content for content in live_calls[-1])
        assert live_calls[-1][-1] == "New raw turn"
        assert not any("large old raw turn" in content for content in live_calls[-1])
        async with open_terminal_graph(tmp_path, second, scope) as (graph, config_head, committed):
            latest = await graph.aget_state(config_head)
            assert await committed(latest)
            assert latest.values["messages"][:len(original)] == original
    asyncio.run(exercise())


def test_storage_only_reader_rejects_pending_node_on_live_graph(tmp_path):
    from langgraph.graph import END, START, StateGraph

    from .checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
    from .deep_agent_runtime import EXECUTOR_ID
    from .harness import HarnessState, open_terminal_graph
    from .identity import ExecutionIdentity

    async def exercise():
        identity = ExecutionIdentity(1, "paused-session", "turn-1", "run-1")
        scope = ("https://example.test", "summary-test-model")
        config = {"configurable": framework_checkpoint_scope(identity, scope)}
        graph = StateGraph(HarnessState)
        graph.add_node("prepare", lambda _: {"stop_status": "complete", "executor_id": EXECUTOR_ID,
                                            "active_run_id": identity.run_id})
        graph.add_node("live_pending", lambda _: pytest.fail("must not run a pending live node"))
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "live_pending")
        graph.add_edge("live_pending", END)
        async with open_framework_checkpoint(tmp_path, identity, scope) as saver:
            live = graph.compile(checkpointer=saver, interrupt_before=["live_pending"])
            await live.ainvoke({"messages": [HumanMessage(content="long tape " * 500), AIMessage(content="answer")]}, config)
            stored = await live.aget_state(config)
            assert stored.next == ("live_pending",)

        async with open_terminal_graph(tmp_path, identity, scope) as (reader, head, committed):
            snapshot = await reader.aget_state(head)
            assert not snapshot.next and snapshot.values["stop_status"] == "complete"
            assert not await committed(snapshot)
            transport = _summary()
            outcome = await compact_post_turn(
                reader, head, summary_model=transport, trigger_tokens=500, keep_tokens=110,
                session_lock=asyncio.Lock(), is_busy=_idle, expected_run_id=identity.run_id,
                check_committed=committed,
            )
            assert outcome == "ineligible" and not transport.received
            assert (await reader.aget_state(head)).config == stored.config
    asyncio.run(exercise())
