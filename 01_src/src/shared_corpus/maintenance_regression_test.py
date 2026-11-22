"""Shared literature publication and global maintenance regressions."""
import asyncio
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from src.api import chat_turn_queue_http_test as fixture
from src.core import corpus_review
from src.shared_corpus import runtime as shared
from src.shared_corpus.activity import ActivityMiddleware
from src.shared_corpus.api_router import router
from src.shared_corpus.journal import read_pointer, write_pointer

runtime = fixture.runtime

class Models:
    model_name = 'intfloat/e5-large-v2'
    dim = 1024
    def embed_query(self, text):
        return [1.0] + [0.0] * 1023
    def embed_passages(self, texts):
        return [self.embed_query(text) for text in texts]
    def rerank(self, query, rows, top_k):
        return rows[:top_k]

@pytest.fixture
def corpus(monkeypatch, tmp_path):
    store = shared.SharedCorpusRuntime(tmp_path / 'shared', models=lambda: (Models(), Models()))
    monkeypatch.setattr(shared, '_runtime', store)
    factory = fixture.FastAPI
    def app_with_maintenance(*args, **kwargs):
        app = factory(*args, **kwargs)
        app.include_router(router)
        app.add_middleware(ActivityMiddleware)
        return app
    monkeypatch.setattr(fixture, 'FastAPI', app_with_maintenance)
    yield store
    if store.handle:
        store.handle.close()

@pytest.fixture
def isolated(corpus, request):
    return request.getfixturevalue('runtime')

def stage(store, i, owner):
    corpus_review._schema_ready = False
    body = f'# Introduction\n\nSynthetic release specimen {i}.\n\n# Methods\n\n' + 'Deterministic harmless citation test. ' * 70 + f'\n\n# Results\n\nExample {i}.\n'
    c = corpus_review.archive_fetch({'url':f'https://example.org/synthetic/{i}', 'title':f'Synthetic release specimen {i}', 'content':body, 'ok':True}, doi=f'10.5555/release-{i}', user_id=owner, verified_license='cc-by', license_source_url=f'https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC{i}.1.json')
    p = store.catalog.propose(c['id'], owner)
    return store.catalog.prepare(p['proposal_id'], owner)

def step(store):
    return asyncio.run(store.step())

def test_gate_queue_activities_and_sqlite(corpus, isolated):
    rt = isolated
    old = stage(corpus, 1, rt.owner)
    original = corpus.request_build(rt.owner)
    step(corpus)
    clock = [corpus.activity.last_activity + 119.9]
    corpus.activity.clock = lambda: clock[0]
    step(corpus)
    assert corpus.journal.job(original['id'])['state'] == 'waiting_for_idle'
    clock[0] += .2
    step(corpus)
    assert corpus.generation and corpus.generation == read_pointer(corpus.root)
    old_generation = corpus.generation
    old_reference = corpus.search('release')[0]['read_ref']
    stage(corpus, 2, rt.owner)
    new = corpus.request_build(rt.owner)
    step(corpus)
    fixture.send(rt, 'first', 'Synthetic in-flight A')
    fixture.wait_for(lambda: rt.started == ['first'])
    fixture.send(rt, 'middle', 'Synthetic queued B')
    clock[0] += 121
    with corpus.activity.reader():
        corpus.activity.enter('H')
        try:
            step(corpus)
            assert corpus.status()['state'] == 'waiting_for_idle'
            assert read_pointer(corpus.root) == old_generation
            assert fixture.state(rt, 'middle') == 'queued'
            assert shared._durable_counts()['Q']
        finally:
            corpus.activity.leave('H')
    rt.released.update(('first', 'middle'))
    fixture.wait_for(lambda: fixture.state(rt, 'middle') == 'terminal')
    fixture.wait_for(lambda: not any(corpus.activity.counts.values()))
    # A real write lock from another connection must not partially enter maintenance.
    with closing(sqlite3.connect(corpus.journal.path)) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            step(corpus)
        assert not corpus.activity.gated and read_pointer(corpus.root) == old_generation
        db.rollback()
    assert corpus.journal.job(new['id'])['state'] == 'waiting_for_idle'
    entered, release = threading.Event(), threading.Event()
    original_publish = corpus._publish
    def publish(*args):
        entered.set()
        if not release.wait(timeout=9):
            raise TimeoutError('test hook did not release')
        return original_publish(*args)
    with patch.object(corpus, '_publish', publish), ThreadPoolExecutor(max_workers=1) as runner:
        started = time.monotonic()
        switched = runner.submit(step, corpus)
        try:
            assert entered.wait(4)
            assert read_pointer(corpus.root) == old_generation
            assert not corpus.activity.try_gate(lambda: {}, lambda: pytest.fail('double gate'))
            r = rt.client.get('/api/v2/shared-corpus/maintenance')
            assert r.status_code == 200 and r.json()['state'] == 'maintenance'
            assert r.json()['message'] and r.json()['active_generation_label'] == old_generation
            r = rt.client.get(f"/api/v2/shared-corpus/documents/{old['document_id']}")
            assert r.status_code == 503 and r.headers['Retry-After'] == '5'
            c = fixture.send(rt, 'after-c', 'Synthetic C during gate')
            replay = fixture.send(rt, 'after-c', 'Synthetic C during gate')
            d = fixture.send(rt, 'after-d', 'Synthetic D during gate')
            assert c['job_id'] == replay['job_id'] and c['queue_seq'] + 1 == d['queue_seq']
            assert fixture.state(rt, 'after-c') == fixture.state(rt, 'after-d') == 'queued'
            assert rt.started == ['first', 'middle']
            rt.released.update(('after-c', 'after-d'))
        finally:
            release.set()
        switched.result(timeout=8)
        print('Controlled gate including test barrier seconds:', round(time.monotonic()-started, 3), flush=True)
    fixture.wait_for(lambda: fixture.state(rt, 'after-d') == 'terminal')
    assert rt.started == ['first', 'middle', 'after-c', 'after-d']
    assert corpus.journal.job(new['id'])['state'] == 'active'
    assert corpus.generation != old_generation and corpus.generation == read_pointer(corpus.root)
    assert corpus.preview(old_reference['document_id'], generation=old_reference['index_generation'], version=old_reference['version_id'], digest=old_reference['md_sha256'])['markdown']



def test_load_failure_interrupted_switch_and_revocation(corpus, isolated):
    document = stage(corpus, 11, isolated.owner)
    corpus.activity.quiet_sec = 0
    corpus.request_build(isolated.owner)
    step(corpus)
    step(corpus)
    previous = corpus.generation
    reference = corpus.search('synthetic')[0]['read_ref']
    page = corpus.preview(reference['document_id'], generation=previous, version=reference['version_id'],
                          start=reference['start'], length=10, digest=reference['md_sha256'])
    assert len(page['markdown']) == 10
    assert page['markdown'] == corpus.catalog.read_document(document['document_id'], document['version_id'])[1][
        reference['start']:reference['start'] + 10]

    stage(corpus, 12, isolated.owner)
    failed = corpus.request_build(isolated.owner)
    step(corpus)
    with patch.object(corpus, '_load', side_effect=OSError('synthetic load failure')):
        step(corpus)
    assert corpus.journal.job(failed['id'])['state'] == 'failed'
    assert not corpus.activity.gated and read_pointer(corpus.root) == previous
    assert corpus.reference_visible(reference)

    interrupted = corpus.request_build(isolated.owner)
    corpus.journal.transition(interrupted, 'building')
    restarted = shared.SharedCorpusRuntime(corpus.root, models=corpus.models, quiet_sec=0)
    try:
        restarted.recover()
        assert restarted.journal.job(interrupted['id'])['state'] == 'queued'
        assert restarted.generation == previous
        step(restarted)
        job = restarted.journal.job(interrupted['id'])
        assert job['state'] == 'waiting_for_idle'
        restarted.journal.transition(job, 'maintenance', prior=previous)
        write_pointer(corpus.root, job['generation'])
    finally:
        restarted.handle.close()
    restarted = shared.SharedCorpusRuntime(corpus.root, models=corpus.models, quiet_sec=0)
    try:
        restarted.recover()
        assert restarted.journal.job(interrupted['id'])['state'] == 'recovered'
        assert restarted.generation == read_pointer(corpus.root) == previous
        assert restarted.reference_visible(reference)
        restarted.catalog.revoke(document['document_id'], document['version_id'], isolated.owner, reason='fixture')
        assert not restarted.reference_visible(reference)
        assert not restarted.search('synthetic')
        with pytest.raises(LookupError):
            restarted.preview(document['document_id'], version=document['version_id'], generation=previous)
    finally:
        restarted.handle.close()


def test_preparation_capacity_persists_and_preserves_license_upgrade(corpus, monkeypatch):
    from src.shared_corpus import orchestrator
    from src.shared_corpus.prepare import MAX_SOURCE_BYTES

    monkeypatch.setattr(orchestrator, 'MAX_PENDING', 1)
    assert orchestrator.schedule(corpus, 1, 1) == 'queued'
    assert orchestrator.schedule(corpus, 1, 2, verified_license=True) == 'queued'
    assert orchestrator.schedule(corpus, 2, 1) == 'queue_full'
    assert orchestrator.schedule(corpus, 2, 2, verified_license=True) == 'queue_full'
    assert orchestrator.schedule(corpus, 2, 1) == 'queue_full'
    assert orchestrator.schedule(corpus, 3, 1, raw_bytes=MAX_SOURCE_BYTES + 1) == 'source_too_large'
    assert corpus.preparation_states([{'id': 1}, {'id': 2}, {'id': 3, 'raw_bytes': MAX_SOURCE_BYTES + 1}]) == {
        1: 'queued', 2: 'queue_full', 3: 'source_too_large'}
    restarted = shared.SharedCorpusRuntime(corpus.root, models=corpus.models)
    assert restarted.capacity()['alerts'] == ['preparation_queue_full']
    assert restarted.capacity()['preparation_deferred'] == 1
    with restarted.journal.connection() as conn:
        admitted = conn.execute('SELECT owner, verified_license FROM shared_preparations ORDER BY candidate_id')
        assert [tuple(row) for row in admitted] == [(2, 1), (2, 1)]
    monkeypatch.setattr('src.auth.user_by_id', lambda _: None)
    orchestrator.process_one(restarted)
    orchestrator.process_one(restarted)
    assert restarted.capacity()['preparation_deferred'] == 0
    assert restarted.preparation_states([{'id': 1}, {'id': 2}]) == {1: 'expired', 2: 'expired'}


def test_index_capacity_alert_and_hard_limit(corpus, isolated, monkeypatch):
    stage(corpus, 21, isolated.owner)
    monkeypatch.setattr(shared, 'INDEX_WARNING_DOCUMENTS', 1)
    assert corpus.capacity()['alerts'] == ['index_document_limit_near']
    monkeypatch.setattr(shared, 'MAX_INDEX_DOCUMENTS', 1)
    assert corpus.capacity()['alerts'] == ['index_document_limit_reached']
    stage(corpus, 22, isolated.owner)
    assert corpus.capacity()['alerts'] == ['index_document_limit_exceeded']
    with pytest.raises(ValueError, match='shared_index_document_limit'):
        corpus.request_build(isolated.owner)
    assert corpus.journal.pending() is None
