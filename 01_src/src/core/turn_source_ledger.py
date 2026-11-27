# -*- coding: utf-8 -*-
"""Append-only source ledger for one answer turn.

``tool_results`` remains the full execution record. This ledger stores only the
small, user-visible source identity needed by the answer bubble, including
sources reused from the recent session-evidence ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any

_schema_ready = False
_ANGLE_TAG = re.compile(r"<[^>]{1,120}>")
_ABSOLUTE_PATH = re.compile(
    r"(?:(?:[A-Za-z]:[\\/])|(?:/(?:home|root|opt|srv|var|mnt)/))[^\s\"']+"
)


def _connect() -> sqlite3.Connection:
    from src.session.manager import DB_PATH, _connect_db
    return _connect_db(str(DB_PATH))


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_source_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                origin TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_key TEXT NOT NULL,
                title TEXT,
                url TEXT,
                snippet TEXT,
                source_turn_seq INTEGER,
                created_at TEXT NOT NULL,
                read_ref TEXT NOT NULL DEFAULT '',
                citation_ref TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turn_source_request "
            "ON turn_source_ledger(session_id, request_id, origin, id)"
        )
        if "read_ref" not in {row[1] for row in conn.execute("PRAGMA table_info(turn_source_ledger)")}:
            conn.execute("ALTER TABLE turn_source_ledger ADD COLUMN read_ref TEXT NOT NULL DEFAULT ''")
        if "citation_ref" not in {row[1] for row in conn.execute("PRAGMA table_info(turn_source_ledger)")}:
            conn.execute("ALTER TABLE turn_source_ledger ADD COLUMN citation_ref TEXT NOT NULL DEFAULT ''")
        columns = {row[2] for row in conn.execute("PRAGMA index_info(uq_turn_source_identity)")}
        if columns and "citation_ref" not in columns:
            conn.execute("DROP INDEX uq_turn_source_identity")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_source_identity "
                     "ON turn_source_ledger(session_id,request_id,origin,kind,source_key,citation_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turn_source_read "
                     "ON turn_source_ledger(kind,source_key)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_source_counters (
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                next_value INTEGER NOT NULL,
                PRIMARY KEY (session_id, request_id, kind)
            )
        """)
        conn.commit()
    _schema_ready = True


def safe_http_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return ""
    return raw


def _safe_text(value: object, limit: int) -> str:
    text = " ".join(_ANGLE_TAG.sub(" ", str(value or "")).split())
    text = _ABSOLUTE_PATH.sub("[路径已隐藏]", text)
    return text[:max(0, int(limit))]


def safe_text(value: object, limit: int) -> str:
    """Public display sanitizer shared by ledger readers and API projection."""
    return _safe_text(value, limit)


def _insert(row: dict[str, Any]) -> bool:
    required = ("session_id", "request_id", "origin", "kind", "source_key")
    if any(not str(row.get(key) or "").strip() for key in required):
        return False
    try:
        ensure_schema()
        stamp = datetime.now(timezone.utc).isoformat()
        with _connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO turn_source_ledger
                   (session_id,request_id,origin,kind,source_key,title,url,
                    snippet,source_turn_seq,created_at,read_ref,citation_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(row["session_id"]), str(row["request_id"]),
                    str(row["origin"]), str(row["kind"]),
                    str(row["source_key"]), _safe_text(row.get("title"), 200),
                    safe_http_url(row.get("url")) if row["kind"] == "web" else "",
                    _safe_text(row.get("snippet"), 320),
                    row.get("source_turn_seq"), stamp, json.dumps(row["read_ref"]) if row.get("read_ref") else "",
                    _citation_ref(row.get("citation_ref")),
                ),
            )
            conn.commit()
        return cursor.rowcount == 1
    except Exception:
        return False



def _citation_ref(value) -> str:
    text = str(value or "").strip().upper()
    import re
    return text if re.fullmatch(r"[LW]\d{1,3}", text) else ""


def allocate_citation_refs(session_id: str, request_id: str, kind: str, count: int) -> list[str]:
    """Allocate turn-global display ids (L#/W#) for answer citations."""
    prefix = {"web": "W", "local": "L"}.get(str(kind or "").casefold())
    try:
        count = max(0, min(50, int(count or 0)))
    except (TypeError, ValueError):
        count = 0
    if not prefix or count <= 0:
        return []
    sid, rid = str(session_id or ""), str(request_id or "")
    if not sid or not rid:
        return [f"{prefix}{n}" for n in range(1, count + 1)]
    try:
        ensure_schema()
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT next_value FROM turn_source_counters "
                "WHERE session_id=? AND request_id=? AND kind=?",
                (sid, rid, kind),
            ).fetchone()
            start = int(row[0]) if row else 1
            if row:
                conn.execute(
                    "UPDATE turn_source_counters SET next_value=? "
                    "WHERE session_id=? AND request_id=? AND kind=?",
                    (start + count, sid, rid, kind),
                )
            else:
                conn.execute(
                    "INSERT INTO turn_source_counters(session_id,request_id,kind,next_value) "
                    "VALUES (?,?,?,?)",
                    (sid, rid, kind, start + count),
                )
            conn.commit()
        return [f"{prefix}{n}" for n in range(start, start + count)]
    except Exception:
        return [f"{prefix}{n}" for n in range(1, count + 1)]

def _local_source_key(row: dict) -> str:
    from src.core.corpus_source import is_valid_record_key

    for key in ("record_key", "paper_id", "record_id", "doc_id"):
        value = str(row.get(key) or "").strip()
        if is_valid_record_key(value):
            return value
    chunk = str(row.get("chunk_id") or "").split("::", 1)[0].split(":", 1)[0]
    return chunk if is_valid_record_key(chunk) else ""


def _local_title(row: dict, record_key: str) -> str:
    from src.core.record_label import display_label

    if isinstance(row, dict):
        title = row.get("title") or row.get("paper_title")
        if title:
            return display_label(row)
    return "本地文献"


def reference_key(kind: str, ref: dict) -> str:
    identity = json.dumps(ref, sort_keys=True, separators=(",", ":"))
    return kind + ":" + hashlib.sha256(identity.encode()).hexdigest()


def local_read_reference(ref: object) -> dict | None:
    if not isinstance(ref, dict):
        return None
    value, turn, generation = (ref.get(key) for key in ("ref", "turn_id", "generation"))
    start, end = ref.get("start"), ref.get("end")
    if (not isinstance(value, str) or not re.fullmatch(r"(?:ev:[a-f0-9]{32}|ce1:[a-f0-9]{64})", value)
            or not isinstance(turn, str) or not 1 <= len(turn) <= 160
            or not isinstance(generation, str) or not 1 <= len(generation) <= 240
            or type(start) is not int or type(end) is not int or not 0 <= start < end
            or end - start > 30_000):
        return None
    return {"ref": value, "turn_id": turn, "generation": generation, "start": start, "end": end}


def record_tool_sources(session_id: str, request_id: str, *, artifact: dict | None,
                        reference: dict | None, tool_name: str, result_text: str = "",
                        limit: int = 12) -> int:
    """Register evidence actually delivered by successful, retained tool calls."""
    from src.core.tracing import get_job_id
    from src.shared_corpus.references import evidence_rows

    if (not session_id or not request_id or not reference or reference.get("tool_name") != tool_name
            or reference.get("request_id") != request_id or not reference.get("storage", {}).get("complete")):
        return 0
    data = artifact if isinstance(artifact, dict) else {}
    receipt = {"tool_call_id": reference["tool_call_id"], "full_sha256": reference["full_sha256"]}
    common = {"session_id": session_id, "request_id": request_id, "origin": "current"}
    if tool_name in {"read_candidate", "read_local_evidence"}:
        ref = local_read_reference(data.get("read_ref"))
        rows = data.get("main_results") or []
        if not ref or len(rows) != 1 or rows[0].get("source_ref") != ref["ref"]:
            return 0
        row = rows[0]
        return int(_insert({**common, "kind": "local", "source_key": reference_key("cite", ref),
                            "origin": "current" if ref["turn_id"] == get_job_id() else "history",
                            "title": _local_title(row, _local_source_key(row)), "read_ref": ref,
                            "citation_ref": row.get("citation_ref")}))

    added = 0
    if tool_name in {"local_hybrid_search", "search_local_candidates"}:
        for row in (data.get("main_results") or [])[:limit]:
            entry = row.get("read_entry") or {}
            limit_chars = 30_000 if data.get("corpus_contract") == 1 and str(entry.get("ref", "")).startswith("ce1:") else 240
            width = min(len(str(row.get("text") or "")), limit_chars)
            ref = local_read_reference({**entry, "start": 0, "end": width})
            marker = _citation_ref(row.get("citation_ref"))
            if ref and marker:
                added += _insert({**common, "kind": "local", "source_key": reference_key("cite", ref),
                                  "title": _local_title(row, _local_source_key(row)), "read_ref": ref,
                                  "citation_ref": marker})

    if tool_name in {"deepseek_search", "deepseek_fetch", "deepseek_fetch_paper", "browse_web"}:
        if tool_name == "browse_web" and not data.get("_browse_evidence"):
            return 0
        from src.core.web_output_separator import clean_web_title
        added = 0
        for row in (data.get("web_results") or [])[:limit]:
            url = safe_http_url(row.get("url"))
            excerpt = row.get("text") or row.get("snippet") or row.get("summary")
            marker = _citation_ref(row.get("citation_ref"))
            if url and excerpt and (tool_name != "deepseek_search" or marker):
                added += _insert({**common, "kind": "web", "source_key": url, "url": url,
                                  "title": clean_web_title(row.get("title"), url), "snippet": excerpt,
                                  "read_ref": receipt, "citation_ref": marker})
        return added

    seen = set()
    for row, ref in evidence_rows(tool_name, result_text):
        key = reference_key("shared", ref)
        if key not in seen:
            added += _insert({**common, "kind": "shared", "source_key": key,
                              "title": row.get("title"), "read_ref": ref,
                              "citation_ref": row.get("citation_ref")})
            seen.add(key)
        if len(seen) >= limit:
            break
    return added


def record_history_sources(session_id: str, request_id: str,
                           turn_seqs: list[int], limit: int = 12) -> int:
    """Copy the actually selected recent evidence into this turn's source view."""
    sid, rid = str(session_id or ""), str(request_id or "")
    turns = sorted({int(v) for v in (turn_seqs or []) if str(v).strip().isdigit()})
    if not sid or not rid or not turns:
        return 0
    try:
        ensure_schema()
        marks = ",".join("?" for _ in turns)
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT turn_seq,evidence_json FROM session_evidence "
                f"WHERE session_id=? AND turn_seq IN ({marks}) ORDER BY turn_seq",
                (sid, *turns),
            ).fetchall()
    except Exception:
        return 0

    added = 0
    seen: set[tuple[str, str]] = set()
    for turn_seq, evidence_json in rows:
        try:
            evidence = json.loads(evidence_json or "[]")
        except Exception:
            continue
        for entry in evidence if isinstance(evidence, list) else []:
            if not isinstance(entry, dict) or len(seen) >= max(1, int(limit or 12)):
                continue
            url = safe_http_url(entry.get("url"))
            source = str(entry.get("source") or "").lower()
            if url and (source == "web" or "http" in url):
                marker = ("web", url)
                if marker in seen:
                    continue
                seen.add(marker)
                from src.core.web_output_separator import clean_web_title
                item = {
                    "session_id": sid, "request_id": rid, "origin": "history",
                    "kind": "web", "source_key": url,
                    "title": clean_web_title(entry.get("title"), url),
                    "url": url, "snippet": entry.get("snippet") or "",
                    "source_turn_seq": int(turn_seq),
                }
            else:
                key = _local_source_key(entry)
                if not key:
                    continue
                marker = ("local", key)
                if marker in seen:
                    continue
                seen.add(marker)
                item = {
                    "session_id": sid, "request_id": rid, "origin": "history",
                    "kind": "local", "source_key": key,
                    "title": _local_title(entry, key),
                    "url": "", "snippet": entry.get("snippet") or "",
                    "source_turn_seq": int(turn_seq),
                }
            if _insert(item):
                added += 1
    return added


def fetch_rows(session_id: str, request_id: str) -> list[dict]:
    sid, rid = str(session_id or ""), str(request_id or "")
    if not sid or not rid:
        return []
    try:
        ensure_schema()
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT origin,kind,source_key,title,url,snippet,source_turn_seq,read_ref,citation_ref "
                "FROM turn_source_ledger WHERE session_id=? AND request_id=? "
                "ORDER BY CASE origin WHEN 'current' THEN 0 ELSE 1 END, "
                "CASE kind WHEN 'local' THEN 0 ELSE 1 END, id",
                (sid, rid),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def find_local_citation(source_key: str) -> dict | None:
    if not re.fullmatch(r"cite:[a-f0-9]{64}", source_key):
        return None
    ensure_schema()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT session_id, title, read_ref FROM turn_source_ledger "
            "WHERE kind='local' AND source_key=? AND read_ref<>'' ORDER BY id LIMIT 1",
            (source_key,),
        ).fetchone()
    return dict(row) if row else None
