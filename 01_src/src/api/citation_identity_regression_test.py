import asyncio
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from langchain_core.messages import ToolMessage

from src import config
from src.api import main
from src.core import jobs, progress_bus, tool_results, tracing, turn_source_ledger
from src.graph.agent_operations import _emit_completed_tool_step
from src.research.result_tools import read_tool_result
from src.retrieval import corpus_registry, evidence_adapter
from src.session import manager
from src.tools.deepseek_search_fetch import read_local_evidence


@pytest.fixture
def citation_session(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(manager, 'STATE_DIR', state)
    monkeypatch.setattr(manager, 'DB_PATH', state / 'sessions.db')
    monkeypatch.setattr(manager.SessionManager, '_instance', None)
    monkeypatch.setattr(jobs, '_DB_PATH', state / 'jobs.db')
    monkeypatch.setattr(tool_results, '_schema_ready', False)
    monkeypatch.setattr(turn_source_ledger, '_schema_ready', False)
    sessions = manager.SessionManager()
    monkeypatch.setattr(main, 'session_manager', sessions)
    monkeypatch.setattr(manager, 'session_manager', sessions)
    user = {'user_id': 31, 'role': 'user'}
    monkeypatch.setattr(main, 'current_user', lambda _: dict(user))
    sid = asyncio.run(sessions.get_or_create_session('citation-session', owner_user_id=31))
    job_id = jobs.claim_job(sid, 'server-request', owner_user_id=31, client_request_id='client-request', payload={}).job_id
    tokens = [(var, var.set(value)) for var, value in (
        (tracing._user_id, 31), (tracing._session_id, sid), (tracing._job_id, job_id),
        (tracing._client_request_id, 'client-request'))]
    progress_bus.set_turn_scope(sid, 'client-request')
    app = FastAPI()
    app.add_api_route('/corpus/source', main.corpus_source_endpoint)
    app.add_api_route('/sessions/{session_id}/turn-sources', main.session_turn_sources)
    app.add_api_route('/sessions/{session_id}/citations', main.session_citations)
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, sid=sid, user=user, job=job_id, root=tmp_path)
    finally:
        progress_bus.clear_turn_scope()
        for var, token in reversed(tokens):
            var.reset(token)


def record_tool(name, call_id, content, artifact):
    outcome = {'status': 'complete', 'reason_code': '', 'artifacts': []}
    message = ToolMessage(name=name, tool_call_id=call_id, content=content,
                          artifact={**artifact, '_tool_outcome': outcome})
    assert _emit_completed_tool_step({'name': name, 'id': call_id}, message)['status'] == 'complete'
    assert message.additional_kwargs['result_reference']['storage']['complete']


def test_base_citation_reads_original_range_and_denies_other_scope(citation_session, monkeypatch):
    run = citation_session
    sources_url = f'/sessions/{run.sid}/turn-sources?request_id=client-request'
    text = 'Intro. Original citation passage. Following paragraph.'
    row = {'corpus_id': 'cqa_literature', 'paper_id': 'paper_fixture_identity',
           'chunk_id': 'chunk_fixture_identity', 'title': 'Original title', 'text': text}
    matches, receipt = evidence_adapter.persist_local_rows([row, {**row, 'corpus_id': 'cqa_methods'}],
                                                         query='synthetic', generation='base-old')
    assert receipt['status'] == 'stored'
    first, second = matches
    assert first['source_ref'] != second['source_ref']
    record_tool('local_hybrid_search', 'candidate', '{"candidates":[]}', {'main_results': matches})
    record_tool('inspect_candidates', 'inspect', 'candidate metadata', {'main_results': matches})
    assert run.client.get(sources_url).json()['sources'] == []
    args = {'ref': first['source_ref'], 'turn_id': run.job, 'generation': 'base-old'}
    body, artifact = read_local_evidence.func(**args, offset=7, max_chars=26)
    record_tool('read_candidate', 'actual-read', body, artifact)
    assert json.loads(read_tool_result.func('actual-read'))['result_text'] == body
    jobs.update_job(run.job, status='completed')
    persisted = run.client.get(sources_url).json()['sources']
    assert len(persisted) == 1
    assert run.client.get(sources_url.replace('client-request', 'server-request')).json()['sources'] == persisted
    source = persisted[0]
    assert source['source_ref'] == first['source_ref']
    assert source['read_ref'] == {**args, 'start': 7, 'end': 33}
    assert source['source_status'] == 'available'
    assert source['corpus'] == 'cqa_literature'
    citation_url = '/corpus/source'
    params = {'record': source['record_key']}
    response = run.client.get(citation_url, params=params)
    assert response.status_code == 200, response.text
    assert response.json()['markdown'] == text[7:33]
    assert response.json()['kind'] == 'stored_evidence'
    assert response.headers['Cache-Control'] == 'private, no-store'

    from src.core.capability_policy import CapabilityPolicy, bind_policy

    with bind_policy(CapabilityPolicy(local_rag=False)):
        with monkeypatch.context() as context:
            def unexpected_evidence_read(*args, **kwargs):
                pytest.fail('disabled retrieval opened stored evidence')

            context.setattr(evidence_adapter, 'read_evidence', unexpected_evidence_read)
            historical_read = json.loads(read_tool_result.func('actual-read'))
            assert historical_read['reason'] == 'evidence_unavailable'
            assert 'result_text' not in historical_read
        assert run.client.get(citation_url, params=params).json()['markdown'] == text[7:33]

    turn_source_ledger._schema_ready = False
    assert run.client.get(sources_url).json()['sources'] == persisted
    other_turn = jobs.claim_job(run.sid, 'next-request', owner_user_id=31, client_request_id='next-client', payload={}).job_id
    tracing.set_job_id(other_turn)
    progress_bus.set_turn_scope(run.sid, 'next-client')
    body, artifact = read_local_evidence.func(**args, offset=7, max_chars=26)
    record_tool('read_local_evidence', 'historical-read', body, artifact)
    historical = run.client.get(sources_url.replace('client-request', 'next-client')).json()
    assert historical['status'] == 'history'
    assert historical['sources'] == [{**source, 'origin': 'history'}]
    assert evidence_adapter.read_evidence(**{**args, 'turn_id': other_turn})['status'] == 'evidence_expired'
    assert evidence_adapter.read_evidence(**{**args, 'generation': 'base-new'})['status'] == 'evidence_expired'
    with pytest.raises(PermissionError):
        evidence_adapter.read_evidence(**args, user=run.user, session_id='other-session')
    legacy_url = f'/sessions/{run.sid}/citations'
    assert run.client.get(legacy_url).status_code == 200
    run.user['user_id'] = 32
    assert run.client.get(legacy_url).status_code == 404
    assert run.client.get(sources_url).status_code == 404
    assert run.client.get(citation_url, params=params).status_code == 404
    run.user['user_id'] = 31

    registry = corpus_registry.load_corpus_registry()
    restricted = SimpleNamespace(registered_corpus_ids={'cqa_methods'}, registry_version='restricted',
                                 access_level_for_corpus=registry.access_level_for_corpus)
    with monkeypatch.context() as context:
        context.setattr(corpus_registry, 'load_corpus_registry', lambda: restricted)
        assert run.client.get(citation_url, params=params).json()['reason'] == 'citation_unavailable'
        assert json.loads(read_tool_result.func('actual-read'))['reason'] == 'evidence_unavailable'
        denied = run.client.get(sources_url).json()['sources'][0]
        assert denied['source_status'] == 'unavailable' and 'snippet' not in denied
    [evidence_file] = run.root.glob('workspace/evidence/**/evidence.jsonl')
    evidence_file.unlink()
    assert run.client.get(citation_url, params=params).status_code == 404
    assert run.client.get(sources_url).json()['sources'][0]['read_ref'] == source['read_ref']


def test_candidate_read_reuses_only_its_bound_turn_scope(citation_session):
    from src.tools.candidate_tools import read_candidate

    run = citation_session
    source = {'corpus_id': 'cqa_literature', 'title': 'Stored fixture',
              'paper_id': 'paper_fixture_identity', 'chunk_id': 'chunk_fixture_identity',
              'text': 'Stored evidence for the current turn.'}
    matches, _ = evidence_adapter.persist_local_rows([source], query='fixture', generation='base-old')
    row = matches[0]
    ref = row['source_ref']
    _, unbound = read_candidate.func(ref, max_chars=20)
    assert unbound['_tool_outcome']['reason_code'] == 'invalid_evidence_read'

    record_tool('local_hybrid_search', 'candidate', 'Delivered [L1]',
                {'main_results': [{**row, 'citation_ref': 'L1'}]})
    _, result = read_candidate.func(ref, max_chars=20)
    assert result['read_ref'] == {'ref': ref, 'turn_id': run.job, 'generation': 'base-old',
                                  'start': 0, 'end': 20}
    _, wrong_scope = read_candidate.func(ref, turn_id='wrong-turn')
    assert wrong_scope['_tool_outcome']['reason_code'] == 'evidence_scope_unavailable'
    _, wrong_generation = read_candidate.func(ref, generation='wrong-generation')
    assert wrong_generation['_tool_outcome']['reason_code'] == 'evidence_expired'
    tracing._client_request_id.set('different-request')
    _, unbound = read_candidate.func(ref)
    assert unbound['_tool_outcome']['reason_code'] == 'invalid_evidence_read'


def test_web_fetch_receipt_and_legacy_ledger_migration(citation_session):
    run = citation_session
    url = f'/sessions/{run.sid}/turn-sources?request_id=client-request'
    with sqlite3.connect(manager.DB_PATH) as db:
        db.execute('CREATE TABLE turn_source_ledger (id INTEGER PRIMARY KEY, session_id TEXT, '
                   'request_id TEXT, origin TEXT, kind TEXT, source_key TEXT, title TEXT, '
                   'url TEXT, snippet TEXT, source_turn_seq INTEGER, created_at TEXT)')
        db.execute("INSERT INTO turn_source_ledger VALUES (1, ?, 'old-client', 'current', 'local', "
                   "'paper_legacy', 'Legacy candidate', '', '', NULL, '')", (run.sid,))
    assert run.client.get(url.replace('client-request', 'old-client')).json()['status'] == 'unavailable'
    row = {'url': 'https://example.org/fetched', 'title': 'Fetched fixture', 'text': 'Real fetched excerpt'}
    record_tool('deepseek_search', 'search', 'Search-only candidate', {'web_results': [row]})
    record_tool('browse_web', 'browse', 'Unverified browse', {'web_results': [row]})
    assert run.client.get(url).json()['sources'] == []
    record_tool('deepseek_fetch', 'fetch', row['text'], {'web_results': [row]})
    source = run.client.get(url).json()['sources'][0]
    assert source['source_status'] == 'available' and source['url'] == row['url']
    assert source['read_ref']['tool_call_id'] == 'fetch'
    with sqlite3.connect(manager.DB_PATH) as db:
        db.execute("UPDATE tool_results SET retained=0 WHERE tool_call_id='fetch'")
    expired = run.client.get(url).json()['sources'][0]
    assert expired['source_ref'] == source['source_ref']
    assert expired['source_status'] == 'unavailable' and 'snippet' not in expired


def test_answer_bubbles_bind_delivered_local_and_duplicate_web_references(citation_session):
    from src.core.turn_sources import answer_citations, visible_citation_bubbles

    run = citation_session
    first = {'corpus_id': 'cqa_literature', 'paper_id': 'paper_fixture_identity',
             'chunk_id': 'chunk_fixture_identity', 'title': 'Fixture title',
             'text': 'First known excerpt. Second known excerpt.'}
    matches, _ = evidence_adapter.persist_local_rows([first], query='fixture', generation='base-old')
    for position, (start, width) in enumerate(((0, 13), (20, 12)), 1):
        body, artifact = read_local_evidence.func(ref=matches[0]['source_ref'], turn_id=run.job,
            generation='base-old', offset=start, max_chars=width)
        artifact['main_results'][0]['citation_ref'] = f'L{position}'
        record_tool('read_candidate', f'local-{position}', body, artifact)
    webpage = {'url': 'https://example.org/page', 'title': 'Site', 'summary': 'Delivered evidence'}
    for position in (1, 2):
        record_tool('deepseek_search', f'web-{position}', f'[W{position}] Site\nDelivered evidence',
                    {'web_results': [{**webpage, 'citation_ref': f'W{position}'}]})
    body, projection = answer_citations(
        'First[L1] second[L2] web[W2][W1] invalid[W99] history[H3]\n\n'
        'Literal `[L1]`, “quoted [W2]”, and [ordinary](https://example.org).',
        session_id=run.sid, request_id='client-request', user=run.user)
    assert body == ('First second web invalid history\n\n'
                    'Literal `[L1]`, “quoted [W2]”, and [ordinary](https://example.org).')
    assert projection['dropped'] == ['W99', 'H3']
    bubbles = projection['bubbles']
    assert [item['ref_id'] for item in bubbles] == ['L1', 'L2', 'W2']
    assert bubbles[0]['record_key'] != bubbles[1]['record_key']
    scope = {'session_id': run.sid, 'request_id': 'client-request'}
    assert visible_citation_bubbles(bubbles, run.user, **scope) == bubbles
    assert not visible_citation_bubbles([bubbles[-1]], run.user,
                                        session_id=run.sid, request_id='another-turn')
    assert not visible_citation_bubbles([{**bubbles[-1], 'url': 'https://example.org/other'}],
                                        run.user, **scope)
    with sqlite3.connect(manager.DB_PATH) as conn:
        conn.execute("UPDATE turn_source_ledger SET read_ref='{}' WHERE citation_ref='W2'")
    assert visible_citation_bubbles(bubbles, run.user, **scope) == bubbles[:2]


@pytest.mark.parametrize('abstract_only', [False, True])
def test_fetched_paper_can_be_cited_without_showing_internal_id(citation_session, monkeypatch, abstract_only):
    from src.core.turn_sources import answer_citations
    from src.tools import deepseek_search_fetch

    run = citation_session
    url = 'https://example.org/paper'
    source = {'url': url, 'title': 'A verified paper', 'format': 'search_result',
              'content': 'An excerpt delivered to the model.', 'ok': True,
              'validity': 'abstract_only' if abstract_only else 'valid'}
    monkeypatch.setattr(deepseek_search_fetch, '_enabled', lambda name: name == 'web_fetch')
    monkeypatch.setattr(deepseek_search_fetch, '_public_http_url_error', lambda _: '')
    monkeypatch.setattr(deepseek_search_fetch, '_retain_paper', lambda *args, **kwargs: None)
    monkeypatch.setattr(deepseek_search_fetch, '_retain_missing_paper', lambda **kwargs: None)

    def race(candidates, deadline, doi, title, attempts):
        attempts.append(source)
        return None if abstract_only else source

    monkeypatch.setattr(deepseek_search_fetch, '_race_candidates', race)
    assert turn_source_ledger.allocate_citation_refs(run.sid, 'client-request', 'web', 1) == ['W1']
    body, artifact = deepseek_search_fetch.deepseek_fetch_paper.func(urls=[url])
    assert '[W2]' in body
    assert artifact['web_results'][0]['citation_ref'] == 'W2'
    record_tool('deepseek_fetch_paper', 'paper-read', body, artifact)
    answer, projection = answer_citations('From the paper.[W2]', session_id=run.sid,
                                          request_id='client-request', user=run.user)
    assert answer == 'From the paper.'
    assert projection['bubbles'] == [
        {'kind': 'web', 'url': url, 'title': 'A verified paper', 'ref_id': 'W2'}]
