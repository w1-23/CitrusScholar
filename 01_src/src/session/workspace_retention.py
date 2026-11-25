"""Retire private workspace versions while preserving live and durable dependencies."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from src.core.jobs import unsettled_history_job_sql


_OWNED_PRIVATE_VERSION = (
    "JOIN session_files f ON f.file_id=v.file_id AND f.owner_user_id=v.owner "
    "AND f.session_id=v.session AND f.sha256=v.sha256 AND f.visibility='internal' "
    "AND f.producer_tool='workspace_write' AND f.origin='uploaded' "
)


def release_expired_workspace_versions(files: sqlite3.Connection, sessions_db: str | Path, *,
                                       scope: tuple[int, str] | None = None,
                                       cutoff: float | None = None) -> None:
    """Release self-pins only, under the library and task/session writer fences.

    Latest versions in open sessions stay addressable. Older versions can stop
    occupying quota after expiry and after the session's jobs settle. A closed
    session retires all unreferenced versions. Ordinary library TTL owns unlink.
    """
    if scope is None:
        if cutoff is None:
            raise ValueError("workspace version expiry requires a cutoff")
        scopes = list(files.execute(
            "SELECT DISTINCT v.owner,v.session FROM workspace_file_versions v "
            + _OWNED_PRIVATE_VERSION + "WHERE f.last_accessed_at < ?", (cutoff,)))
    else:
        scopes = [scope]
    path = Path(sessions_db).absolute()
    if not scopes or path.is_symlink() or not path.is_file():
        return

    # The file DB writer is already held. Do not wait for a session writer
    # that might itself be waiting on the file store. A busy sweep is retried.
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0)) as sessions:
        try:
            sessions.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return
            raise
        tables = {row[0] for row in sessions.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sessions", "task_jobs"} <= tables:
            return  # No trustworthy task or session state from this database.
        blocking_job = unsettled_history_job_sql(sessions, tables)
        for owner, session in scopes:
            key = (owner, session)
            row = sessions.execute(
                "SELECT deleted_at FROM sessions WHERE owner_user_id=? AND session_id=?", key).fetchone()
            if row is None or (scope is not None and not row[0]):
                continue
            if sessions.execute(
                "SELECT 1 FROM task_jobs j WHERE j.owner_user_id=? AND j.session_id=? "
                "AND (" + blocking_job + " OR (j.status='failed' AND j.current_step='interrupted' "
                "AND j.error='service_restart')) LIMIT 1", key).fetchone():
                continue
            if "chat_turn_queue" in tables and sessions.execute(
                "SELECT 1 FROM chat_turn_queue WHERE owner_user_id=? AND session_id=? "
                "AND state<>'terminal' LIMIT 1", key).fetchone():
                continue
            closed = bool(row[0])
            if closed:
                files.execute("INSERT OR IGNORE INTO workspace_file_closed_sessions VALUES(?,?)", key)
            _retire_scope(files, key, closed=closed, cutoff=cutoff)
        # Both locks must cover the version-index commit. This connection does
        # not write anything, so its context close merely releases the fence.
        files.commit()


def _retire_scope(files, key, *, closed, cutoff):
    expired_history = (
        " AND f.last_accessed_at < ? AND EXISTS (SELECT 1 FROM workspace_file_versions x "
        "WHERE x.owner=v.owner AND x.session=v.session AND x.path=v.path AND x.version>v.version)"
    ) if not closed else ""
    rows = files.execute(
        "SELECT v.file_id FROM workspace_file_versions v " + _OWNED_PRIVATE_VERSION
        + "WHERE v.owner=? AND v.session=? AND NOT EXISTS (SELECT 1 FROM session_file_resource_refs r "
        "WHERE r.file_id=v.file_id AND NOT (r.owner_user_id=v.owner "
        "AND r.resource_kind='workspace_version' AND r.resource_id=v.file_id))" + expired_history,
        key if closed else (*key, cutoff)).fetchall()
    for (fid,) in rows:
        files.execute("DELETE FROM session_file_resource_refs WHERE owner_user_id=? "
                      "AND resource_kind='workspace_version' AND resource_id=? AND file_id=?", (key[0], fid, fid))
        files.execute("DELETE FROM workspace_file_versions WHERE owner=? AND session=? AND file_id=?", (*key, fid))
