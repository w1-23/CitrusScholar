"""Account-scoped administrative request audit with a fixed one-year retention."""
from __future__ import annotations

import json
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
_STATUSES = {"running", "completed", "failed", "cancelled", "stopped", "awaiting_input", "detached",
             "attachment_rejected", "quota_rejected", "quota_denied", "quota_exceeded", "queue_full", "queue_timeout"}
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
        conn.execute("BEGIN IMMEDIATE")
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
                finished_at REAL,
                metadata_version INTEGER NOT NULL DEFAULT 0
            )
        """)
        if "metadata_version" not in {row[1] for row in conn.execute("PRAGMA table_info(request_audit)")}:
            conn.execute("ALTER TABLE request_audit ADD COLUMN metadata_version INTEGER NOT NULL DEFAULT 0")
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
                (request_id,user_id,username,session_id,query,attachment_metadata_json,created_at,metadata_version)
                VALUES(?,?,?,?,'',?,?,1)
            """, (request_id, int(user_id), _code(username), str(session_id), json.dumps(metadata), time.time()))


def _safe_tools(tools):
    result = []
    for item in (tools if isinstance(tools, list) else [])[:100]:
        if isinstance(item, str):
            result.append({"name": _code(item)})
        elif isinstance(item, dict):
            row = {key: _code(item[key]) for key in ("name", "tool", "status") if key in item}
            if "elapsed_ms" in item:
                row["elapsed_ms"] = _size(item["elapsed_ms"])
            result.append(row)
    return result


def _safe_retrieval(metrics):
    return {key: value for key, value in (metrics.items() if isinstance(metrics, dict) else ())
            if key in _RETRIEVAL_METRICS and type(value) in {int, float} and 0 <= value < 2**53}


def finish_request(*, request_id: str, answer: str = "", tool_trace: list[Any] | None = None,
                   retrieval_trace: dict[str, Any] | None = None,
                   status: str = "completed", elapsed_ms: int | None = None) -> None:
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("""
            UPDATE request_audit SET answer='', tool_trace_json=?, retrieval_trace_json=?,
                status=?, elapsed_ms=?, finished_at=? WHERE request_id=?
        """, (json.dumps(_safe_tools(tool_trace)), json.dumps(_safe_retrieval(retrieval_trace)), _code(status),
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


def _decode(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def list_requests(*, limit: int = 50, offset: int = 0, user_id: int | None = None,
                  query: str = "", actor_user_id: int | None = None, actor_token: str = "") -> dict[str, Any]:
    from src import auth

    limit = max(1, min(int(limit), 100))
    offset = max(0, min(int(offset), 2**63 - 1))
    where = ["r.created_at>=?", "a.deleting=0"]
    params: list[Any] = [time.time() - RETENTION_SECONDS]
    if user_id is not None:
        where.append("r.user_id=?")
        params.append(user_id)
    if query.strip():
        where.append("(u.username LIKE ? OR r.session_id LIKE ? OR r.request_id LIKE ?)")
        params.extend([f"%{query.strip()[:100]}%"] * 3)
    scope = ("FROM audit_source.request_audit r JOIN users u ON u.user_id=r.user_id "
             "JOIN account_admission a ON a.owner=r.user_id WHERE " + " AND ".join(where))

    # Authenticate and filter membership using one account snapshot. Counts and
    # the page then share one audit snapshot; neither store takes a writer lock.
    with auth.read_transaction() as conn:
        if actor_user_id is not None:
            auth._require_admin_actor(conn, actor_user_id, actor_token)
        conn.execute("ATTACH DATABASE ? AS audit_source", (DB_PATH.resolve().as_uri() + "?mode=ro",))
        total = int(conn.execute(f"SELECT COUNT(*) {scope}", params).fetchone()[0])
        rows = conn.execute(
            "SELECT r.request_id,r.user_id,u.username,r.session_id,"
            "CASE WHEN r.metadata_version=1 THEN r.attachment_metadata_json END AS attachment_metadata_json,"
            "CASE WHEN r.metadata_version=1 THEN r.tool_trace_json END AS tool_trace_json,"
            "CASE WHEN r.metadata_version=1 THEN r.retrieval_trace_json END AS retrieval_trace_json,"
            "r.status,r.elapsed_ms,r.created_at,r.finished_at,r.metadata_version "
            f"{scope} ORDER BY r.created_at DESC,r.request_id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        version = item.pop("metadata_version")
        attachments = _decode(item.pop("attachment_metadata_json"))
        tools = _decode(item.pop("tool_trace_json"))
        retrieval = _decode(item.pop("retrieval_trace_json"))
        item.update(query="", answer="", metadata_recorded=version == 1,
                    status=item["status"] if item["status"] in _STATUSES else "unknown",
                    tools=_safe_tools(tools), retrieval=_safe_retrieval(retrieval),
                    attachments=[{"approx_bytes": _size(a["approx_bytes"])} for a in attachments[:8]
                                 if isinstance(a, dict) and type(a.get("approx_bytes")) is int]
                    if isinstance(attachments, list) else [])
        item["created_at"] = datetime.fromtimestamp(item["created_at"], timezone.utc).isoformat()
        if item.get("finished_at"):
            item["finished_at"] = datetime.fromtimestamp(item["finished_at"], timezone.utc).isoformat()
        items.append(item)
    return {"total": total, "items": items, "retention_days": 365}
