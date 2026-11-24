from hashlib import sha256
import json
from queue import Queue
import sqlite3

import pytest

from citrus_corpus.intake import Intake
from citrus_corpus.store import Store
from src.corpus_integration import fetch_submit
from src.corpus_integration.legacy_read import LegacyReader
from src.corpus_integration.migration import dry_run
from src.shared_corpus import catalog, journal, prepare


def test_fetch_observation_never_archives_or_fetches_inline(tmp_path, monkeypatch):
    from src.core import tracing
    from src.tools import deepseek_search_fetch as fetch

    root = Store.initialize(tmp_path / "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(root.root))
    monkeypatch.setattr(tracing, "get_user_id", lambda: 7)
    monkeypatch.setattr(fetch, "_enabled", lambda capability: capability == "web_fetch")
    monkeypatch.setattr(fetch_submit, "_ensure_consumer", lambda: True)
    monkeypatch.setattr(fetch_submit, "_pending", Queue(maxsize=1))

    content = "# A single permitted observation\n\nSource text containing a citation."
    winner = {"url": "https://a.example/study", "content": content}
    before = winner.copy()
    assert fetch._retain_paper(winner)["retention_status"] == "handoff_unconfirmed"
    assert winner == before and list(root.originals.iterdir()) == []
    with root.read() as conn:
        assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 0
    assert fetch._retain_paper(winner)["retention_status"] == "queue_full"
    assert fetch_submit._pending.get_nowait()[-1] is content
    assert fetch._retain_missing_paper(doi="", pmid="", title="", url="https://a.example") is None

    raw = b"A permitted original saved by the fixture."
    receipt = fetch_submit.SavedOriginal(7, "saved.txt", sha256(raw).hexdigest())
    (root.inbox / receipt.inbox_path).write_bytes(raw)
    with root.write():
        assert fetch._retain_paper(winner, saved_original=receipt)["retention_status"] == "handoff_busy"
    assert fetch._retain_paper(winner, saved_original=fetch_submit.SavedOriginal(8, "saved.txt", receipt.sha256))[
        "retention_status"] == "receipt_denied"
    accepted = fetch._retain_paper(winner, saved_original=receipt)
    assert accepted["retention_status"] == "accepted" and list(root.originals.iterdir()) == []
    assert Intake(root).queue.status(accepted["intake_id"])["state"] == "queued"
    assert Intake(root).process_one()["state"] == "prepared"
    monkeypatch.setattr(fetch, "_enabled", lambda capability: False)
    assert fetch._retain_paper(winner) is None


def test_legacy_citation_and_consistent_migration_dry_run(tmp_path):
    legacy = tmp_path / "legacy"
    old_catalog = catalog.Catalog(legacy)
    old_journal = journal.Journal(legacy)
    raw = "# Methods\n\nLegacy citrus trial methods.\n"
    digest = sha256(raw.encode()).hexdigest()
    metadata = {"document_id": "old", "version_id": "v1", "schema_version": 2, "title": "Legacy methods",
                "content_sha256": digest, "char_count": len(raw), "chunk_count": 1}
    prepare.write_document(old_catalog.documents, "old", "v1", metadata, raw)
    with old_journal.connection() as conn:
        conn.execute("""INSERT INTO shared_documents(document_id,version_id,candidate_id,proposal_id,content_sha256,
                     char_count,chunk_count,schema_version,prepared_at) VALUES ('old','v1',1,'p_1',?,?,1,2,0)""",
                     (digest, len(raw)))
        conn.execute("INSERT INTO shared_rights VALUES('old','v1','granted','admin_grant',1,'grant',0,0)")
        conn.execute("""INSERT INTO shared_builds (id,owner,fingerprint,documents,state,generation,created,updated)
                     VALUES ('build',1,'hash',?,'active','old_generation',0,0)""", (json.dumps([["old", "v1"]]),))
    journal.write_pointer(legacy, "old_generation")
    base = [tmp_path / f"base_{index}.json" for index in range(5)]
    for index, path in enumerate(base):
        path.write_text(str(index))
    export = dry_run(legacy, base)
    assert export["legacy_active_generation"] == "old_generation"
    assert export["copied_catalog_counts"]["shared_documents"] == 1
    assert export["sample"][0]["markdown_sha256"] == digest
    assert export["base_sha256"] == [sha256(path.read_bytes()).hexdigest() for path in base]
    assert journal.read_pointer(legacy) == "old_generation"
    reader = LegacyReader(legacy)
    assert reader.preview("old", generation="old_generation", version="v1", digest=digest,
                          start=11, length=12)["markdown"] == raw[11:23]
    with old_journal.connection() as conn:
        conn.execute("UPDATE shared_rights SET status='revoked'")
    with pytest.raises(LookupError):
        reader.preview("old", generation="old_generation", version="v1", digest=digest)


def test_disabled_sixth_corpus_preserves_base_search(monkeypatch):
    from src.tools import deepseek_search_fetch as fetch

    monkeypatch.delenv("CITRUS_CORPUS_READ", raising=False)
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setattr(fetch, "_enabled", lambda capability: capability == "local_rag")
    calls = []

    def base(query, families, limit, topics, **kwargs):
        calls.append(families)
        return "", {"query": query, "main_results": [{"text": "base"}], "local_status": "hit"}

    monkeypatch.setattr(fetch, "_base_hybrid_search", base)
    _, result = fetch._local_hybrid_search_impl("test", ["literature", "isolated_literature"],
                                               render=False, progress=False)
    assert calls == [["literature"]] and result["main_results"] == [{"text": "base"}]
    assert result["shared_search_status"] == "unavailable"


def test_admin_explicit_intake_returns_durable_receipt(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.core import capability_policy
    from src.corpus_integration import api_router

    store = Store.initialize(tmp_path / "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    monkeypatch.setattr(api_router, "require_admin", lambda user: user["role"] == "admin")
    monkeypatch.setattr(capability_policy, "allows", lambda name: False)
    app = FastAPI()
    app.include_router(api_router.router)

    @app.middleware("http")
    async def identify(request, call_next):
        request.state.user = {"active": True, "approval_status": "approved", "user_id": 7,
                              "role": request.headers.get("x-test-role", "regular")}
        return await call_next(request)

    with TestClient(app) as client:
        path = "/api/v2/isolated-corpus/admin/intake"
        assert client.post(path, json={"doi": "10.1234/work"}).status_code == 403
        admin = {"x-test-role": "admin"}
        assert client.post(path, json={"doi": "10.1234/work"}, headers=admin).status_code == 403
        response = client.post(path, headers=admin, json={"saved_path": "work.md", "sha256": "0" * 64})
        assert response.status_code == 202 and response.json()["accepted"] is True
        job_id = response.json()["job"]["id"]
        assert client.get(path + "/" + job_id, headers=admin).json()["state"] == "queued"
        assert Intake(store).process_one()["error"] == "source_missing"
        assert client.get(path + "/" + job_id, headers=admin).json()["state"] == "failed"
