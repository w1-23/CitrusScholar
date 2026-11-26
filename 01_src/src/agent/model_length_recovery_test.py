"""Length-truncated model output: bounded re-ask, never a half-batch dispatch."""
import asyncio
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agent.capabilities import CapabilityCatalog, CapabilityDefinition
from src.agent.checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
from src.agent.framework_test_support import ScriptedModel
from src.agent.harness import HarnessRuntime
from src.agent.identity import ExecutionIdentity
from src.agent.runtime import FinalCheck, RunLimits
from src.core import jobs, task_events, tracing
from src.core.request_context import resolve_main_model_budget


def _length_message(call_id="cut-1"):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "inspect_uploaded_table",
                                              "args": {"path": "/input/report.csv"}}],
                     response_metadata={"finish_reason": "length"})


async def _run_turn(tmp_path, monkeypatch, script):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "ledger.sqlite3")
    claim = jobs.claim_job("session-length", "request", owner_user_id=7, payload={})
    task_events.init_budget(claim.job_id, time.time() + 60, max_calls=10, max_tokens=100_000)
    identity = ExecutionIdentity(7, "session-length", "request", claim.job_id)
    tracing.set_user_id(7)
    tracing.set_session_id(identity.session_id)
    tracing.set_job_id(claim.job_id)
    dispatches, histories = [], []
    catalog = CapabilityCatalog()
    catalog.register(CapabilityDefinition(
        "inspect_uploaded_table", "fixture table reader",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        lambda: None))

    async def execute(call, _identity):
        dispatches.append(call["id"])
        raise AssertionError("a truncated proposal must never be dispatched")

    async def admit(*_args):
        return None

    async def accept(*_args):
        return FinalCheck()

    async def tracked(history, request_identity, *, native_schemas):
        histories.append(history)
        return await script(history, request_identity, native_schemas=native_schemas)

    runtime = HarnessRuntime(ScriptedModel(script=tracked), execute, admit, accept,
                             RunLimits(max_model_steps=6),
                             catalog.schemas({"inspect_uploaded_table"}),
                             export_host=lambda: {}, recover_output=True)
    provider = ("https://example.test", "fixture")
    config = framework_checkpoint_scope(identity, provider)
    human = HumanMessage("draw the figure", additional_kwargs=identity.message_metadata("user"))
    async with open_framework_checkpoint(tmp_path / "state", identity, provider) as saver:
        result = await runtime.run(identity, [human], saver=saver, checkpoint_config=config,
                                   agent_options={"system_prompt": "fixture"})
    return result, dispatches, histories


def test_truncated_proposal_is_withdrawn_and_reasked_once(tmp_path, monkeypatch):
    async def script(history, identity, *, native_schemas):
        calls = sum(1 for message in history if isinstance(message, AIMessage))
        if calls == 0:
            return _length_message()
        return AIMessage(content="short answer")

    async def scenario():
        result, dispatches, histories = await _run_turn(tmp_path, monkeypatch, script)
        assert result.status == "complete", result.reason
        assert dispatches == []
        assert len(histories) == 2
        rejected = [message for message in result.messages
                    if isinstance(message, ToolMessage) and message.tool_call_id == "cut-1"]
        assert rejected and rejected[0].status == "error"
        assert rejected[0].artifact["_tool_outcome"] == {"status": "not_run", "reason_code": "model_length"}

    asyncio.run(scenario())


def test_a_second_truncation_stops_without_dispatch(tmp_path, monkeypatch):
    async def script(history, identity, *, native_schemas):
        return _length_message()

    async def scenario():
        result, dispatches, histories = await _run_turn(tmp_path, monkeypatch, script)
        assert (result.status, result.reason) == ("incomplete", "model_length")
        assert dispatches == []
        # Exactly one bounded re-ask, never an unbounded loop.
        assert len(histories) == 2

    asyncio.run(scenario())


def test_output_budget_uses_the_verified_provider_entry():
    from src.config import settings

    transport = SimpleNamespace(provider_id="opencode_go", model="deepseek-v4.1-flash",
                                configured_context_window=1_000_000)
    budget = resolve_main_model_budget(transport, settings)
    assert budget.max_output_tokens == 8192
    assert budget.output_limit_source == "provider_model_configuration"

    unknown = SimpleNamespace(provider_id="unlisted", model="unlisted-model",
                              configured_context_window=1_000_000)
    fallback = resolve_main_model_budget(unknown, settings)
    assert fallback.max_output_tokens == min(int(settings.MAX_TOKENS), int(settings.AGENT_MAX_OUTPUT_TOKENS))
    assert fallback.output_limit_source == "legacy_configuration"


def test_reask_resets_on_new_trusted_turn_in_same_thread():
    from collections import Counter
    from langgraph.checkpoint.memory import InMemorySaver

    observed = Counter()

    async def model(history, identity, *, native_schemas):
        observed[identity.run_id] += 1
        if observed[identity.run_id] == 1:
            return _length_message("cut-" + identity.run_id)
        return AIMessage(content="short answer")

    async def dispatch(*args):
        raise AssertionError("a truncated call must not dispatch")

    async def admit(*args):
        pass

    async def accept(*args):
        return FinalCheck()

    async def run():
        runtime = HarnessRuntime(ScriptedModel(script=model), dispatch, admit, accept,
                                 RunLimits(max_model_steps=6), [], recover_output=True)
        saver = InMemorySaver()
        for turn in ("first", "second"):
            identity = ExecutionIdentity(7, "shared", turn, turn)
            message = HumanMessage(content=turn, additional_kwargs=identity.message_metadata("user"))
            result = await runtime.run(identity, [message], saver=saver,
                checkpoint_config={"thread_id": "length-recovery-same-thread"})
            assert result.status == "complete", result.reason
        assert observed == {"first": 2, "second": 2}

    asyncio.run(run())
