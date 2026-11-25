"""Automatic admission is a private consequence of exact first-party evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import md5, sha256
from io import BytesIO
import json
from queue import Queue
import threading

import pytest

from citrus_corpus.auto_intake import AutoIntake
from citrus_corpus.store import Store
from src import auth
from src.core import capability_policy as policy, tracing, web_failure_ledger
from src.corpus_integration import fetch_submit, paper_source as source
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
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    monkeypatch.setattr(fetch_submit, "_ensure_consumer", lambda: True)
    monkeypatch.setattr(fetch_submit, "_pending", Queue(maxsize=1))
    monkeypatch.setattr(auth, "user_by_id", lambda uid: {"user_id": uid, "active": True, "approval_status": "approved"})
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


def _worker_receipt():
    queued = fetch_submit._pending.get_nowait()
    context, *job = queued
    assert threading.current_thread() is threading.main_thread()
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(context.run, fetch_submit._persist, *job).result(timeout=5)


def _winner(*args):
    row = {"ok": True, "url": "https://publisher.example/paper", "content": "An unrelated body.",
           "license": "CC0", "title": "Untrusted model title", "format": "publisher_html", "validity": "valid"}
    args[-1].append(row)
    return row


def test_dedicated_fetch_paper_registers_only_exact_original(app_queue, monkeypatch):
    calls = _mock_http(monkeypatch)
    monkeypatch.setattr(fetch, "_race_candidates", _winner)
    rendered, result = fetch.deepseek_fetch_paper.func(doi=DOI, pmid=PMID, title="Wrong title")
    assert result["fetch_status"] == "full_text" and result["review_candidate"] is None
    assert "An unrelated body." in rendered and "Literature review candidate" not in rendered
    assert not calls and not list(app_queue.originals.iterdir())
    receipt = _worker_receipt()
    digest = sha256(RAW).hexdigest()
    assert receipt["state"] == "queued"
    with app_queue.read() as conn:
        assert conn.execute("SELECT expected_sha256 FROM intake WHERE id=?", (receipt["id"],)).fetchone()[0] == digest
    assert len(calls) == 4 and receipt["title"] == TITLE
    from citrus_corpus import originals
    assert originals.read_archive(app_queue.originals, digest) == RAW
    assert AutoIntake(app_queue).lookup(doi=DOI)["source_url"] == f"https://doi.org/{DOI}"


def test_no_winner_can_register_exact_link_without_trusting_provider(app_queue, monkeypatch):
    calls = _mock_http(monkeypatch, license_patch={"license_code": "https://evil.example/cc-by"})

    def failed(*args):
        args[-1].append({"ok": True, "format": "identifier_metadata", "url": "https://provider.example",
                         "content": '{"resultList":{"result":[{"doi":"invented"}]}}'})

    monkeypatch.setattr(fetch, "_race_candidates", failed)
    rendered, result = fetch.deepseek_fetch_paper.func(doi=DOI, pmid=PMID)
    assert result["fetch_status"] == "failed" and result["review_candidate"] is None
    assert "Literature review candidate" not in rendered and not calls
    receipt = _worker_receipt()
    assert receipt["state"] == "metadata_only" and receipt["title"] == TITLE
    assert not any(url.endswith(".txt") for url in calls)
    assert not list(app_queue.originals.iterdir())
    with app_queue.read() as conn:
        assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 0


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
    calls = _mock_http(monkeypatch, duplicate_version=True)
    assert "raw" not in source.resolve(DOI, PMID, allowed=lambda: True)
    assert len(calls) == 2


@pytest.mark.parametrize("denied", ["account", "after_network", "revocation", "root", "web_optin", "growth"])
def test_worker_revalidates_captured_principal_and_live_controls(app_queue, monkeypatch, denied):
    calls = _mock_http(monkeypatch)
    if denied == "web_optin":
        tracing.set_web_search_enabled(False)
    fetch_submit.submit(owner=7, doi=DOI, pmid=PMID)
    if denied == "account":
        monkeypatch.setattr(auth, "user_by_id", lambda uid: {"user_id": uid, "active": True, "approval_status": "pending"})
    elif denied == "after_network":
        users = iter(({"user_id": 7, "active": True, "approval_status": "approved"}, None))
        monkeypatch.setattr(auth, "user_by_id", lambda uid: next(users))
    elif denied == "revocation":
        monkeypatch.setattr(policy, "_revocations", lambda: frozenset({"bibliographic_lookup"}))
    elif denied == "root":
        monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(app_queue.root.parent))
    elif denied == "growth":
        monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "0")
    assert _worker_receipt() is None
    assert len(calls) == (4 if denied == "after_network" else 0)
    assert not list(app_queue.originals.iterdir())


def test_stable_identifiers_do_not_repair_or_extract_dirty_input():
    assert source.identifiers(DOI, "PMC27148745") == ("", "")
    assert source.identifiers("Find " + DOI, "") == ("", "")
    assert source.identifiers("", "", f"https://evil.example/{DOI}") == ("", "")
    assert source.identifiers("", "", f"https://doi.org/{DOI}") == (DOI, "")
    assert source.identifiers("", "", f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/") == ("", PMID)
