"""Automatic admission is a private consequence of exact first-party evidence."""
import asyncio
from contextlib import contextmanager
from hashlib import md5, sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from types import ModuleType

import pytest
from urllib3.exceptions import ProtocolError

from citrus_corpus import originals
from citrus_corpus.auto_intake import AutoIntake
from citrus_corpus.intake import Intake
from citrus_corpus.queue import IntakeQueue, JOB_TTL, LEASE_SECONDS
from citrus_corpus.store import Store
from src import auth
from src.core import capability_policy as policy, tracing, web_failure_ledger
from src.corpus_integration import automatic_worker, fetch_submit, paper_source as source
from src.tools import deepseek_search_fetch as fetch

DOI = "10.1371/journal.pone.0154431"
PMID = "27148745"
PMC = "PMC4858230"
TITLE = "Spatial, Phylogenetic, Environmental and Biological Components of Variation in Extinction Risk: A Case Study Using Banksia."
RAW = (TITLE + "\r\n\r\nAbstract\r\n\r\n" + ("Original UTF-8 text: café. " * 100) + "\r\n\r\n").encode()
MEMBER = f"{PMC}.1"
TEXT = f"/{MEMBER}/{MEMBER}.txt"
ARTICLE = {"uid": PMID, "title": TITLE, "pubtype": ["Journal Article", "Research-article"],
           "fulljournalname": "PloS One", "articleids": [{"idtype": key, "value": value}
                for key, value in (("pubmed", PMID), ("doi", DOI), ("pmc", PMC))]}
LICENSE = {"doi": DOI, "pmid": int(PMID), "pmcid": PMC, "title": TITLE.rstrip("."), "version": 1,
           "is_retracted": False, "is_pmc_openaccess": True, "license_code": "CC BY",
           "text_url": f"s3://pmc-oa-opendata{TEXT}?md5={md5(RAW, usedforsecurity=False).hexdigest()}"}


def _mock_http(monkeypatch, *, article=None, license_patch=None, original=RAW, duplicate_version=False):
    calls = []

    @contextmanager
    def get(url, *, params, headers, timeout, stream, allow_redirects):
        assert stream and not allow_redirects and headers == {"Accept-Encoding": "identity"}
        assert 0 < timeout[0] <= 3 and 0 < timeout[1] <= 8
        calls.append(url)
        if url == source._NCBI + "esummary.fcgi":
            assert params == {"db": "pubmed", "id": PMID, "retmode": "json"}
            payload = {"result": {"uids": [PMID], PMID: ARTICLE if article is None else article}}
        elif url == source._CLOUD + "/":
            prefix = f"metadata/{PMC}."
            assert params == {"list-type": "2", "prefix": prefix, "max-keys": "2"}
            members = (1, 2) if duplicate_version else (1,)
            keys = "".join(f"<Contents><Key>{prefix}{v}.json</Key></Contents>" for v in members)
            payload = (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                       f"<Prefix>{prefix}</Prefix><IsTruncated>false</IsTruncated>{keys}</ListBucketResult>").encode()
        elif url == source._CLOUD + "/metadata/" + MEMBER + ".json":
            payload = {**LICENSE, **(license_patch or {})}
        elif url == source._CLOUD + TEXT:
            if isinstance(original, Exception):
                raise original
            payload = original
        else:
            pytest.fail(f"unexpected request: {url}")
        raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        response = type("Response", (), {"raw": BytesIO(raw), "status_code": 200,
                       "headers": {"Content-Length": str(len(raw))}})
        yield response

    monkeypatch.setattr(source.requests, "get", get)
    monkeypatch.setattr(source, "_pace_ncbi", lambda deadline: None)
    return calls


@pytest.fixture
def app_queue(tmp_path, monkeypatch):
    store = Store.initialize(tmp_path / "isolated")
    db = tmp_path / "accounts.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE users(user_id INTEGER, active INTEGER, approval_status TEXT, role TEXT)")
        conn.execute("INSERT INTO users VALUES(7,1,'approved','regular')")
        from src.core.account_admission import init_schema
        init_schema(conn)
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    monkeypatch.setattr(fetch, "_public_http_url_error", lambda url: None)
    monkeypatch.setattr(fetch, "_web_citation_ref", lambda: "W1")
    monkeypatch.setattr(web_failure_ledger, "record_fetch_failures", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy, "_revocations", frozenset)
    user_token = tracing._user_id.set(7)
    web_token = tracing._web_search_enabled.set(True)
    try:
        with policy.bind_policy(policy.CapabilityPolicy()):
            yield store
    finally:
        tracing._web_search_enabled.reset(web_token)
        tracing._user_id.reset(user_token)


def _consume(store):
    # A fresh consumer has no request owner or request-level opt-in. Its
    # persisted principal and current system policy are what matter.
    token = tracing._user_id.set(None)
    web_token = tracing._web_search_enabled.set(False)
    try:
        reopened = Store(store.root)
        return Intake(reopened).process_one(
            automatic=automatic_worker.AutomaticWorker(reopened.root, network_allowed=lambda: True))
    finally:
        tracing._web_search_enabled.reset(web_token)
        tracing._user_id.reset(token)


def _official(*, metadata=LICENSE, raw=RAW):
    return ({"url": source._CLOUD + TEXT, "raw_bytes": raw, "metadata_raw": json.dumps(metadata).encode()},
            "cc-by", source._CLOUD + "/metadata/" + MEMBER + ".json")


def test_dedicated_foreground_reuses_exact_original_and_checks_article(app_queue, monkeypatch):
    calls = _mock_http(monkeypatch)

    async def winner(*args):
        official = _official()
        row = {"ok": True, "url": source._CLOUD + TEXT, "content": RAW.decode(), "title": TITLE,
               "format": "pmc_cloud_text", "validity": "valid"}
        args[-1].append(row)
        return row, official

    monkeypatch.setattr(fetch, "_race_candidates", winner)
    rendered, result = asyncio.run(fetch.deepseek_fetch_paper.coroutine(doi=DOI, pmid=PMID))
    assert result["fetch_status"] == "full_text" and result["review_candidate"] is None
    assert "Original UTF-8 text" in rendered and "raw_bytes" not in str(result)
    assert calls == []
    before, = IntakeQueue(app_queue).recent()
    assert before["state"] == "queued" and before["path"] == "foreground"
    digest = sha256(RAW).hexdigest()
    assert before["original_sha256"] == digest
    assert before["source_rights"] == {"basis": "ncbi_pmc_cloud", "license": "cc-by"}
    assert before["rights_obtained"] is None and not before["prepared"]
    assert not before["bibliography_verified"] and before["indexed_generation"] is None
    assert originals.read_archive(app_queue.originals, digest) == RAW
    receipt = _consume(app_queue)
    assert receipt["state"] == "prepared" and receipt["raw_sha256"] == digest
    prepared, = IntakeQueue(app_queue).recent()
    assert prepared["bibliography_verified"] and prepared["prepared"] and prepared["rights"] == "granted"
    assert prepared["rights_obtained"] == {"event_id": 1, "basis": "ncbi_pmc_cloud", "license": "cc-by"}
    assert prepared["indexed_generation"] is None and prepared["published_generation"] is None
    assert calls == [source._NCBI + "esummary.fcgi"]
    assert AutoIntake(app_queue).lookup(doi=DOI)["source_url"] == f"https://doi.org/{DOI}"

    # A disconnected background request still finds current metadata, but the
    # same official version/hash does not redownload, re-extract or regrant.
    calls.clear()
    background = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    assert background["path"] == "background"
    monkeypatch.setattr("citrus_corpus.intake.extract_markdown",
                        lambda *args: pytest.fail("duplicate must not be extracted"))
    duplicate = _consume(app_queue)
    assert duplicate["version_id"] == receipt["version_id"]
    assert not any(url.endswith(".txt") for url in calls)
    with app_queue.read() as conn:
        assert conn.execute("SELECT count(*) FROM versions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM rights_events").fetchone()[0] == 1


def test_no_winner_durably_records_background_even_without_rights(app_queue, monkeypatch):
    calls = _mock_http(monkeypatch, license_patch={"license_code": "https://evil.example/cc-by"})

    async def failed(*args):
        args[-1].append({"ok": True, "format": "identifier_metadata", "url": "https://provider.example",
                         "content": '{"resultList":{"result":[{"doi":"invented"}]}}'})
        return None, None

    monkeypatch.setattr(fetch, "_race_candidates", failed)
    rendered, result = asyncio.run(fetch.deepseek_fetch_paper.coroutine(doi=DOI, pmid=PMID))
    assert result["fetch_status"] == "failed" and result["review_candidate"] is None
    assert "Literature review candidate" not in rendered and calls == []
    queued, = IntakeQueue(app_queue).recent()
    assert queued["state"] == "queued" and queued["path"] == "background"
    receipt = _consume(app_queue)
    assert receipt["state"] == "metadata_only" and receipt["id"] == queued["id"]
    metadata, = IntakeQueue(app_queue).recent()
    assert metadata["bibliography_verified"] and not metadata["prepared"]
    assert metadata["original_sha256"] is None and metadata["rights_obtained"] is None
    assert not any(url.endswith(".txt") for url in calls)
    assert not list(app_queue.originals.iterdir())
    assert AutoIntake(app_queue).lookup(doi=DOI)["title"] == TITLE


def test_background_fills_original_without_chat_or_foreground_proof(app_queue, monkeypatch):
    calls = _mock_http(monkeypatch)
    before = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID, proof={"content": "generated"})
    assert before["path"] == "background" and not calls
    completed = _consume(app_queue)
    assert completed["state"] == "prepared" and completed["id"] == before["id"]
    assert calls.count(source._CLOUD + TEXT) == 1
    assert originals.read_archive(app_queue.originals, sha256(RAW).hexdigest()) == RAW


def test_background_then_foreground_never_reencodes_or_republishes(app_queue, monkeypatch):
    from citrus_corpus.models import FusedRank
    from citrus_corpus.publication import Publisher
    from citrus_corpus.tests.corpus_test import FixtureEmbedder

    calls = _mock_http(monkeypatch)
    fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    background = _consume(app_queue)
    publisher = Publisher(app_queue, embedder=FixtureEmbedder(), reranker=FusedRank())
    active = publisher.publish()["generation"]
    assert IntakeQueue(app_queue).recent()[0]["published"]
    monkeypatch.setattr(publisher, "_build", lambda *_: pytest.fail("duplicate generation rebuild"))
    proof = source.verified_original(*_official(), doi=DOI, pmid=PMID)
    admitted = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID, proof=proof)
    assert admitted["path"] == "foreground"
    foreground = _consume(app_queue)
    assert foreground["version_id"] == background["version_id"]
    assert publisher.publish() == {"state": "active", "generation": active}
    assert calls.count(source._CLOUD + TEXT) == 1
    assert all(job["published"] for job in IntakeQueue(app_queue).recent())
    Intake(app_queue).catalog.decide(background["document_id"], background["version_id"], decision="revoked",
                                   actor="operator", reason="fixture revocation")
    assert all(job["rights"] == "revoked" and not job["published"] for job in IntakeQueue(app_queue).recent())


@pytest.mark.parametrize("mutation", ["body", "version", "rights", "identity", "html", "short"])
def test_foreground_malformed_proof_cannot_claim_original(mutation):
    patch = dict(LICENSE)
    raw = RAW
    if mutation == "body":
        raw += b"changed"
    elif mutation == "version":
        patch["version"] = 2
    elif mutation == "rights":
        patch["license_code"] = "CC BY-NC"
    elif mutation == "identity":
        patch["doi"] = "10.1234/other"
    elif mutation in {"html", "short"}:
        raw = b"<article>" + RAW if mutation == "html" else b"Short abstract"
        patch["text_url"] = f"s3://pmc-oa-opendata{TEXT}?md5={md5(raw, usedforsecurity=False).hexdigest()}"
    assert source.verified_original(*_official(metadata=patch, raw=raw), doi=DOI, pmid=PMID) is None


def test_disabled_producer_and_consumer_preserve_automatic_rows(app_queue, monkeypatch):
    receipt = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    queue = IntakeQueue(app_queue)
    claimed = queue.claim(automatic=True)
    assert claimed["id"] == receipt["id"]
    later = queue.now() + JOB_TTL + LEASE_SECONDS + 1
    monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "0")
    calls = _mock_http(monkeypatch)
    assert fetch_submit.submit(owner=7, doi="10.1234/disabled") is None
    worker = automatic_worker.AutomaticWorker(app_queue.root, network_allowed=lambda: True)
    assert not worker.enabled()
    paused = IntakeQueue(app_queue, now=lambda: later)
    assert Intake(app_queue, queue=paused).process_one(automatic=worker) is None
    assert paused.status(receipt["id"])["state"] == "leased"
    assert not calls
    with app_queue.read() as conn:
        assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 1
    monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "1")
    assert worker.enabled()
    assert Intake(app_queue, queue=paused).process_one(automatic=worker) is None
    assert paused.status(receipt["id"])["state"] == "expired"


def test_automatic_timeout_and_interrupted_lease_retry(app_queue, monkeypatch):
    submitted = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    now = [IntakeQueue(app_queue).now()]
    queue = IntakeQueue(app_queue, now=lambda: now[0])
    intake = Intake(app_queue, queue=queue)
    worker = automatic_worker.AutomaticWorker(app_queue.root, network_allowed=lambda: True)
    calls = _mock_http(monkeypatch, original=TimeoutError("fixture timeout"))
    first = intake.process_one(automatic=worker)
    assert first["id"] == submitted["id"] and first["state"] == "queued"
    assert first["attempts"] == 1 and first["error"] == "network_timeout"
    assert calls.count(source._CLOUD + TEXT) == 1 and not list(app_queue.originals.iterdir())
    assert intake.process_one(automatic=worker) is None
    now[0] += 16
    abandoned = queue.claim(automatic=True)
    assert abandoned["id"] == submitted["id"] and abandoned["attempts"] == 2
    now[0] += LEASE_SECONDS + 1
    calls = _mock_http(monkeypatch)
    completed = intake.process_one(automatic=worker)
    assert completed["id"] == submitted["id"] and completed["state"] == "prepared"
    assert completed["attempts"] == 3 and completed["error"] is None
    assert calls.count(source._CLOUD + TEXT) == 1
    assert originals.read_archive(app_queue.originals, completed["raw_sha256"]) == RAW
    with pytest.raises(ValueError, match="corpus_lease_lost"):
        queue.fail(abandoned, "worker_interrupted")


def test_pinned_legacy_worker_only_processes_legacy_rows(app_queue):
    path = os.getenv("CITRUS_TEST_OLD_QUEUE")
    if not path:
        pytest.skip("supply the sealed 6191802 consumer queue.py via CITRUS_TEST_OLD_QUEUE")
    source_bytes = Path(path).read_bytes()
    assert sha256(source_bytes).hexdigest() == "c2d6b1f8b5c73db52e7129a839a47a2d59da2e0e206593f2d4c47acca67ba548"
    old = ModuleType("citrus_corpus._legacy_queue_fixture")
    exec(compile(source_bytes, path, "exec"), old.__dict__)
    current = IntakeQueue(app_queue)
    clock = [current.now()]
    previous = old.IntakeQueue(app_queue, now=lambda: clock[0])
    first = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    legacy = previous.submit("local:legacy", {"kind": "saved", "path": "legacy.md", "format": "markdown"},
                             actor="operator", expected_sha256="1" * 64)
    assert previous.claim(network=True)["id"] == legacy["id"]
    assert previous.claim(network=True) is None
    leased = current.claim(automatic=True)
    assert leased["id"] == first["id"]
    clock[0] += JOB_TTL + LEASE_SECONDS + 1
    assert previous.claim(network=True) is None
    assert current.status(legacy["id"])["state"] == "expired"
    assert current.status(first["id"])["state"] == "leased"

    record = b"# Legacy source\n\nFixture addition using the old queue.\n"
    (app_queue.inbox / "new-legacy.md").write_bytes(record)
    legacy = previous.submit("local:new-legacy", {"kind": "saved", "path": "new-legacy.md", "format": "markdown"},
                             actor="operator", expected_sha256=sha256(record).hexdigest())
    next_worker = Intake(app_queue, queue=IntakeQueue(app_queue, now=lambda: clock[0]))
    result = next_worker.process_one()
    assert result["id"] == legacy["id"] and result["state"] == "prepared"
    assert originals.read_archive(app_queue.originals, result["raw_sha256"]) == record
    assert current.status(first["id"])["state"] == "leased"


@pytest.mark.parametrize("patch", [{"pubtype": ["Dataset", "Journal Article"]},
    {"pubtype": ["Published Erratum", "Journal Article"]},
    {"pubtype": ["Retracted Publication", "Journal Article"]},
    {"title": "Retraction: " + TITLE}, {"title": "Correction to: " + TITLE}, {"fulljournalname": ""}])
def test_exact_nonpaper_and_retracted_records_are_rejected_without_fallback(monkeypatch, patch):
    calls = _mock_http(monkeypatch, article={**ARTICLE, **patch})
    assert source.resolve(DOI, PMID, allowed=lambda: True) is None
    assert calls == [source._NCBI + "esummary.fcgi"]


def test_conflicting_doi_and_pmid_are_not_salvaged_by_doi(monkeypatch):
    calls = _mock_http(monkeypatch)
    assert source.resolve("10.1371/journal.pone.0000081", PMID, allowed=lambda: True) is None
    assert calls == [source._NCBI + "esummary.fcgi"]


@pytest.mark.parametrize("patch", [{"license_code": ["CC BY", "CC BY-NC"]},
    {"license_code": "CC BY-NC-ND"}, {"license_code": {"id": "CC BY"}},
    {"license_code": "https://untrusted.example/creativecommons.org/licenses/by/4.0/"},
    {"text_url": "s3://other-provider/path"}, {"pmid": 1}, {"doi": "10.1234/wrong"},
    {"pmcid": "PMC999999"}, {"version": 2}, {"is_pmc_openaccess": False}])
def test_ambiguous_license_or_cloud_identity_never_reads_body(monkeypatch, patch):
    calls = _mock_http(monkeypatch, license_patch=patch)
    record = source.resolve(DOI, PMID, allowed=lambda: True)
    assert record == {"doi": DOI, "pmid": PMID, "pmcid": PMC, "title": TITLE, "source_url": f"https://doi.org/{DOI}"}
    assert len(calls) == 3 and not any(url.endswith(".txt") for url in calls)


def test_pmc_retraction_blocks_metadata_but_integrity_failure_keeps_link(monkeypatch):
    calls = _mock_http(monkeypatch, license_patch={"is_retracted": True})
    assert source.resolve(DOI, PMID, allowed=lambda: True) is None
    assert len(calls) == 3
    calls = _mock_http(monkeypatch, original=RAW + b"changed")
    assert "raw" not in source.resolve(DOI, PMID, allowed=lambda: True)
    assert len(calls) == 4
    calls = _mock_http(monkeypatch, original=ProtocolError("incomplete body"))
    assert "raw" not in source.resolve(DOI, PMID, allowed=lambda: True)
    assert len(calls) == 4
    calls = _mock_http(monkeypatch, duplicate_version=True)
    assert "raw" not in source.resolve(DOI, PMID, allowed=lambda: True)
    assert len(calls) == 2


@pytest.mark.parametrize("denied", ["account", "epoch", "revocation", "after_network", "web_optin", "growth"])
def test_standalone_worker_revalidates_principal_and_live_controls(app_queue, monkeypatch, denied):
    calls = _mock_http(monkeypatch)
    if denied == "web_optin":
        tracing.set_web_search_enabled(False)
        assert fetch_submit.submit(owner=7, doi=DOI, pmid=PMID) is None
        assert _consume(app_queue) is None
        return
    receipt = fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    assert receipt and not calls
    if denied == "growth":
        monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "0")
        assert _consume(app_queue) is None
        assert IntakeQueue(app_queue).status(receipt["id"])["state"] == "queued"
        assert not calls
        return
    if denied == "account":
        with sqlite3.connect(auth.DB_PATH) as conn:
            conn.execute("UPDATE users SET active=0")
    elif denied == "epoch":
        with sqlite3.connect(auth.DB_PATH) as conn:
            conn.execute("UPDATE users SET active=0")
            conn.execute("UPDATE users SET active=1")
    elif denied == "revocation":
        monkeypatch.setattr(policy, "_revocations", lambda: frozenset({"bibliographic_lookup"}))
    else:
        get = source.requests.get

        @contextmanager
        def get_then_revoke(url, **options):
            with get(url, **options) as response:
                yield response
            if url == source._CLOUD + TEXT:
                with sqlite3.connect(auth.DB_PATH) as conn:
                    conn.execute("UPDATE users SET active=0")

        monkeypatch.setattr(source.requests, "get", get_then_revoke)
    result = _consume(app_queue)
    assert result["state"] == "failed" and result["error"] == "network_scope_denied"
    assert len(calls) == (4 if denied == "after_network" else 0)
    assert not list(app_queue.originals.iterdir())


@pytest.mark.parametrize("selector", ["pmcid", "title"])
@pytest.mark.parametrize("match", ["exact", "ambiguous", "wrong"])
def test_standalone_title_and_pmcid_prove_exact_unique_record(app_queue, monkeypatch, selector, match):
    original_calls = _mock_http(monkeypatch)
    get = source.requests.get
    authority_calls = []

    @contextmanager
    def authoritative(url, **options):
        if url != source._EUROPE_PMC:
            with get(url, **options) as response:
                yield response
            return
        term = options["params"]["query"]
        assert term == (f'PMCID:{PMC} AND SRC:MED' if selector == "pmcid" else f'TITLE:"{TITLE}" AND SRC:MED')
        authority_calls.append(term)
        row = {"source": "MED", "id": PMID, "doi": DOI, "pmid": PMID, "pmcid": PMC, "title": TITLE,
               "pubTypeList": {"pubType": ["journal article"]}, "journalInfo": {"journal": {"title": "PLoS One"}}}
        if match == "wrong":
            row["pmcid" if selector == "pmcid" else "title"] = "PMC999999" if selector == "pmcid" else "Similar title"
        rows = [row, row] if match == "ambiguous" else [row]
        raw = json.dumps({"hitCount": len(rows), "resultList": {"result": rows}}).encode()
        yield type("Response", (), {"raw": BytesIO(raw), "status_code": 200, "headers": {"Content-Length": str(len(raw))}})

    monkeypatch.setattr(source.requests, "get", authoritative)
    adapter = AutoIntake(app_queue)
    before = adapter.submit(owner=7, epoch=1, producer="literature", **{selector: PMC if selector == "pmcid" else TITLE})
    assert before["path"] == "background" and not authority_calls
    after = _consume(app_queue)
    assert after["state"] == ("prepared" if match == "exact" else "failed")
    assert len(authority_calls) == 1 and len(original_calls) == (3 if match == "exact" else 0)


def test_stable_identifiers_do_not_repair_or_extract_dirty_input():
    assert source.identifiers(DOI, "PMC27148745") == ("", "")
    assert source.identifiers("Find " + DOI, "") == ("", "")
    assert source.identifiers("", "", f"https://evil.example/{DOI}") == ("", "")
    assert source.identifiers("", "", f"https://doi.org/{DOI}") == (DOI, "")
    assert source.identifiers("", "", f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/") == ("", PMID)
