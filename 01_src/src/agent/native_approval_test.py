import asyncio
from copy import deepcopy
from dataclasses import replace
from importlib.metadata import version

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agent.checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
from src.agent.deep_agent_runtime import create_framework_agent
from src.agent.framework_test_support import ScriptedModel
from src.agent.harness import HarnessState, _RunContext
from src.agent.identity import ExecutionIdentity
from src.core import jobs, task_events
from src.agent.native_approval import (
    InvalidNativeApproval, StoredApproval, approval_resume, native_approval_middleware,
    durable_approvals, pending_approvals,
)


@pytest.mark.parametrize('negative', ['rejected', 'expired', 'cancelled'])
def test_native_per_call_approval_reopens_protected_checkpoint(tmp_path, negative, monkeypatch):
    assert version('deepagents') == '0.7.38'
    assert version('langgraph') == '1.2.22'
    monkeypatch.setattr(jobs, '_DB_PATH', tmp_path / 'tasks.sqlite3')
    claim = jobs.claim_job('test-session', 'test-turn', owner_user_id=7)
    who = ExecutionIdentity(7, 'test-session', 'test-turn', claim.job_id)
    provider = ('https://test.invalid/v1', 'deepseek-fixture')
    scope = framework_checkpoint_scope(who, provider)
    config = {'configurable': scope}
    policy = {'scope': scope, 'tool_names': ('guarded',)}
    args = {'item': 'private argument fixture', 'count': 2}
    executions = []

    async def guarded(item: str, count: int) -> str:
        """Produce a test-only side effect."""
        executions.append((item, count))
        return 'completed the one approved call'

    async def model(messages, identity, **_):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage('done', additional_kwargs=identity.message_metadata('assistant'))
        return AIMessage('', tool_calls=[
            {'name': 'guarded', 'args': {**args}, 'id': cid}
            for cid in ('first', 'second')
        ], additional_kwargs=identity.message_metadata('assistant'))

    def compile_graph(saver):
        return create_framework_agent(ScriptedModel(script=model), [guarded],
            [native_approval_middleware(policy['tool_names'])], saver, HarnessState,
            context_schema=_RunContext, task_subagent=False)

    async def scenario():
        context = _RunContext(who)
        async with open_framework_checkpoint(tmp_path, who, provider) as saver:
            graph = compile_graph(saver)
            await graph.ainvoke({'messages': [HumanMessage('test')], 'active_run_id': who.run_id},
                                config, context=context, durability='sync')
            parked = await graph.aget_state(config)
            bindings = pending_approvals(parked, who, **policy)
        assert [b.tool_call_id for b in bindings] == ['first', 'second']
        assert len({b.approval_id for b in bindings}) == 2
        assert bindings[0].args_hash == bindings[1].args_hash
        assert all(b.request_event() == {'approval_id': b.approval_id, 'job_id': who.run_id,
                                          'tool_name': 'guarded', 'status': 'pending'} for b in bindings)
        assert not executions
        requested = durable_approvals(parked, who, register=True, **policy)
        assert requested == tuple(StoredApproval(b.approval_id, b, 'pending') for b in bindings)
        assert durable_approvals(parked, who, register=True, **policy) == requested
        with task_events.read_transaction() as conn:
            events = conn.execute("SELECT data FROM task_events WHERE job_id=? AND event='permission_request'",
                                  (who.run_id,)).fetchall()
        assert len(events) == 2 and all('private argument' not in event[0] for event in events)
        with pytest.raises(InvalidNativeApproval, match='approval_job_mismatch'):
            durable_approvals(parked, replace(who, owner_user_id=8), **policy)
        for binding, decision in zip(bindings, ('granted', negative), strict=True):
            task_events.decide_approval(who.run_id, binding.approval_id, decision)

        async with open_framework_checkpoint(tmp_path, who, provider, read_only=True) as saver:
            graph = compile_graph(saver)
            reopened = await graph.aget_state(config)
            assert pending_approvals(reopened, who, **policy) == bindings
            rows = durable_approvals(reopened, who, **policy)
            assert tuple(row.status for row in rows) == ('granted', negative)
            assert pending_approvals(reopened._replace(interrupts=reopened.interrupts[::-1]),
                                     who, **policy) == bindings
            with pytest.raises(InvalidNativeApproval, match='batch_not_ready'):
                approval_resume(reopened, who, records=rows[:1], **policy)
            with pytest.raises(InvalidNativeApproval, match='duplicate_approval_record'):
                approval_resume(reopened, who, records=(rows[0], rows[0]), **policy)
            for status in ('pending', 'edit', 'approve', 'respond', 'unknown'):
                with pytest.raises(InvalidNativeApproval, match='decision_not_final'):
                    approval_resume(reopened, who, records=(replace(rows[0], status=status), rows[1]), **policy)
            for field, bogus in [('args_hash', 'bad-hash'), ('tool_call_id', 'second'),
                                 ('interrupt_id', 'bad-interrupt'), ('checkpoint_id', 'stale-checkpoint'),
                                 ('thread_id', 'wrong-thread'), ('checkpoint_ns', 'nested'),
                                 ('identity', replace(who, owner_user_id=8))]:
                changed = replace(rows[0], binding=replace(rows[0].binding, **{field: bogus}))
                with pytest.raises(InvalidNativeApproval, match='binding_mismatch'):
                    approval_resume(reopened, who, records=(changed, rows[1]), **policy)
            wrong_scope = {**scope, 'thread_id': 'another-thread'}
            with pytest.raises(InvalidNativeApproval, match='checkpoint'):
                pending_approvals(reopened, who, **{**policy, 'scope': wrong_scope})
            with pytest.raises(InvalidNativeApproval, match='untrusted_approval_proposal'):
                pending_approvals(reopened, replace(who, session_id='other-session'), **policy)
            with pytest.raises(InvalidNativeApproval, match='proposal_call_mismatch'):
                pending_approvals(reopened, who, **{**policy, 'tool_names': ('other-tool',)})
            modified = deepcopy(reopened)
            modified.interrupts[0].value['args']['count'] = 3
            with pytest.raises(InvalidNativeApproval, match='argument_hash_mismatch'):
                pending_approvals(modified, who, **policy)
            modified = deepcopy(reopened)
            modified.values['messages'][-1].additional_kwargs['context_trusted'] = 1
            with pytest.raises(InvalidNativeApproval, match='untrusted_approval_proposal'):
                pending_approvals(modified, who, **policy)

        async with open_framework_checkpoint(tmp_path, who, provider) as saver:
            graph = compile_graph(saver)
            reopened = await graph.aget_state(config)
            command = approval_resume(reopened, who, records=rows[::-1], **policy)
            assert command.resume == {bindings[0].interrupt_id: {'type': 'approve'},
                                      bindings[1].interrupt_id: {'type': 'reject'}}
            result = await graph.ainvoke(command, config, context=context, durability='sync')
            assert executions == [(args['item'], args['count'])]
            observations = {m.tool_call_id: m for m in result['messages'] if isinstance(m, ToolMessage)}
            assert observations['first'].content == 'completed the one approved call'
            assert observations['second'].status == 'error'
            assert 'not executed' in observations['second'].content
            head = await graph.aget_state(config)
            with pytest.raises(InvalidNativeApproval):
                approval_resume(head, who, records=rows, **policy)
            assert executions == [(args['item'], args['count'])]

    asyncio.run(scenario())



@pytest.mark.parametrize("decision", ["granted", "rejected", "mixed_batch"])
def test_harness_resumes_only_the_durable_approved_call(tmp_path, monkeypatch, decision):
    from langgraph.checkpoint.memory import InMemorySaver
    from src.agent.harness import HarnessRuntime
    from src.agent.runtime import FinalCheck, RunLimits

    monkeypatch.setattr(jobs, '_DB_PATH', tmp_path / 'harness-tasks.sqlite3')
    who = ExecutionIdentity(7, 'session-harness', 'turn-harness',
                            jobs.claim_job('session-harness', 'turn-harness', owner_user_id=7).job_id)
    effects, decisions = [], []
    schemas = [{"type": "function", "function": {"name": key, "description": key,
                "parameters": {"type": "object", "properties": {}}}} for key in ("earlier", "guarded")]

    async def model(messages, identity, **_):
        if any(isinstance(msg, ToolMessage) and msg.tool_call_id == "guarded-call" for msg in messages):
            return AIMessage("done")
        key = "guarded" if any(isinstance(msg, ToolMessage) for msg in messages) else "earlier"
        tool_calls = [{"name": key, "args": {}, "id": key + "-call"}]
        if key == "guarded" and decision == "mixed_batch":
            tool_calls.append({"name": "earlier", "args": {}, "id": "sibling-call"})
        return AIMessage("", tool_calls=tool_calls)

    async def execute(call, identity):
        effects.append(call["name"])
        return ToolMessage("effect", tool_call_id=call["id"], name=call["name"]), {}

    async def permit(identity, ids):
        assert identity == who and effects == ["earlier"] and len(ids) == 1
        decisions.extend(ids)
        task_events.decide_approval(identity.run_id, ids[0], decision)
        return "this callback value is never a decision"

    async def check(*_):
        return FinalCheck()

    async def admit(*_):
        return None

    runtime = HarnessRuntime(ScriptedModel(script=model), execute, admit, check,
                             RunLimits(4), schemas, native_approval_tools=("guarded",), wait_native_approval=permit)
    result = asyncio.run(runtime.run(who, [HumanMessage("fixture")], saver=InMemorySaver(),
                                      agent_options={"task_subagent": False}))
    if decision == "mixed_batch":
        assert (result.status, result.reason) == ("incomplete", "approval_requires_single_call")
        assert effects == ["earlier"] and not decisions
        return
    assert result.status == "complete" and len(decisions) == 1
    assert effects == (["earlier", "guarded"] if decision == "granted" else ["earlier"])
    if decision == "rejected":
        observation, = [msg for msg in result.messages if isinstance(msg, ToolMessage)
                        and msg.tool_call_id == "guarded-call"]
        assert observation.additional_kwargs["execution_dispatched"] is False



@pytest.mark.parametrize("action", ["granted", "expired", "cancelled"])
def test_live_approval_waiter_observes_canonical_job_decision(tmp_path, monkeypatch, action):
    from src.core import progress_bus
    from src.agent.native_approval import wait_for_approval_decisions

    monkeypatch.setattr(jobs, '_DB_PATH', tmp_path / 'waiter.sqlite3')
    run = jobs.claim_job('session-wait', 'turn-wait', owner_user_id=7).job_id
    who = ExecutionIdentity(7, 'session-wait', 'turn-wait', run)
    aid = task_events.create_approval(run, 'commit_document_index', {})
    notifications = []

    async def scenario():
        notified = asyncio.Event()

        def emit(event, data):
            notifications.append(task_events.append_event(run, event, data))
            notified.set()

        monkeypatch.setattr(progress_bus, 'emit_progress', emit)
        task = asyncio.create_task(wait_for_approval_decisions(
            who, (aid,), timeout=0 if action == 'expired' else 5, poll_interval=0.01))
        await asyncio.wait_for(notified.wait(), timeout=2)
        if action == 'granted':
            task_events.decide_approval(run, aid, 'granted')
        if action == 'cancelled':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, timeout=2)
        assert task_events.approval_status(aid) == action
        frame = notifications[0]
        duplicate = task_events.append_event(run, 'permission_request', frame['data'])
        assert duplicate == frame
        with pytest.raises(InvalidNativeApproval, match='approval_job_mismatch'):
            await wait_for_approval_decisions(replace(who, owner_user_id=8), (aid,), timeout=0)

    asyncio.run(scenario())
