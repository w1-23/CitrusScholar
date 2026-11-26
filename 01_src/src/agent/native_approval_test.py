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
from src.agent.native_approval import (
    InvalidNativeApproval, StoredApproval, approval_resume, native_approval_middleware,
    pending_approvals,
)


@pytest.mark.parametrize('negative', ['rejected', 'expired', 'cancelled'])
def test_native_per_call_approval_reopens_protected_checkpoint(tmp_path, negative):
    assert version('deepagents') == '0.7.38'
    assert version('langgraph') == '1.2.22'
    who = ExecutionIdentity(7, 'test-session', 'test-turn', 'test-run')
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
        rows = tuple(StoredApproval(b.approval_id, b, state)
                     for b, state in zip(bindings, ('granted', negative), strict=True))

        async with open_framework_checkpoint(tmp_path, who, provider, read_only=True) as saver:
            graph = compile_graph(saver)
            reopened = await graph.aget_state(config)
            assert pending_approvals(reopened, who, **policy) == bindings
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
