"""Offline regressions for the signed Fetch-to-Selfcorpus handoff."""
import asyncio
from base64 import b64decode
from hashlib import md5, sha256
import inspect
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from src import auth
from src.core import account_leases, capability_policy, tracing, web_failure_ledger
from src.corpus_integration import selfcorpus_intake as intake
from src.shared_corpus import pmc
from src.tools import deepseek_search_fetch as fetch

_DOI = "10.1234/intake-fixture"
_ARTICLE = "https://pmc.ncbi.nlm.nih.gov/articles/PMC123.1/"
_TEXT_PATH = "/PMC123.1/PMC123.1.txt"
_META_PATH = "/metadata/PMC123.1.json"
_RAW = ("Original introduction. Exact UTF-8 café. " * 80).encode() + b"\r\n"
_META = json.dumps({"doi": _DOI, "pmid": 7, "pmcid": "PMC123", "version": 1,
                    "title": "Verified source", "license_code": "CC BY", "is_retracted": False,
                    "is_pmc_openaccess": True,
                    "text_url": f"s3://pmc-oa-opendata{_TEXT_PATH}?md5={md5(_RAW, usedforsecurity=False).hexdigest()}"
                    }).encode()
_SECRET = b"synthetic-selfcorpus-test-key-only-42"
_REF = "req_" + "a" * 32


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    key = tmp_path / "intake.key"
    key.write_bytes(_SECRET)
    key.chmod(0o600)
    db = tmp_path / "accounts.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE users(user_id INTEGER, active INTEGER, approval_status TEXT, role TEXT)")
        conn.execute("INSERT INTO users VALUES(42,1,'approved','regular')")
        conn.execute("CREATE TABLE account_admission(owner INTEGER, epoch INTEGER, deleting INTEGER, claim_token TEXT)")
        conn.execute("INSERT INTO account_admission VALUES(42,5,0,NULL)")
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setenv("CITRUS_SELFCORPUS_RETRIEVAL", "1")
    monkeypatch.setenv("CITRUS_SELFCORPUS_AUTO_INTAKE", "1")
    monkeypatch.setenv("CITRUS_SELFCORPUS_URL", "http://127.0.0.1:8061")
    monkeypatch.setenv("CITRUS_SELFCORPUS_SECRET_FILE", str(key))
    monkeypatch.setattr(tracing, "get_user_id", lambda: 42)
    monkeypatch.setattr(tracing, "web_search_enabled", lambda: True)
    monkeypatch.setattr(capability_policy, "_revocations", frozenset)
    monkeypatch.setattr(account_leases, "current_account_lease", lambda: SimpleNamespace(owner=42, epoch=5))
    return db


def _offline_http(monkeypatch, send):
    client = httpx.AsyncClient

    class Stream(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            yield self.data

    async def transport(request):
        response = send(request)
        if inspect.isawaitable(response):
            response = await response
        return httpx.Response(response.status_code, headers=response.headers, stream=Stream(response.content))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(transport)))


def _official(raw=_RAW):
    return {"url": pmc._BASE + _TEXT_PATH, "raw_bytes": raw, "metadata_raw": _META}, "cc-by", pmc._BASE + _META_PATH


def _queued(scope="personal:42"):
    return {"scope_ref": scope, "request_ref": _REF, "operation_id": _REF, "status": "queued",
            "output": {"private": "not projected"}}


def test_only_exact_signed_personal_proof_and_stable_doi_idempotency(monkeypatch, fixture):
    from corpus.auth import INGEST_TARGET, verify_signed_actor

    requests = []

    def respond(request):
        assert request.url.host == "127.0.0.1" and request.url.path == INGEST_TARGET
        assert request.method == "POST"
        owner = verify_signed_actor(_SECRET, "POST", INGEST_TARGET, request.content, request.headers)
        assert owner.subject == "42" and not owner.shared and not owner.governor and not owner.projects
        payload = json.loads(request.content)
        assert payload["scope_ref"] == "personal:42"
        spec = payload["spec"]
        assert spec == {"kind": "verified_text", "doi": _DOI, "pmid": "7", "pmcid": "PMC123",
                        "title": "Verified source", "license_id": "cc-by", "sha256": sha256(_RAW).hexdigest(),
                        "text_url": pmc._BASE + _TEXT_PATH, "evidence_url": pmc._BASE + _META_PATH,
                        "content_b64": spec["content_b64"], "metadata_b64": spec["metadata_b64"]}
        assert b64decode(spec["content_b64"]) == _RAW and b64decode(spec["metadata_b64"]) == _META
        requests.append(payload)
        return httpx.Response(202, json=_queued())

    _offline_http(monkeypatch, respond)
    expected = {"status": "queued", "request_ref": _REF}
    with capability_policy.bind_policy(capability_policy.CapabilityPolicy()):
        assert intake.submit(_official(), doi=_DOI, pmid="") == expected
        assert intake.submit(_official(), doi="", pmid="7") == expected
    assert requests[0] == requests[1]
    identity = (_DOI, pmc._BASE + _META_PATH, pmc._BASE + _TEXT_PATH,
                sha256(_RAW).hexdigest(), sha256(_META).hexdigest())
    assert requests[0]["idempotency_key"] == "fetch-pmc-v1:" + sha256(
        json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("case", ["disabled", "wrong_owner", "no_lease", "epoch_changed", "account_disabled",
                                  "no_network", "no_bibliography", "no_local", "mismatched_doi", "no_identity",
                                  "vendor_only", "changed_original"])
def test_unverified_or_unauthorized_inputs_never_post(monkeypatch, fixture, case):
    policy = capability_policy.CapabilityPolicy(**{
        "no_network": {"external_data_network": False}, "no_bibliography": {"bibliographic_lookup": False},
        "no_local": {"local_rag": False},
    }.get(case, {}))
    if case == "disabled":
        monkeypatch.delenv("CITRUS_SELFCORPUS_AUTO_INTAKE")
    elif case in {"wrong_owner", "no_lease"}:
        monkeypatch.setattr(account_leases, "current_account_lease",
                            lambda: None if case == "no_lease" else SimpleNamespace(owner=99, epoch=5))
    elif case in {"epoch_changed", "account_disabled"}:
        with sqlite3.connect(fixture) as conn:
            conn.execute("UPDATE account_admission SET epoch=6" if case == "epoch_changed" else "UPDATE users SET active=0")
    official = None if case == "vendor_only" else _official(_RAW + b"miss" if case == "changed_original" else _RAW)
    doi = {"no_identity": "", "mismatched_doi": "10.1234/something-else"}.get(case, _DOI)
    _offline_http(monkeypatch, lambda request: pytest.fail("unexpected signed network request"))
    with capability_policy.bind_policy(policy):
        assert intake.submit(official, doi=doi, pmid="")["status"] == "not_run"


@pytest.mark.parametrize("status,response,expected", [
    (409, {"error": {"code": "already_present"}}, {"status": "already_present"}),
    (409, {"error": {"code": "already_pending"}}, {"status": "already_pending"}),
    (409, {"error": {"code": ["already_pending"]}}, {"status": "unavailable", "reason": "selfcorpus_invalid_response"}),
    (409, {"error": {"code": "idempotency_conflict", "message": "private"}},
     {"status": "unavailable", "reason": "selfcorpus_idempotency_conflict"}),
    (202, _queued("personal:99"), {"status": "unavailable", "reason": "selfcorpus_invalid_response"}),
    (202, {"error": {"code": "already_present"}}, {"status": "unavailable", "reason": "selfcorpus_invalid_response"}),
    (503, {}, {"status": "unavailable", "reason": "selfcorpus_unavailable"}),
])
def test_queue_responses_fail_explicitly_without_fallback(monkeypatch, fixture, status, response, expected):
    _offline_http(monkeypatch, lambda request: httpx.Response(status, json=response))
    with capability_policy.bind_policy(capability_policy.CapabilityPolicy()):
        assert intake.submit(_official(), doi=_DOI, pmid="") == expected


@pytest.mark.parametrize("official_finishes", [True, False])
def test_fast_vendor_leaves_short_grace_for_inflight_official_proof(monkeypatch, fixture, official_finishes):
    calls = []
    order = []

    async def inline_authorized(lease, fn, *args, executor):
        return await asyncio.to_thread(fn, *args)

    monkeypatch.setattr(account_leases, "run_account_thread", inline_authorized)
    monkeypatch.setattr(fetch, "_headers", lambda: {})
    monkeypatch.setattr(fetch, "_public_http_url_error", lambda _: None)
    monkeypatch.setattr(fetch, "_web_citation_ref", lambda: "W1")
    monkeypatch.setattr(fetch, "_OFFICIAL_PROOF_GRACE_SECONDS", 0.2 if official_finishes else 0.04)
    monkeypatch.setattr(web_failure_ledger, "record_fetch_failures", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "_queue_paper_registration", lambda *a, **k: pytest.fail("old intake called"))

    async def respond(request):
        calls.append(request.url.path)
        if request.url.host == "127.0.0.1":
            return httpx.Response(202, json=_queued())
        if request.url.path == _META_PATH:
            try:
                if official_finishes:
                    await asyncio.sleep(0.02)
                else:
                    await asyncio.Future()
            finally:
                order.append("official_metadata_stopped")
            return httpx.Response(200, content=_META)
        if request.url.path == _TEXT_PATH:
            return httpx.Response(200, content=_RAW)
        order.append("vendor_valid")
        body = _DOI + "\n# Introduction\n\n" + "Vendor-rendered evidence. " * 100 + "\n# Results\n\nEnd."
        return httpx.Response(200, json={"title": "Vendor paper", "content": body})

    _offline_http(monkeypatch, respond)
    with capability_policy.bind_policy(capability_policy.CapabilityPolicy()):
        message, artifact = asyncio.run(fetch.deepseek_fetch_paper.coroutine(doi=_DOI, urls=[_ARTICLE]))
    assert artifact["fetch_status"] == "full_text" and artifact["review_candidate"] is None
    assert calls[0] == _META_PATH and order == ["vendor_valid", "official_metadata_stopped"]
    assert "content_b64" not in str(artifact) and "metadata_b64" not in str(artifact)
    if official_finishes:
        assert "Verified PMC Cloud source" in message
        assert artifact["corpus_intake"] == {"status": "queued", "request_ref": _REF}
        assert calls.count(_TEXT_PATH) == calls.count("/api/v1/ingestion/requests") == 1
    else:
        assert "Vendor-rendered evidence" in message
        assert artifact["corpus_intake"] == {"status": "not_run", "reason": "selfcorpus_verified_source_required"}
        assert calls == [_META_PATH, "/v1/tools/fetch"]
