"""Pending-only legacy import uses exact stored revisions and never grants rights."""
from contextlib import closing
from hashlib import sha256
import fcntl
import json
from pathlib import Path
import sqlite3

import pytest

from citrus_corpus import originals, prepare
from citrus_corpus.catalog import Catalog
from citrus_corpus.store import Store
from src.shared_corpus import chunks as old_chunks
from src.shared_corpus.prepare import write_document
from .pending_migration import migrate_pending


def _fixture(tmp_path):
    root, archive = tmp_path / "legacy", tmp_path / "archive"
    root.mkdir()
    (root / ".web.lock").touch(mode=0o600)
    archive.mkdir()
    db = root / "catalog.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE shared_activity (id, last_activity);
            CREATE TABLE shared_audit (seq, action);
            CREATE TABLE shared_build_events (seq);
            CREATE TABLE shared_builds (seq);
            CREATE TABLE shared_rights (seq);
            CREATE TABLE shared_documents (document_id, version_id, candidate_id, proposal_id,
                source_kind, content_sha256, doi, prepared_at, superseded_by);
            CREATE TABLE shared_preparations (candidate_id, owner, state);
            CREATE TABLE shared_proposals (proposal_id, candidate_id, document_id, version_id,
                owner_user_id, state, rights, candidate_raw_sha256);
            INSERT INTO shared_audit VALUES (1, 'propose'), (2, 'prepare_needs_review');
            INSERT INTO shared_preparations VALUES (5, 42, 'prepared');
        """)
        raw = b"# A reproducible methods note\r\n\r\nA pending research work."
        markdown = raw.decode().replace("\r\n", "\n") + "\n"
        raw_hash, digest = sha256(raw).hexdigest(), sha256(markdown.encode()).hexdigest()
        source_text = sha256(b"Original upstream extracted text.").hexdigest()
        write_document(root / "documents", "old_doc", "old_v", {
            "schema_version": old_chunks.SCHEMA_VERSION, "document_id": "old_doc", "version_id": "old_v",
            "title": "A methods note", "source_sha256": source_text, "content_sha256": digest,
            "char_count": len(markdown), "prepared_at": "2026-11-24T00:00:00Z",
        }, markdown)
        conn.execute("INSERT INTO shared_documents VALUES ('old_doc','old_v',5,'p5','fetch_paper',?,?,10,NULL)",
                     (digest, "10.1234/methods"))
        conn.execute("INSERT INTO shared_proposals VALUES ('p5',5,'old_doc','old_v',42,'needs_review','needs_review',?)",
                     (raw_hash,))
    historical = archive / "historical"
    historical.mkdir()
    (historical / "paper.md").write_bytes(raw)
    (historical / "meta.json").write_text(json.dumps({"sha256_of_saved_bytes": raw_hash,
        "sha256_of_source_text": source_text, "raw_format": "plain_text"}))
    replacement = archive / "replacement"
    replacement.mkdir()
    (replacement / "paper.md").write_bytes(b"different, newer candidate source")
    (replacement / "meta.json").write_text(json.dumps({"sha256_of_saved_bytes": sha256(
        (replacement / "paper.md").read_bytes()).hexdigest()}))
    return root, archive, db, raw, markdown


def test_pending_migration_preserves_identifiers_bytes_and_review_gate(tmp_path):
    root, archive, database, raw, markdown = _fixture(tmp_path)
    before = database.read_bytes()
    target = tmp_path / "new"
    result = migrate_pending(root, archive)
    assert result["legacy_counts"]["shared_audit"] == 2
    assert not target.exists()
    with pytest.raises(ValueError, match="legacy_plan_changed"):
        migrate_pending(root, archive, target=target, expected_plan="wrong")
    assert not target.exists()
    with (root / ".web.lock").open("rb") as live_writer:
        fcntl.flock(live_writer, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="legacy_web_writer_still_active"):
            migrate_pending(root, archive, target=target, expected_plan=result["plan_sha256"])
    assert not target.exists()
    applied = migrate_pending(root, archive, target=target, expected_plan=result["plan_sha256"])
    assert applied == {**result, "state": "imported_pending"}
    entry, = result["versions"]
    assert entry["legacy_document_id"] == "old_doc"
    assert entry["legacy_version_id"] == "old_v"
    assert entry["original_relpath"] == "historical/paper.md"
    provenance = (target / "legacy-provenance" / (entry["raw_sha256"] + ".json")).read_bytes()
    assert sha256(provenance.rstrip(b"\n")).hexdigest() == entry["provenance_sha256"]
    store = Store(target)
    catalog = Catalog(store)
    doc, version = entry["document_id"], entry["version_id"]
    assert originals.read_archive(store.originals, entry["raw_sha256"]) == raw
    assert prepare.read_document_files(store.documents, doc, version)[1] == markdown
    assert catalog.versions()[0]["rights"] == "needs_review"
    assert not catalog.visible(doc, version)
    with store.read() as conn:
        assert not catalog.selected(conn, 5)
        assert conn.execute("SELECT count(*) FROM rights_events").fetchone()[0] == 0
    copy = target / "legacy-catalog.sqlite3"
    with closing(sqlite3.connect(copy.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        assert conn.execute("SELECT action FROM shared_audit ORDER BY seq").fetchall() == [
            ("propose",), ("prepare_needs_review",)]
    catalog.decide(doc, version, decision="granted", actor="test reviewer", reason="Reviewed fixture source",
                   evidence="rights-record:test")
    assert catalog.document(doc, version)[1] == markdown
    with pytest.raises(ValueError, match="legacy_target_must_be_fresh_and_separate"):
        migrate_pending(root, archive, target=target, expected_plan=result["plan_sha256"])
    assert database.read_bytes() == before
    assert not Path(str(database) + "-wal").exists()


def test_tampering_or_an_uncheckpointed_rights_change_fails_closed(tmp_path):
    root, archive, database, raw, _ = _fixture(tmp_path)
    plan = migrate_pending(root, archive)
    original = archive / "historical" / "paper.md"
    original.write_bytes(raw + b"tamper")
    target = tmp_path / "new"
    with pytest.raises(ValueError, match="legacy_original_hash_mismatch"):
        migrate_pending(root, archive, target=target, expected_plan=plan["plan_sha256"])
    assert not target.exists()
    original.write_bytes(raw)
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO shared_rights VALUES (1)")
        writer.commit()
        wal = database.with_name(database.name + "-wal")
        assert wal.stat().st_size > 32
        with pytest.raises(ValueError, match="legacy_rights_or_build_history_requires_full_migration"):
            migrate_pending(root, archive, target=target, expected_plan=plan["plan_sha256"])
    assert not target.exists()
