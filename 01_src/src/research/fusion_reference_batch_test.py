from collections import Counter
from dataclasses import replace
from hashlib import sha256
import json
from threading import Lock
from types import SimpleNamespace
import time

import pytest

from src.agent import fusion_tools, fusion_worker
from src.research import fusion_observations, fusion_reference_batch, literature


REFERENCES = [
    {"doi": "https://doi.org/10.5555/known", "title": "Known registry title"},
    {"doi": "DOI: 10.5555/known"},
    {"doi": "10.5555/conflict", "arxiv_id": "arXiv:2601.00001v2", "title": "Submitted registry title"},
    {"doi": "10.5555/retracted"},
    {"title": "OpenReview-only paper", "venue": "OpenReview", "author": "Fixture, A", "year": "2025"},
    {"pmid": "https://pubmed.ncbi.nlm.nih.gov/101/"},
    {"pmid": "PMID: 102"},
    {"doi": "10.5555/source-down"},
    {"doi": "10.5555/missing"},
    {},
]


def install_fixture(monkeypatch):
    calls = Counter()
    lock = Lock()

    def lookup(url, *, params=None, source):
        key = (source, url, json.dumps(params, sort_keys=True))
        with lock:
            calls[key] += 1
        time.sleep(.01)
        name = url.rsplit("/", 1)[-1]
        if source == "crossref":
            doi = "10.5555/" + name
            if name == "source-down":
                return 503, None, "http_503"
            if name == "missing":
                return 404, None, ""
            if name == "works":
                return 200, {"message": {"items": []}}, ""
            title = {"known": "Known registry title", "conflict": "Other registered title",
                     "retracted": "Retracted registry record"}[name]
            row = {"DOI": doi, "title": [title]}
            if name == "retracted":
                row["updated-by"] = [{"type": "retraction"}]
            return 200, {"message": row}, ""
        if source == "datacite" and "arxiv.2601.00001" in url.lower():
            return 200, {"data": {"attributes": {"doi": "10.48550/arxiv.2601.00001",
                "titles": [{"title": "Submitted registry title"}]}}}, ""
        if source == "epmc":
            if "102" in params["query"]:
                return 429, None, "http_429"
            return 200, {"resultList": {"result": [{"pmid": "101", "title": "PMID fixture"}]}}, ""
        if name == "works" and source == "openalex":
            return 200, {"results": []}, ""
        return 404, None, ""

    monkeypatch.setattr(literature, "_get", lookup)
    return calls


def test_ten_records_one_bounded_batch_reaches_external_worker(monkeypatch, tmp_path):
    calls = install_fixture(monkeypatch)
    monkeypatch.setattr(fusion_observations, "_policy_check", lambda: None)
    identity = SimpleNamespace(owner_user_id=7)
    epochs = []
    def acquire(owner, *, expected_epoch):
        assert (owner, expected_epoch) == (7, 4)
        epochs.append(expected_epoch)
        return SimpleNamespace(release=lambda: None)
    monkeypatch.setattr("src.core.account_leases.acquire_account_lease", acquire)
    monkeypatch.setattr(fusion_tools, "_bound", lambda _skill: (identity, "call", tmp_path))
    monkeypatch.setattr(fusion_tools, "_literature_coverage", lambda *_a, **_k: (None, {}))
    monkeypatch.setattr("src.agent.code_tools._deadline", lambda _: time.time() + 15)
    raw_records = [{"record_index": index, "submitted": row, "status": "not_processed"}
                   for index, row in enumerate(REFERENCES, start=1)]
    extraction = {"records": raw_records, "execution": {"version": "0.1.2", "inputs": []}}
    digest, root_id, child_id = "f" * 64, "a" * 32, "b" * 32
    delivered = {}

    def schedule(_identity, **kwargs):
        received = kwargs["observations"]["reference_batch"]
        assert kwargs["inputs"] == {"bibliography": {"file_id": "c" * 32, "sha256": "d" * 64}}
        assert received["original_report_sha256"] == digest
        assert kwargs["options"] == {"strict": True}
        assert not delivered
        delivered.update(received)
        return {"execution_id": child_id, "status": "queued"}

    def consume(_identity, *, execution_id, **kwargs):
        if execution_id == child_id:
            return {"status": "succeeded", "execution_id": child_id, "report": {
                "records": delivered["records"], "status": "partial", "execution": {"version": "0.1.2",
                    "inputs": [{"role": "observations"}]}}}
        return {"status": "succeeded", "report": extraction, "receipt": {"result": {"sha256": digest}}, "_account_epoch": 4,
                "inputs": {"bibliography": {"file_id": "c" * 32, "sha256": "d" * 64}}, "options": {"strict": True}}

    class Coverage:
        state = None
        child = ""

        def __init__(self, _identity):
            pass

        def get(self, _execution_id, _digest):
            return int(self.state is not None), self.child, self.state

        def advance(self, _execution_id, _digest, revision, state):
            assert revision == 0
            type(self).state = state

        def bind_child(self, _execution_id, _digest, revision, child, state):
            assert revision == 1 and self.state is state
            type(self).child = child

    cache = {}

    def once(self, key, operation):
        with lock_guard:
            already = key in cache
        if not already:
            result = operation()
            with lock_guard:
                cache[key] = result
        return cache[key]

    lock_guard = Lock()
    monkeypatch.setattr(fusion_observations.RegistryReceipts, "send", once)
    monkeypatch.setattr(fusion_observations, "RegistryCoverage", Coverage)
    monkeypatch.setattr(fusion_worker, "schedule_fusion", schedule)
    monkeypatch.setattr(fusion_worker, "consume_fusion", consume)
    monkeypatch.setattr(fusion_worker, "find_fusion", lambda *_a, **_k: None)
    first = json.loads(fusion_tools.continue_external_literature.func(root_id)[0])
    assert first["execution_id"] == child_id
    second = json.loads(fusion_tools.continue_external_literature.func(root_id)[0])
    assert len(second["records"]) == 10
    assert [row["status"] for row in second["records"]] == ["verified", "verified", "metadata_conflict", "retracted",
        "unconfirmed", "verified", "source_unavailable", "source_unavailable", "not_found", "not_processed"]
    assert second["records"][2]["identifier_checks"]["doi"]["registered"]["title"] == "Other registered title"
    assert second["records"][2]["identifier_checks"]["arxiv"]["registered"]["title"] == "Submitted registry title"
    assert second["records"][4]["reason"] == "openreview_direct_source_not_checked"
    assert second["records"][6]["attempts"][0]["http_status"] == 429
    assert second["records"][7]["attempts"][0]["http_status"] == 503
    assert max(calls.values()) == 1
    assert delivered["distinct_queries"] == 9
    assert delivered["coverage"] == {"requested": 10, "actual": 9, "gap": 1}
    assert delivered["provider_requests"] == len(calls)
    assert len(epochs) == len(calls) + 2  # outer continuation plus each new transport and final read
    print(json.dumps({key: delivered[key] for key in ("coverage", "distinct_queries", "provider_requests", "elapsed_ms")}))


def test_unknown_receipt_is_distinct_and_never_retried(monkeypatch):
    calls = install_fixture(monkeypatch)
    records = [{"record_index": 1, "submitted": REFERENCES[0]}]
    outcome = fusion_reference_batch.run_reference_batch(records, check=lambda: None,
        send=lambda *_: None, deadline=time.monotonic() + 3)
    assert not calls and outcome["provider_requests"] == 0
    assert outcome["records"][0]["status"] == "source_unavailable"
    assert [attempt["reason"] for attempt in outcome["records"][0]["attempts"]] == ["dispatch_unknown"] * 3


def test_revoked_bibliographic_capability_blocks_batch_before_dispatch(monkeypatch):
    from citrus_refcheck.lookup import AuditStopped
    from src.core.capability_policy import CapabilityPolicy, bind_policy

    calls = install_fixture(monkeypatch)
    records = [{"record_index": 1, "submitted": REFERENCES[0]}]
    policy = CapabilityPolicy(bibliographic_lookup=False, web_search=True)
    with bind_policy(policy), pytest.raises(AuditStopped, match="network_disabled"):
        fusion_observations.batch_observations({"records": records}, identity=object(), account_epoch=4,
            deadline=time.monotonic() + 5, original_report_sha256="f" * 64)
    assert not calls


def test_wrong_pmid_or_arxiv_identity_stays_unconfirmed(monkeypatch):
    def lookup(url, *, params=None, source):
        if source == 'epmc':
            return 200, {'resultList': {'result': [{'pmid': '999', 'title': 'Other work'}]}}, ''
        return 200, {'data': {'attributes': {'doi': '10.48550/arxiv.2401.11111', 'titles': [{'title': 'Other work'}]}}}, ''

    monkeypatch.setattr(literature, '_get', lookup)
    submitted = [{'pmid': '101'}, {'arxiv_id': '2401.12345'}]
    rows = [{'record_index': index, 'submitted': value} for index, value in enumerate(submitted, 1)]
    checked = fusion_reference_batch.run_reference_batch(rows, check=lambda: None, send=lambda _, op: op(),
                                                          deadline=time.monotonic() + 3)
    assert [(row['status'], row['reason']) for row in checked['records']] == [
        ('unconfirmed', 'identifier_not_returned'), ('unconfirmed', 'identifier_not_returned')]


@pytest.mark.parametrize("crossref_status", [503, 429, 404, None])
def test_alternate_registry_preserves_failure_receipt_and_compares_each_claim(monkeypatch, crossref_status):
    calls = Counter()
    doi = "10.5555/alternate"

    def lookup(url, *, params=None, source):
        assert url.endswith(doi)
        calls[source] += 1
        if source == "crossref":
            assert crossref_status is not None
            return crossref_status, None, "" if crossref_status == 404 else f"http_{crossref_status}"
        if source == "datacite":
            return 200, {"data": {"attributes": {"doi": doi, "titles": [{"title": "Verified fixture"}]}}}, ""
        assert source == "openalex"
        return 404, None, ""

    monkeypatch.setattr(literature, "_get", lookup)

    def once(key, operation):
        if json.loads(key)[1] == "Crossref" and crossref_status is None:
            return None
        return operation()

    records = [{"record_index": 1, "submitted": {"doi": f"https://doi.org/{doi}"}},
               {"record_index": 2, "submitted": {"doi": f"DOI: {doi}", "title": "A different claim"}}]
    result = fusion_reference_batch.run_reference_batch(records, check=lambda: None, send=once,
                                                        deadline=time.monotonic() + 3)
    assert [row["status"] for row in result["records"]] == ["verified", "metadata_conflict"]
    first, second = result["records"]
    assert first["registered"]["doi"] == doi
    assert first["registered"]["source"] == "DataCite"
    assert first["attempts"] == second["attempts"]
    assert [(row["provider"], row["http_status"]) for row in first["attempts"]] == [
        ("Crossref", crossref_status or 0), ("DataCite", 200), ("OpenAlex", 404)]
    expected = {None: "dispatch_unknown", 404: "", 429: "http_429", 503: "http_503"}
    assert first["attempts"][0]["reason"] == expected[crossref_status]
    assert bool(first["attempts"][0]["queried_at"]) == (crossref_status is not None)
    assert len({row["record_id"] for row in first["attempts"]}) == 3
    assert calls == {"datacite": 1, "openalex": 1, **({"crossref": 1} if crossref_status is not None else {})}
    assert result["distinct_queries"] == 1 and result["provider_requests"] == sum(calls.values())
    assert result["coverage"] == {"requested": 2, "actual": 2, "gap": 0}


def test_provider_cooldown_does_not_suppress_independent_doi_queries(monkeypatch):
    real_limits = fusion_reference_batch.BatchLimits
    monkeypatch.setattr(fusion_reference_batch, "BatchLimits",
                        lambda **kwargs: replace(real_limits(**kwargs), concurrency=1))
    calls = []

    def lookup(url, *, params=None, source):
        calls.append((source, url.rsplit("/", 1)[-1]))
        if source == "crossref":
            return 429, None, "http_429"
        if source == "openalex" and url.endswith("/reachable"):
            return 200, {"doi": "https://doi.org/10.5555/reachable", "title": "Exact alternate"}, ""
        return 404, None, ""

    monkeypatch.setattr(literature, "_get", lookup)
    records = [{"record_index": index, "submitted": {"doi": f"10.5555/{name}"}}
               for index, name in enumerate(("limited", "reachable"), 1)]
    result = fusion_reference_batch.run_reference_batch(records, check=lambda: None, send=lambda _, op: op(),
                                                        deadline=time.monotonic() + 3)
    first, second = result["records"]
    assert first["status"] == "source_unavailable" and first["attempts"][0]["http_status"] == 429
    assert second["status"] == "verified" and second["registered"]["source"] == "OpenAlex"
    assert second["attempts"][0]["reason"] == "rate_limit_cooldown"
    assert [(source, suffix) for source, suffix in calls if source == "crossref"] == [("crossref", "limited")]
    assert result["distinct_queries"] == 2 and result["provider_requests"] == 5


def test_wrong_doi_alternate_does_not_hide_failed_source(monkeypatch):
    def lookup(url, *, params=None, source):
        if source == "crossref":
            return 503, None, "http_503"
        if source == "datacite":
            return 200, {"data": {"attributes": {"doi": "10.5555/other", "titles": [{"title": "Wrong work"}]}}}, ""
        assert source == "openalex"
        return 404, None, ""

    monkeypatch.setattr(literature, "_get", lookup)
    result = fusion_reference_batch.run_reference_batch(
        [{"record_index": 1, "submitted": {"doi": "10.5555/intended"}}],
        check=lambda: None, send=lambda _, op: op(), deadline=time.monotonic() + 3)
    row = result["records"][0]
    assert row["status"] == "source_unavailable" and row["registered"] == {}
    assert [attempt["http_status"] for attempt in row["attempts"]] == [503, 200, 404]


def test_alternate_registries_obey_provider_request_budget(monkeypatch):
    calls = []

    def lookup(url, *, params=None, source):
        calls.append(source)
        return (503, None, "http_503") if source == "crossref" else (404, None, "")

    monkeypatch.setattr(literature, "_get", lookup)
    monkeypatch.setattr(fusion_reference_batch, "MAX_REQUESTS", 2)
    result = fusion_reference_batch.run_reference_batch(
        [{"record_index": 1, "submitted": {"doi": "10.5555/intended"}}],
        check=lambda: None, send=lambda _, op: op(), deadline=time.monotonic() + 3)
    assert calls == ["crossref", "datacite"]
    assert result["stop_reason"] == "request_budget_exhausted"
    assert result["records"][0]["status"] == "not_processed"
    assert result["coverage"] == {"requested": 1, "actual": 0, "gap": 1}
    assert result["provider_requests"] == 2
