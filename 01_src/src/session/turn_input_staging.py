"""File-store side of queued chat input admission.

The queue receipt is the only accept fact. A pending record is committed
before any bytes or pins, and survives until the queue fact can be observed.
Callers hold the scoped admission lock when starting or resolving a stage.
"""
from __future__ import annotations

import time
from pathlib import Path

from src.session import files

RESOURCE_KIND = "chat_turn_input"
_SCOPE = "owner_user_id=? AND session_id=? AND client_request_id=?"


def begin(scope):
    files.init_store()
    with files._connect() as conn:
        conn.execute("INSERT INTO chat_turn_input_stages VALUES(?,?,?)", scope)


def pending(*, after=0, limit=64):
    with files._connect() as conn:
        return conn.execute("SELECT rowid,owner_user_id,session_id,client_request_id "
            "FROM chat_turn_input_stages WHERE rowid>? ORDER BY rowid LIMIT ?", (after, limit)).fetchall()


def pin(scope, resource_id, inputs):
    owner, session, request = scope
    expected = {item["file_id"]: item["sha256"] for item in inputs}
    with files._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM chat_turn_input_stages WHERE " + _SCOPE, scope).fetchone():
            raise ValueError("turn input stage is missing")
        refs = files._replace_resource_references_locked(conn, owner, RESOURCE_KIND, resource_id, expected)
        if {ref["file_id"]: ref["sha256"] for ref in refs} != expected:
            raise ValueError("turn input checksum changed before admission")
        now = time.time()
        for file_id in expected:
            files._link_turn(conn, session_id=session, owner_user_id=owner,
                             client_request_id=request, file_id=file_id, now=now)
            conn.execute("UPDATE session_files SET last_accessed_at=? WHERE file_id=? AND owner_user_id=?",
                         (now, file_id, owner))


def resolve(scope, resource_id, *, accepted):
    """Commit the handoff or compensate only this attempt's files and links."""
    owner, session, _ = scope
    with files._connect() as conn:
        stage_query = "SELECT 1 FROM chat_turn_input_stages WHERE " + _SCOPE
        if not conn.execute(stage_query, scope).fetchone():
            return  # Ordinary unattached turns do not take the File writer lock.
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(stage_query, scope).fetchone():
            return  # Account purge may have won the writer lock.
        if not accepted:
            key = (owner, RESOURCE_KIND, resource_id)
            conn.execute("DELETE FROM session_file_turn_refs WHERE " + _SCOPE +
                " AND file_id IN (SELECT file_id FROM session_file_resource_refs "
                "WHERE owner_user_id=? AND resource_kind=? AND resource_id=?)", (*scope, *key))
            conn.execute("DELETE FROM session_file_resource_refs "
                         "WHERE owner_user_id=? AND resource_kind=? AND resource_id=?", key)
            for item in conn.execute("SELECT file_id,storage_path FROM chat_turn_staged_uploads WHERE " + _SCOPE,
                                     scope).fetchall():
                row = conn.execute("SELECT * FROM session_files WHERE file_id=?", (item["file_id"],)).fetchone()
                if row is not None:
                    if (row["owner_user_id"], row["session_id"], row["client_request_id"],
                            row["origin"], row["storage_path"]) != (*scope, files.ORIGIN_UPLOADED, item["storage_path"]):
                        raise ValueError("staged upload identity mismatch")
                    if conn.execute("SELECT 1 FROM session_file_resource_refs WHERE file_id=? LIMIT 1",
                                    (item["file_id"],)).fetchone():
                        # A different accepted resource owns the bytes now.
                        continue
                    if not files._delete_indexed_file(conn, row)[0]:
                        raise OSError("staged upload cleanup failed")
                else:
                    path = Path(item["storage_path"])
                    root = files._session_dir(session, owner)
                    if path.parent != root or not path.name.startswith(item["file_id"] + "_"):
                        raise ValueError("staged upload path is invalid")
                    path.unlink(missing_ok=True)
        conn.execute("DELETE FROM chat_turn_staged_uploads WHERE " + _SCOPE, scope)
        conn.execute("DELETE FROM chat_turn_input_stages WHERE " + _SCOPE, scope)
