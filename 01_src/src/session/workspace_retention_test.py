"""Workspace pin lifecycle tests against disposable session and library stores."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from src.agent.workspace_storage import WorkspaceStorage
from src.core import chat_turn_queue, jobs
from src.figures import store as figures
from src.session import files, manager

OWNER = 7
SID = "workspace-closed"
OTHER = "workspace-other"
PATH = "/workspace/note.txt"


@pytest.fixture
def state(tmp_path, monkeypatch):
    sessions = tmp_path / "state" / "sessions.db"
    sessions.parent.mkdir()
    with closing(sqlite3.connect(sessions)) as conn, conn:
        conn.execute("CREATE TABLE sessions(session_id TEXT PRIMARY KEY, owner_user_id INTEGER, "
                     "deleted_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
        conn.executemany("INSERT INTO sessions(session_id,owner_user_id) VALUES (?,?)",
                         [(SID, OWNER), (OTHER, OWNER)])
    monkeypatch.setattr(jobs, "_DB_PATH", sessions)
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.db")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    monkeypatch.setattr(figures, "migrate_resource_references", lambda: None)
    monkeypatch.setattr(manager.session_manager, "db_path", sessions)
    from src.retrieval import evidence_adapter
    monkeypatch.setattr(evidence_adapter, "purge_session_evidence", lambda *a, **kw: None)
    chat_turn_queue.ensure_schema()
    return sessions


def _save(storage, text, previous=None):
    return storage.save(PATH, content=text, expected_version=previous["version"] if previous else 0,
                        expected_sha256=previous["sha256"] if previous else "")["ref"]


def _age(*refs):
    with files._connect() as conn:
        conn.executemany("UPDATE session_files SET last_accessed_at=1 WHERE file_id=?",
                         [(ref["file_id"],) for ref in refs])


def _present(ref):
    with files._connect() as conn:
        row = conn.execute("SELECT storage_path FROM session_files WHERE file_id=?",
                           (ref["file_id"],)).fetchone()
    return bool(row and Path(row[0]).is_file())


def test_ttl_preserves_current_recent_active_and_independently_pinned_versions(state):
    storage = WorkspaceStorage(OWNER, SID, "test-run")
    old = _save(storage, "older")
    pinned = _save(storage, "published-source", old)
    recent = _save(storage, "recently-read", pinned)
    latest = _save(storage, "current", recent)
    other_owner = _save(WorkspaceStorage(8, SID, "other-run"), "different scope")
    _age(old, pinned, recent, latest, other_owner)
    assert storage.read(PATH, version=3)["text"] == "recently-read"
    files.replace_resource_references(OWNER, "published_artifact", "example", [pinned["file_id"]])
    job = jobs.claim_job(SID, "work-run", owner_user_id=OWNER, client_request_id="work-run", payload={})

    files.cleanup_idle_sessions(idle_days=1)
    assert all(_present(ref) for ref in (old, pinned, recent, latest, other_owner))
    assert jobs.mark_completed_if_open(job.job_id)
    files.cleanup_idle_sessions(idle_days=1)

    assert not _present(old)
    with pytest.raises(LookupError):
        storage.read(PATH, version=1)
    assert all(_present(ref) for ref in (pinned, recent, latest, other_owner))
    assert storage.read(PATH)["ref"] == latest
    following = _save(storage, "next", latest)
    assert following["version"] == latest["version"] + 1


def test_closed_workspace_retires_private_versions_without_touching_other_scopes(state):
    storage = WorkspaceStorage(OWNER, SID, "test-run")
    first = _save(storage, "private")
    protected = _save(storage, "protected", first)
    neighbor = _save(WorkspaceStorage(OWNER, OTHER, "other-run"), "unrelated")
    _age(first, protected, neighbor)
    files.replace_resource_references(OWNER, "published_artifact", "durable", [protected["file_id"]])

    assert manager.session_manager.soft_delete_session(SID)
    assert files.file_dependencies(first["file_id"], OWNER) == []
    assert {pin["resource_kind"] for pin in files.file_dependencies(protected["file_id"], OWNER)} == {
        "workspace_version", "published_artifact"}
    assert _present(first)  # soft delete retires metadata, ordinary TTL unlinks bytes
    with pytest.raises(PermissionError, match="workspace_session_closed"):
        _save(storage, "cannot recreate", protected)
    files.cleanup_idle_sessions(idle_days=1)
    assert not _present(first) and _present(protected) and _present(neighbor)

    files.release_resource_references(OWNER, "published_artifact", "durable")
    files.cleanup_idle_sessions(idle_days=1)
    assert not _present(protected) and _present(neighbor)


def test_closed_workspace_with_queued_turn_defers_retirement_until_settled(state):
    storage = WorkspaceStorage(OWNER, SID, "test-run")
    ref = _save(storage, "for the queued turn")
    _age(ref)
    chat_turn_queue.enqueue_turn(owner_user_id=OWNER, session_id=SID, client_request_id="queued",
                                request_id="server-request", bounded_payload={})
    # Test the lower-level storage protection even if a caller bypasses the
    # normal HTTP guard that disallows soft deletion with a queued turn.
    assert manager.session_manager.soft_delete_session(SID)
    files.cleanup_idle_sessions(idle_days=1)
    assert _present(ref) and files.file_dependencies(ref["file_id"], OWNER)
    chat_turn_queue.cancel_queued(owner_user_id=OWNER, session_id=SID, client_request_id="queued")
    files.cleanup_idle_sessions(idle_days=1)
    assert not _present(ref)


def test_account_purge_removes_orphan_version_rows_only_for_that_owner(state):
    _save(WorkspaceStorage(OWNER, SID, "our-run"), "ours")
    foreign = _save(WorkspaceStorage(8, SID, "other-run"), "foreign")
    with files._connect() as conn:
        conn.execute("INSERT INTO workspace_file_versions VALUES (?,?,?,?,?,?)",
                     (OWNER, "orphan-session", PATH, 1, "f" * 32, "e" * 64))
        conn.executemany("INSERT INTO workspace_file_closed_sessions VALUES (?,?)", [(OWNER, SID), (8, SID)])
    files.purge_account_files(OWNER, [SID])
    with files._connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT owner,file_id FROM workspace_file_versions")] == [
            (8, foreign["file_id"])]
        assert [tuple(row) for row in conn.execute("SELECT * FROM workspace_file_closed_sessions")] == [(8, SID)]
    assert _present(foreign)
