"""Account-scoped administrative request audit with a fixed one-year retention."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from src.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "state" / "admin_audit.sqlite3"
RETENTION_SECONDS = 365 * 24 * 60 * 60
_lock = threading.RLock()
_CODE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,79}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
_RETRIEVAL_METRICS = {"candidates", "passed", "filtered", "dense_candidates", "bm25_candidates",
                      "rrf_candidates", "elapsed_ms", "reranked", "selected", "total_ms"}


def _code(value):
    from src.logger import redact_log_text
    return redact_log_text(value) if isinstance(value, str) and _CODE.fullmatch(value) else "-"


def _size(value):
    return min(value, 2**53 - 1) if type(value) is int and value >= 0 else 0


def _visible(conn, user_id):
    return conn.execute("SELECT 1 FROM users u JOIN account_admission a ON a.owner=u.user_id "
                        "WHERE u.user_id=? AND a.deleting=0", (user_id,)).fetchone()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if os.name != "nt":
        try:
            DB_PATH.parent.chmod(0o700)
            DB_PATH.chmod(0o600)
        except OSError:
            pass
    return conn


def init_store() -> None:
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_audit (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                session_id TEXT NOT NULL,
                query TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                attachment_metadata_json TEXT NOT NULL DEFAULT '[]',
                tool_trace_json TEXT NOT NULL DEFAULT '[]',
                retrieval_trace_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'running',
                elapsed_ms INTEGER,
                created_at REAL NOT NULL,
                finished_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_audit_created ON request_audit(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_audit_user ON request_audit(user_id, created_at DESC)")
        conn.execute("DELETE FROM request_audit WHERE created_at < ?", (time.time() - RETENTION_SECONDS,))
        conn.commit()


def start_request(*, request_id: str, user_id: int, username: str,
                  session_id: str, query: str, attachments: list[dict[str, Any]]) -> None:
    from src.core.account_admission import current_store

    if not _ID.fullmatch(request_id) or not _ID.fullmatch(session_id):
        return
    metadata = [{"approx_bytes": max(_size(item.get("size_bytes")),
                                      len(item["data"]) * 3 // 4 if isinstance(item.get("data"), str) else 0)}
                for item in (attachments or [])[:8] if isinstance(item, dict)]
    with current_store().transaction() as admission:
        if not _visible(admission, user_id):
            return
        with _lock, closing(_connect()) as conn, conn:
            conn.execute("""
                INSERT OR IGNORE INTO request_audit
                (request_id,user_id,username,session_id,query,attachment_metadata_json,created_at)
                VALUES(?,?,?,?,'',?,?)
            """, (request_id, int(user_id), _code(username), str(session_id), json.dumps(metadata), time.time()))


def finish_request(*, request_id: str, answer: str = "", tool_trace: list[Any] | None = None,
                   retrieval_trace: dict[str, Any] | None = None,
                   status: str = "completed", elapsed_ms: int | None = None) -> None:
    safe_tools = []
    for item in (tool_trace or [])[:100]:
        if isinstance(item, str):
            safe_tools.append({"name": _code(item)})
        elif isinstance(item, dict):
            row = {key: _code(item[key]) for key in ("name", "tool", "status") if key in item}
            if "elapsed_ms" in item:
                row["elapsed_ms"] = _size(item["elapsed_ms"])
            safe_tools.append(row)
    safe_retrieval = {key: value for key, value in (retrieval_trace or {}).items()
                      if key in _RETRIEVAL_METRICS and ((type(value) is int and 0 <= value < 2**53)
                         or (type(value) is float and 0 <= value < 2**53 and math.isfinite(value)))}
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("""
            UPDATE request_audit SET answer='', tool_trace_json=?, retrieval_trace_json=?,
                status=?, elapsed_ms=?, finished_at=? WHERE request_id=?
        """, (json.dumps(safe_tools), json.dumps(safe_retrieval), _code(status),
              _size(elapsed_ms) if elapsed_ms is not None else None, time.time(), request_id))


def purge_owner(owner: int, *, claim) -> None:
    """Erase only the claimed owner; absence and repeat cleanup are safe."""
    from src.core.account_leases import AccountDeletionPending

    with claim.store.transaction() as admission:
        claim.validate(admission)
        if claim.owner != owner:
            raise PermissionError("request_audit_deletion_owner_mismatch")
        if admission.execute("SELECT 1 FROM account_work_leases WHERE owner=? AND refs>0 LIMIT 1", (owner,)).fetchone():
            raise AccountDeletionPending("account has admitted work still running")
        if not DB_PATH.exists():
            return
        with _lock, closing(_connect()) as conn, conn:
            conn.execute("PRAGMA secure_delete=ON")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='request_audit'").fetchone():
                conn.execute("DELETE FROM request_audit WHERE user_id=?", (owner,))


def mark_interrupted_requests() -> int:
    """Close audit rows left running when the single service process restarted."""
    with _lock, closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "UPDATE request_audit SET status='failed', finished_at=? WHERE status='running'",
            (time.time(),),
        )
        conn.commit()
        return int(cursor.rowcount or 0)


def list_requests(*, limit: int = 50, offset: int = 0, user_id: int | None = None,
                  query: str = "") -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    cutoff = time.time() - RETENTION_SECONDS
    clauses = ["created_at >= ?", "EXISTS (SELECT 1 FROM auth_source.users u "
               "JOIN auth_source.account_admission a ON a.owner=u.user_id "
               "WHERE u.user_id=request_audit.user_id AND a.deleting=0)"]
    params: list[Any] = [cutoff]
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(int(user_id))
    if query.strip():
        clauses.append("(username LIKE ? OR session_id LIKE ? OR request_id LIKE ? OR query LIKE ?)")
        like = f"%{query.strip()[:100]}%"
        params.extend([like] * 4)
    where = " AND ".join(clauses)
    from src.core.account_admission import current_store

    # Hold the same identity barrier as account close across both count and
    # page. The attached read-only view cannot silently include orphan rows.
    store = current_store()
    with store.transaction(), _lock, closing(_connect()) as conn, conn:
        conn.execute("ATTACH DATABASE ? AS auth_source", (store.path.as_uri() + "?mode=ro",))
        conn.execute("BEGIN")
        conn.execute("DELETE FROM request_audit WHERE created_at < ?", (cutoff,))
        total = int(conn.execute(f"SELECT COUNT(*) FROM request_audit WHERE {where}", params).fetchone()[0])
        rows = conn.execute(
            "SELECT request_id,user_id,username,session_id,query,answer,attachment_metadata_json,"
            "tool_trace_json,retrieval_trace_json,status,elapsed_ms,created_at,finished_at "
            f"FROM request_audit WHERE {where} ORDER BY created_at DESC,request_id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for col, key in (("attachment_metadata_json", "attachments"),
                         ("tool_trace_json", "tools"),
                         ("retrieval_trace_json", "retrieval")):
            try:
                item[key] = json.loads(item.pop(col) or ("{}" if key == "retrieval" else "[]"))
            except (TypeError, ValueError):
                item[key] = {} if key == "retrieval" else []
        item["created_at"] = datetime.fromtimestamp(item["created_at"], timezone.utc).isoformat()
        if item.get("finished_at"):
            item["finished_at"] = datetime.fromtimestamp(item["finished_at"], timezone.utc).isoformat()
        items.append(item)
    return {"total": total, "items": items, "retention_days": 365}
