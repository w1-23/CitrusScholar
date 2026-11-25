from hashlib import md5, sha256
import json
from pathlib import Path

import pytest
import requests

from src.api import chat_turn_queue_http_test as fixture
from src.core import corpus_review, tracing
from src.shared_corpus import pmc, prepare, rights, runtime as shared
from src.shared_corpus.orchestrator import process_one
from src.tools import deepseek_search_fetch as fetch

runtime = fixture.runtime
URL = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC100/'
DOI = '10.5555/synthetic-pmc'
TITLE = 'Synthetic text of a computational experiment'
BODY = (f'{TITLE}\n\nIntroduction\n\n' + 'Official text from the synthetic archive. ' * 80 + '\n\nMethods\n\nFull methods.\n\nResults\n\nEnd of complete text.\n').encode()
META = dict(pmcid='PMC100', version=1, doi=DOI, pmid=99, title=TITLE,
    is_retracted=False, is_pmc_openaccess=True, license_code='CC BY',
    text_url=f's3://pmc-oa-opendata/PMC100.1/PMC100.1.txt?md5={md5(BODY, usedforsecurity=False).hexdigest()}')
PROVIDER_BODY = '# Introduction\n\n' + 'Provider-rendered text. ' * 100 + '\n\n# Results\n\nComplete source.'


def mock_cloud(monkeypatch, *, meta=None, body=BODY, status=200, keys=(1,), truncated=False):
    calls=[]
    class Response:
        def __init__(self, data, status=200): self.data, self.status_code = data, status
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def iter_content(self, chunk_size):
            for start in range(0, len(self.data), chunk_size):
                yield self.data[start:start+chunk_size]
    def get(url, *, params, timeout, stream, allow_redirects):
        assert url.startswith(pmc._BASE + '/') and stream and not allow_redirects and timeout == (2, 3)
        calls.append(url)
        if url == pmc._BASE+'/':
            assert params == {'list-type':'2','prefix':'metadata/PMC100.','max-keys':'2'}
            entries = ''.join(f'<Contents><Key>metadata/PMC100.{v}.json</Key></Contents>' for v in keys)
            return Response((f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f'<Prefix>metadata/PMC100.</Prefix><IsTruncated>{str(truncated).lower()}</IsTruncated>'
                f'{entries}</ListBucketResult>').encode())
        if url.endswith('.json'): return Response(json.dumps(META if meta is None else meta).encode(), status)
        assert url == pmc._BASE+'/PMC100.1/PMC100.1.txt'
        return Response(body)
    monkeypatch.setattr(requests, 'get', get)
    return calls


@pytest.mark.parametrize('overrides', [dict(pmcid='PMC200'), dict(version=2),
    dict(is_retracted=True), dict(is_retracted=None), dict(is_pmc_openaccess=False),
    dict(license_code='CC BY-NC'), dict(license_code=None), dict(doi='10.5555/other'),
    dict(text_url='https://elsewhere.test/body'), dict(pmid=12)])
def test_wrong_identity_or_rights_cannot_fetch_body(monkeypatch, overrides):
    calls = mock_cloud(monkeypatch, meta={**META, **overrides})
    assert pmc.licensed_full_text(URL, doi=DOI, pmid='99', max_bytes=10000) is None
    assert len(calls) == 2


@pytest.mark.parametrize('kwargs', [dict(body=BODY+b'changed'), dict(status=302), dict(keys=(1,2)), dict(truncated=True)])
def test_transport_or_version_ambiguity_fails_closed(monkeypatch, kwargs):
    mock_cloud(monkeypatch, **kwargs)
    assert pmc.licensed_full_text(URL, doi=DOI, pmid='99', max_bytes=10000) is None


def test_versioned_source_and_response_cap(monkeypatch):
    calls = mock_cloud(monkeypatch)
    assert pmc.licensed_full_text(URL, doi=DOI, pmid='99', max_bytes=16) is None
    assert len(calls) == 3
    calls.clear()
    result = pmc.licensed_full_text(URL.rstrip('/')+'.1/', doi=DOI, pmid='99', max_bytes=10000)
    assert result[0]['content'].encode() == BODY and len(calls) == 2
    assert pmc.licensed_full_text(URL.replace('.gov', '.gov.example'), doi=DOI, pmid='99', max_bytes=10000) is None
    assert len(calls) == 2


def test_foreground_deadline_prevents_next_cloud_http_leg(monkeypatch):
    at = [100.0]
    calls = []

    class Listing:
        status_code = 200

        def __enter__(self): return self

        def __exit__(self, *_): pass

        def iter_content(self, chunk_size):
            yield (b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   b'<Prefix>metadata/PMC100.</Prefix><IsTruncated>false</IsTruncated>'
                   b'<Contents><Key>metadata/PMC100.1.json</Key></Contents></ListBucketResult>')
            at[0] = 101.0

    def get(url, *, timeout, **_kwargs):
        calls.append((url, timeout))
        return Listing()

    monkeypatch.setattr(pmc, 'monotonic', lambda: at[0])
    monkeypatch.setattr(requests, 'get', get)
    assert pmc.licensed_full_text(URL, doi=DOI, pmid='99', max_bytes=10000, deadline=101.0) is None
    assert calls == [(pmc._BASE+'/', (1.0, 1.0))]


def test_actual_retention_dedup_and_publication_rights(monkeypatch, runtime, tmp_path):
    monkeypatch.setattr(corpus_review, '_schema_ready', False)
    corpus = shared.SharedCorpusRuntime(tmp_path/'shared', quiet_sec=0)
    monkeypatch.setattr(shared, '_runtime', corpus)
    monkeypatch.setattr(tracing, 'get_user_id', lambda: runtime.owner)
    monkeypatch.setattr(fetch, '_local_paper_exists', lambda *a: False)
    mock_cloud(monkeypatch)
    winner = dict(url=URL, title=TITLE, format='pmc_html', content=PROVIDER_BODY)
    first = fetch._retain_paper(winner, doi=DOI, pmid='99')
    second = fetch._retain_paper(winner, doi=DOI, pmid='99')
    assert first['candidate_id'] == second['candidate_id'] and second['deduped']
    candidate = corpus_review.get_candidate(first['candidate_id'])
    path = Path(candidate['stored_path'])
    assert path.read_bytes() == BODY.rstrip(b'\n')
    evidence = json.loads((path.parent/'license.json').read_bytes())
    assert evidence['sha256_of_saved_bytes'] == sha256(path.read_bytes()).hexdigest()
    assert evidence['url'] == pmc._BASE+'/metadata/PMC100.1.json'
    process_one(corpus)
    second_owner = runtime.owner + 100
    proposal = corpus.catalog.propose(first['candidate_id'], second_owner)
    duplicate = corpus.catalog.prepare(proposal['proposal_id'], second_owner)
    authorized = corpus.catalog.publishable()
    assert len(authorized) == 1
    assert (duplicate['document_id'], duplicate['version_id']) == (
        authorized[0]['document_id'], authorized[0]['version_id'])
    meta, markdown = corpus.catalog.read_document(authorized[0]['document_id'], authorized[0]['version_id'])
    assert markdown.encode() == BODY and meta['license'] == 'cc-by' and meta['license_evidence'] == 'ncbi_pmc_cloud'
    assert prepare.load_archive_metadata({'source_kind':'fetch_paper','stored_path':str(path),'raw_sha256':evidence['sha256_of_saved_bytes']}, corpus_review._archive_root())['license_source_url'] == evidence['url']
    # Simulate a sidecar copied from a different document. Archive metadata alone cannot grant rights.
    evidence['sha256_of_saved_bytes'] = '0'*64
    (path.parent/'license.json').write_text(json.dumps(evidence))
    record = prepare.load_archive_metadata({'source_kind':'fetch_paper', 'stored_path':str(path), 'raw_sha256':sha256(path.read_bytes()).hexdigest()}, corpus_review._archive_root())
    assert record['sha256_of_saved_bytes'] == sha256(path.read_bytes()).hexdigest()
    assert rights.evaluate_source_rights(record)['status'] == 'needs_review'
    mock_cloud(monkeypatch, meta={**META,'is_retracted':True})
    pending = fetch._retain_paper(winner, doi=DOI, pmid='99')
    pending_path = Path(corpus_review.get_candidate(pending['candidate_id'])['stored_path'])
    assert pending_path.read_text() == PROVIDER_BODY and not (pending_path.parent/'license.json').exists()
    process_one(corpus)
    assert len(corpus.catalog.publishable()) == 1
