from langchain_core.messages import ToolMessage
from src.agent.identity import ExecutionIdentity
from src.core import tool_results


def test_provider_alias_is_bound_to_the_exact_execution_and_ambiguous_after_rotation(monkeypatch, tmp_path):
    from src.session import manager

    monkeypatch.setattr(manager, 'DB_PATH', tmp_path / 'test.sqlite3')
    monkeypatch.setattr(tool_results, '_schema_ready', False)
    provider = 'same-provider-call'
    scopes = [ExecutionIdentity(1, 'session-1', 'turn-1', 'job-1'),
              ExecutionIdentity(1, 'session-1', 'turn-2', 'job-2'),
              ExecutionIdentity(2, 'session-2', 'turn-2', 'job-3')]
    keys = [tool_results.execution_receipt_key(scope, provider) for scope in scopes]
    assert len(set(keys)) == 3
    for i, (scope, key) in enumerate(zip(scopes, keys)):
        values = dict(tool_call_id=key, provider_call_id=provider, session_id=scope.session_id,
                      request_id=scope.user_turn_id, tool_name='fixture', result_text=str(i))
        ref = tool_results.record_with_reference(**values)
        msg = ToolMessage(content=str(i), name='fixture', tool_call_id=provider,
                          additional_kwargs={'result_reference': ref})
        assert msg.tool_call_id == provider and tool_results.observation_receipt_key(msg)==key
        assert tool_results.record_with_reference(**values) == ref
        assert tool_results.fetch_for_session(scope.session_id,key)['result_text'] == str(i)
    assert tool_results.fetch_for_session('session-1', provider) is None
    assert tool_results.fetch_for_session('session-1', provider, request_id='turn-2')['result_text'] == '1'
    assert tool_results.fetch_for_session('session-2', keys[0]) is None
    assert tool_results.pin_references('session-1',[keys[0],keys[1]])
    with tool_results._connect() as db:
        db.execute('UPDATE tool_results SET retained=0 WHERE tool_call_id=?',(keys[0],))
    assert tool_results.fetch_for_session('session-1', provider) is None
    assert tool_results.fetch_for_session('session-1', provider, request_id='turn-1') is None
    assert tool_results.record(tool_call_id=provider, session_id='legacy', request_id='older', result_text='legacy result')
    assert tool_results.fetch_for_session('legacy', provider)['result_text'] == 'legacy result'
