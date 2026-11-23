"""Operator-only TXT summaries of committed chat turns.

The database remains the source of truth.  This module only writes a compact,
redacted text view after a turn reaches its durable terminal state.  Logging
failure must never change a task result.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

logger = logging.getLogger(__name__)

_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}")
_ABS_PATH = re.compile(r"(?:(?:[A-Za-z]:[\\/])|(?:/(?:home|root|opt|srv|var|mnt|tmp)/))[^\s\"']+")
_SECRET = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+\S+|api[_-]?key\s*[=:]\s*\S+"
                        r"|token\s*[=:]\s*\S+)")
_ROOT_JOB = re.compile(r"[a-f0-9]{12,32}")


def _code(value) -> str:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else "-"


def _time(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _text(value, limit: int = 5000) -> str:
    text = str(value or "").replace("\x00", " ")
    text = _ABS_PATH.sub("[路径已隐藏]", text)
    text = _SECRET.sub("[凭据已隐藏]", text)
    text = " ".join(text.split())
    return text[:max(0, int(limit))]


def _json(value, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def write_turn_log(turn: dict) -> None:
    """Write one bounded TXT file; never raise into the turn transaction."""
    from src.core import jobs
    from src.logger import _log_dir

    root_job = str(turn.get("root_job_id") or "")
    if not _ROOT_JOB.fullmatch(root_job):
        return
    temp_path = None
    try:
        uri = jobs._DB_PATH.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            owner, session = turn["owner_user_id"], turn["session_id"]
            row = conn.execute(
                "SELECT bounded_payload,clear_reason FROM chat_turn_queue "
                "WHERE owner_user_id=? AND session_id=? AND root_job_id=?",
                (owner, session, root_job),
            ).fetchone()
            payload = _json(row["bounded_payload"], {}) if row else {}
            query = _text(payload.get("query"), 3000) if isinstance(payload, dict) else ""
            lineage = conn.execute(
                "WITH RECURSIVE lineage(job_id) AS ("
                "SELECT job_id FROM task_jobs WHERE job_id=? AND owner_user_id=? AND session_id=? "
                "UNION SELECT j.job_id FROM task_jobs j JOIN lineage l ON j.parent_job_id=l.job_id "
                "WHERE j.owner_user_id=? AND j.session_id=?) "
                "SELECT job_id,status,current_step,stop_reason,terminal_snapshot,created_at "
                "FROM task_jobs WHERE job_id IN lineage ORDER BY created_at",
                (root_job, owner, session, owner, session),
            ).fetchall()
            lines = [
                f"任务 {root_job}",
                f"结束时间 {_time(turn.get('finished_at'))}",
                f"会话历史已保存 {'是' if turn.get('history_saved') else '否'}",
                f"结束原因 {_code(turn.get('clear_reason'))}",
                "",
                "## 用户本轮",
                query or "-",
            ]
            for job in lineage:
                snapshot = _json(job["terminal_snapshot"], {})
                lines.extend([
                    "",
                    f"## 执行 {job['job_id']}",
                    f"状态={_code(job['status'])} 阶段={_code(job['current_step'])} "
                    f"结果={_code(snapshot.get('display_status') if isinstance(snapshot, dict) else '')} "
                    f"停止原因={_code(job['stop_reason'])}",
                ])
                ops = conn.execute(
                    "SELECT tool_name,status,attempts,updated_at,outcome FROM task_operations "
                    "WHERE job_id=? AND owner_user_id=? ORDER BY updated_at",
                    (job["job_id"], owner),
                )
                for op in ops:
                    outcome = _json(op["outcome"], {})
                    reason = outcome.get("reason_code") if isinstance(outcome, dict) else ""
                    lines.append(
                        f"{_time(op['updated_at'])} 工具 {_code(op['tool_name'])} "
                        f"状态={_code(op['status'])} 尝试={op['attempts']} 原因={_code(reason)}"
                    )
                attempts = conn.execute(
                    "SELECT purpose,status,created_at FROM task_model_attempts "
                    "WHERE job_id=? ORDER BY created_at", (job["job_id"],),
                )
                for attempt in attempts:
                    lines.append(
                        f"{_time(attempt['created_at'])} 模型 {_code(attempt['purpose'])} "
                        f"状态={_code(attempt['status'])}"
                    )
            # The public conversation body is scoped to this turn only.  A
            # turn's batch carries ``crid:<client_request_id>`` on its first
            # row, so the id range between this turn's first row and the next
            # turn's first row is exactly this turn.  Never fall back to
            # "latest 24 rows in the session": that mixes other turns into an
            # operator log.
            request_id = str(turn.get("client_request_id") or "")
            request_key = f"crid:{request_id[:64]}" if request_id else ""
            messages = []
            if request_key:
                first = conn.execute(
                    "SELECT MIN(id) AS id FROM messages WHERE session_id=? "
                    "AND (client_request_id=? OR idempotency_key=?)",
                    (session, request_key, request_key)).fetchone()
                first_id = first["id"] if first is not None else None
                if first_id is not None:
                    following = conn.execute(
                        "SELECT MIN(id) AS id FROM messages WHERE session_id=? "
                        "AND id>? AND (client_request_id LIKE 'crid:%' "
                        "OR idempotency_key LIKE 'crid:%')",
                        (session, first_id)).fetchone()
                    next_id = following["id"] if following is not None else None
                    sql = ("SELECT msg_type,content,name,timestamp FROM messages "
                           "WHERE session_id=? AND id>=?")
                    params = [session, first_id]
                    if next_id is not None:
                        sql += " AND id<?"
                        params.append(next_id)
                    sql += " ORDER BY id ASC LIMIT 400"
                    messages = conn.execute(sql, params).fetchall()
            if messages:
                lines.extend(["", "## 本轮对话（只含公开正文与工具名）"])
                for message in messages:
                    kind = str(message["msg_type"] or "")
                    if kind == "tool":
                        lines.append(f"{message['timestamp']} [工具:{_code(message['name'])}]")
                        continue
                    body = _text(message["content"], 6000)
                    if body:
                        lines.append(f"{message['timestamp']} [{kind}] {body}")
            else:
                lines.extend(["", "## 本轮对话", "本轮消息未找到（未用其它轮次补齐）"])
        log_dir: Path = _log_dir() / "turns" / _time(turn.get("finished_at"))[:10]
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=log_dir, delete=False) as handle:
            temp_path = handle.name
            handle.write("\n".join(lines) + "\n")
        target = log_dir / f"{root_job}.txt"
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
        temp_path = None
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, OverflowError) as exc:
        logger.warning("turn_txt_log_failed kind=%s", type(exc).__name__)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
