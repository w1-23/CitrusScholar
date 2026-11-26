"""Metadata-only diagnostics derived from durable, owner-bound terminal turns."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

logger = logging.getLogger(__name__)
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}")
_ROOT_JOB = re.compile(r"[a-f0-9]{12,32}")
_CURSOR_LOCK = Lock()
_CURSORS: dict[Path, int] = {}
_VISIBLE_OWNER = ("SELECT 1 FROM users u JOIN account_admission a ON a.owner=u.user_id "
                  "WHERE u.user_id=? AND a.deleting=0")


def _code(value) -> str:
    from src.logger import redact_log_text
    return redact_log_text(value) if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else "-"


def _time(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _json_dict(value) -> dict:
    try:
        result = json.loads(value or "")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


@contextmanager
def _read_db(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        yield conn


def _target_parts(root_job, finished_at):
    if not isinstance(root_job, str) or not _ROOT_JOB.fullmatch(root_job):
        return None
    if not isinstance(finished_at, (int, float)) or finished_at <= 0:
        return None
    day = _time(finished_at)[:10]
    return (day, f"{root_job}.txt") if day != "-" else None


def _check_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise OSError("turn log directory is not a real directory")


def _target(parts):
    from src.logger import _log_dir

    root = _log_dir()
    directory = root / "turns" / parts[0]
    for path in (root, directory.parent, directory):
        _check_directory(path)
    return directory / parts[1]


def _regular_target(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise OSError("turn log path is not a regular file")
    return True


def _published_text(conn, root_job, owner, session, turn):
    lines = [
        "# Turn diagnostics (metadata only)",
        f"root_job_id={root_job}",
        f"finished_at={_time(turn['finished_at'])}",
        f"history_saved={int(bool(turn['history_saved']))}",
        f"clear_reason={_code(turn['clear_reason'])}",
    ]
    jobs = conn.execute(
        "WITH RECURSIVE lineage(job_id) AS ("
        "SELECT job_id FROM task_jobs WHERE job_id=? AND owner_user_id=? AND session_id=? "
        "UNION SELECT j.job_id FROM task_jobs j JOIN lineage l ON j.parent_job_id=l.job_id "
        "WHERE j.owner_user_id=? AND j.session_id=?) "
        "SELECT job_id,status,current_step,stop_reason,terminal_snapshot FROM task_jobs "
        "WHERE job_id IN lineage ORDER BY created_at LIMIT 100",
        (root_job, owner, session, owner, session),
    )
    for job in jobs:
        snapshot = _json_dict(job["terminal_snapshot"])
        lines.append(f"job={_code(job['job_id'])} status={_code(job['status'])} "
                     f"step={_code(job['current_step'])} result={_code(snapshot.get('display_status'))} "
                     f"stop_reason={_code(job['stop_reason'])}")
        for op in conn.execute(
                "SELECT tool_name,status,attempts,updated_at,outcome FROM task_operations "
                "WHERE job_id=? AND owner_user_id=? ORDER BY updated_at LIMIT 100", (job["job_id"], owner)):
            reason = _json_dict(op["outcome"]).get("reason_code")
            attempts = op["attempts"] if type(op["attempts"]) is int else "-"
            lines.append(f"{_time(op['updated_at'])} tool={_code(op['tool_name'])} "
                         f"status={_code(op['status'])} attempts={attempts} reason={_code(reason)}")
        for attempt in conn.execute(
                "SELECT purpose,status,created_at FROM task_model_attempts "
                "WHERE job_id=? ORDER BY created_at LIMIT 100", (job["job_id"],)):
            lines.append(f"{_time(attempt['created_at'])} model={_code(attempt['purpose'])} "
                         f"status={_code(attempt['status'])}")
    return "\n".join(lines) + "\n"


def write_turn_log(turn: dict) -> bool:
    """Write a missing terminal diagnostic; existing files are never replaced."""
    from src.core import jobs
    from src.core.account_admission import current_store

    root_job = turn.get("root_job_id")
    if not isinstance(root_job, str) or not _ROOT_JOB.fullmatch(root_job):
        return False
    temp_path = None
    try:
        owner, session = turn["owner_user_id"], turn["session_id"]
        with _read_db(jobs._DB_PATH) as conn:
            row = conn.execute(
                "SELECT q.finished_at,q.history_saved,q.clear_reason FROM chat_turn_queue q "
                "JOIN task_jobs j ON j.job_id=q.root_job_id AND COALESCE(j.parent_job_id,'')='' "
                "AND j.owner_user_id=q.owner_user_id AND j.session_id=q.session_id "
                "WHERE q.root_job_id=? AND q.owner_user_id=? AND q.session_id=? AND q.state='terminal' "
                "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.session_id=q.session_id "
                "AND s.owner_user_id IS NOT q.owner_user_id)", (root_job, owner, session),
            ).fetchone()
            parts = _target_parts(root_job, row["finished_at"]) if row else None
            if parts is None:
                return False
            target = _target(parts)
            if _regular_target(target):
                return False
            body = _published_text(conn, root_job, owner, session, row)
        # Deletion closes admission through this same SQLite write barrier. A
        # pre-fence snapshot cannot publish a log after its cleanup has run.
        with current_store().transaction() as admission:
            if not admission.execute(_VISIBLE_OWNER, (owner,)).fetchone():
                return False
            target = _target(parts)
            if _regular_target(target):
                return False
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(body)
            temp_path.replace(target)
            return True
    except (OSError, sqlite3.Error, KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        logger.warning("turn_txt_log_failed kind=%s", type(exc).__name__)
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def backfill_terminal_turn_logs(*, limit: int = 100) -> int:
    """Walk bounded rowid chunks, circling back for failures and late terminals."""
    from src.core import jobs

    batch = max(1, min(int(limit), 200))
    path = jobs._DB_PATH.resolve()
    if not path.is_file() or not _CURSOR_LOCK.acquire(blocking=False):
        return 0
    try:
        with _read_db(path) as conn:
            rows = conn.execute(
                "SELECT rowid,owner_user_id,session_id,root_job_id,state FROM chat_turn_queue "
                "WHERE rowid>? ORDER BY rowid LIMIT ?", (_CURSORS.get(path, 0), batch),
            ).fetchall()
        _CURSORS[path] = rows[-1]["rowid"] if len(rows) == batch else 0
        return sum(write_turn_log(dict(row)) for row in rows if row["state"] == "terminal")
    finally:
        _CURSOR_LOCK.release()


def purge_owner(owner: int, *, claim, sessions_db: Path) -> None:
    """Unlink only verified exact root/date files, before deleting the evidence."""
    from src.core.account_leases import AccountDeletionPending

    with claim.store.transaction() as admission:
        claim.validate(admission)
        if claim.owner != owner:
            raise PermissionError("turn_log_deletion_owner_mismatch")
        if admission.execute("SELECT 1 FROM account_work_leases WHERE owner=? AND refs>0 LIMIT 1", (owner,)).fetchone():
            raise AccountDeletionPending("account has admitted work still running")
    if not sessions_db.exists():
        return
    with _read_db(sessions_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chat_turn_queue" not in tables:
            return
        checks = []
        if "task_jobs" in tables and {"owner_user_id", "session_id"}.issubset(
                {row[1] for row in conn.execute("PRAGMA table_info(task_jobs)")}):
            checks.append("EXISTS (SELECT 1 FROM task_jobs j WHERE j.job_id=q.root_job_id "
                          "AND (j.owner_user_id IS NOT NULL AND j.owner_user_id<>q.owner_user_id "
                          "OR j.session_id IS NOT q.session_id))")
        if "sessions" in tables:
            checks.append("EXISTS (SELECT 1 FROM sessions s WHERE s.session_id=q.session_id "
                          "AND s.owner_user_id IS NOT q.owner_user_id)")
        conflict = " OR ".join(checks) if checks else "0"
        for row in conn.execute(
                f"SELECT q.root_job_id,q.finished_at,({conflict}) AS conflict "
                "FROM chat_turn_queue q WHERE q.owner_user_id=? AND q.finished_at>0", (owner,)):
            parts = _target_parts(row["root_job_id"], row["finished_at"])
            if parts is None:
                continue  # the writer could not have produced a scoped path
            target = _target(parts)
            if not _regular_target(target):
                continue
            if row["conflict"]:
                raise AccountDeletionPending("turn log ownership evidence conflicts")
            target.unlink(missing_ok=True)
