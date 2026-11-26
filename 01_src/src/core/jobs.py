"""Task jobs — 长任务最小闭环状态表（v8.3.7 M2）。

write 类长任务断连后保活执行，状态落 task_jobs 表，前端/接口可查询。
不做完整任务中心（无队列/优先级/暂停恢复/多节点）。

S1 增补（取消生命周期）
----------------------
* 状态机从四态扩为六态：`running / detached / cancel_requested / completed / failed / cancelled`；
* `detached` = 观察者离开但执行继续，**永不**被当作 user cancel；
* `cancel_requested` 与终态分离：前者只表示"已收到停止请求"，不代表线程已退出；
* 终态不可回退（`update_job` 内置守卫，除非显式 `force=True`）；
* 新增列走幂等迁移（SQLite 无 `ADD COLUMN IF NOT EXISTS`），重复执行无副作用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_DB_PATH = PROJECT_ROOT / "state" / "sessions.db"
_write_lock = threading.Lock()

TERMINAL_STATUS = frozenset({"completed", "failed", "cancelled", "awaiting_input"})
OPEN_STATUS = frozenset({"running", "detached", "cancel_requested"})
VALID_STATUS = OPEN_STATUS | TERMINAL_STATUS | {"queued"}

# 幂等迁移：(列名, 声明)
_MIGRATIONS = (
    ("cancel_requested_at", "TEXT NOT NULL DEFAULT ''"),
    ("detached_at", "TEXT NOT NULL DEFAULT ''"),
    ("stop_reason", "TEXT NOT NULL DEFAULT ''"),
    ("stop_note", "TEXT NOT NULL DEFAULT ''"),
    # Minimal terminal snapshot for detached recovery: status, display state,
    # history_saved, notices and verified artifact count.  It never contains
    # the answer body (that stays in session history) and never claims work
    # that has not finished.
    ("terminal_snapshot", "TEXT NOT NULL DEFAULT ''"),
    # Legacy rows have no owner/key and remain independent requests.
    ("owner_user_id", "INTEGER"),
    ("client_request_id", "TEXT NOT NULL DEFAULT ''"),
    ("payload_hash", "TEXT NOT NULL DEFAULT ''"),
    ("parent_job_id", "TEXT NOT NULL DEFAULT ''"),
    ("question_id", "TEXT NOT NULL DEFAULT ''"),
    ("native_context_scope", "TEXT NOT NULL DEFAULT ''"),
    ("history_hidden", "INTEGER NOT NULL DEFAULT 0"),
)


def unsettled_history_job_sql(conn, tables: set[str]) -> str:
    """Predicate on alias j: an awaiting segment needs proof its question closed."""
    safe = "j.status IN ('completed','failed','cancelled')"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_jobs)")}
    evidence = []
    if "owner_user_id" in cols and "chat_turn_queue" in tables:
        evidence.append("EXISTS (SELECT 1 FROM chat_turn_queue tq WHERE tq.state='terminal' "
                        "AND tq.owner_user_id=j.owner_user_id AND tq.session_id=j.session_id "
                        "AND j.job_id IN (tq.root_job_id,tq.active_child_job_id))")
    if {"owner_user_id", "question_id", "parent_job_id"}.issubset(cols) and "task_questions" in tables:
        question_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_questions)")}
        current_question = ("AND q.recovery_cursor=COALESCE((SELECT MAX(e.seq) FROM task_events e "
                            "WHERE e.job_id=q.job_id AND e.event='agent_recovery'),0) "
                            if "recovery_cursor" in question_cols and "task_events" in tables else "")
        evidence.append("EXISTS (SELECT 1 FROM task_questions q LEFT JOIN task_jobs c ON c.job_id=q.child_job_id "
                        "AND c.question_id=q.id AND c.parent_job_id=q.job_id "
                        "AND c.owner_user_id=q.owner_user_id AND c.session_id=q.session_id "
                        "WHERE q.job_id=j.job_id AND q.owner_user_id=j.owner_user_id AND q.session_id=j.session_id "
                        + current_question +
                        "AND ((q.status IN ('cancelled','expired') AND q.child_job_id='') "
                        "OR (q.status='answered' AND c.job_id IS NOT NULL)))")
    if evidence:
        safe += " OR (j.status='awaiting_input' AND (" + " OR ".join(evidence) + "))"
    return "(NOT COALESCE((" + safe + "), 0) OR j.current_step='code_reconciliation_required')"


def _migrate(conn) -> None:
    """补齐 S1 新增列。重复执行无副作用（列已存在则跳过）。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(task_jobs)").fetchall()}
    for name, decl in _MIGRATIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE task_jobs ADD COLUMN {name} {decl}")
    from src.core.task_events import init_schema
    init_schema(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_jobs_owner_client_request "
        "ON task_jobs(owner_user_id, client_request_id) "
        "WHERE owner_user_id IS NOT NULL AND client_request_id <> ''")


def _connect():
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # SQLite can return BUSY immediately when two fresh connections both
        # switch journal mode; busy_timeout does not cover that upgrade race.
        wal_deadline = time.monotonic() + 30.0
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= wal_deadline:
                    raise
                time.sleep(0.01)
        # Schema inspection and ALTER must be serialized across connections,
        # including two processes that first open a legacy database together.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_jobs (
                job_id TEXT PRIMARY KEY,
                session_id TEXT,
                request_id TEXT,
                job_type TEXT,
                status TEXT,
                current_step TEXT,
                progress_summary TEXT,
                result_path TEXT,
                error TEXT,
                created_at TEXT,
                updated_at TEXT,
                finished_at TEXT
            )
        """)
        _migrate(conn)
        conn.commit()
        return conn
    except BaseException:
        conn.rollback()
        conn.close()
        raise


@contextmanager
def read_transaction(*, path=None):
    """Read one initialized store snapshot without schema setup or writer locks."""
    uri = Path(path if path is not None else _DB_PATH).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.15)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        yield conn


def read_job(job_id, *, conn=None):
    if conn is None:
        with read_transaction() as reader:
            return read_job(job_id, conn=reader)
    row = conn.execute("SELECT * FROM task_jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


class IdempotencyConflict(ValueError):
    """The same owner's client key already identifies a different payload."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__("client_request_id is already bound to a different request")


@dataclass(frozen=True)
class JobClaim:
    """Only created=True permits dispatch; existing jobs are facts, not leases."""

    created: bool
    job: dict

    @property
    def job_id(self) -> str:
        return str(self.job["job_id"])


def claim_job(session_id: str, request_id: str, job_type: str = "chat", *,
              owner_user_id: int | None = None,
              client_request_id: str | None = None,
              payload: dict | None = None) -> JobClaim:
    """Atomically create or return an owner's durable HTTP request claim.

    For keyed requests, payload must be the complete JSON client request before
    server mutations: include the requested session, query, attachment content
    (or content digests), file references and behavior options. Exclude generated
    request/session IDs and upload IDs. The resolved session_id and request_id
    belong to the winning claim; retries return those original stored values.

    Every existing state, including running/interrupted/failed, returns
    created=False. No checkpoint recovery, side-effect replay or lease takeover
    is implied. Database failures propagate so callers cannot dispatch without a
    durable claim. No-key callers get a fresh independent job for legacy use.
    """
    if client_request_id is not None and not isinstance(client_request_id, str):
        raise ValueError("client_request_id must be a string")
    key = client_request_id or ""
    if key and (not key.strip() or len(key) > 64):
        raise ValueError("client_request_id must contain 1 to 64 characters")
    if owner_user_id is not None and (type(owner_user_id) is not int or owner_user_id <= 0):
        raise ValueError("owner_user_id must be a positive integer")
    digest = ""
    if key:
        if owner_user_id is None:
            raise ValueError("keyed requests require owner_user_id")
        if not isinstance(payload, dict):
            raise ValueError("keyed requests require the complete request payload")
        canonical = json.dumps(
            {"job_type": job_type, "payload": payload}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # SQLite, not the process-local lock, owns this compare-and-insert. The
    # unique partial index also protects against other writers/processes.
    with closing(_connect()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        if key:
            existing = conn.execute(
                "SELECT * FROM task_jobs WHERE owner_user_id=? AND client_request_id=?",
                (owner_user_id, key)).fetchone()
            if existing is not None:
                if existing["payload_hash"] != digest:
                    raise IdempotencyConflict(str(existing["job_id"]))
                return JobClaim(created=False, job=dict(existing))
        job_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO task_jobs (job_id, session_id, request_id, job_type, "
            "status, current_step, progress_summary, result_path, error, "
            "created_at, updated_at, finished_at, owner_user_id, client_request_id, payload_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, session_id, request_id, job_type, "running", "init", "", "", "",
             now, now, "", owner_user_id, key, digest))
        row = conn.execute("SELECT * FROM task_jobs WHERE job_id=?", (job_id,)).fetchone()
        return JobClaim(created=True, job=dict(row))


def cancel_queued_job(conn, job_id: str, *, reason: str = "turn_queue_cancelled") -> bool:
    """CAS a still-queued root job to cancelled inside the caller's transaction.

    The durable turn queue owns the transaction (and therefore the SQLite write
    lock). This helper must not open a connection or take ``_write_lock``; it
    applies the same terminal semantics as ``update_job`` without calling any
    stop or recovery path. Returns whether this call performed the transition.
    """
    if not job_id:
        return False
    now = datetime.now().isoformat()
    cursor = conn.execute(
        "UPDATE task_jobs SET status='cancelled', current_step='cancelled', "
        "stop_reason=?, updated_at=?, finished_at=? "
        "WHERE job_id=? AND status='queued'",
        (str(reason or ""), now, now, job_id))
    return bool(cursor.rowcount)


def create_job(session_id: str, request_id: str, job_type: str = "chat") -> str:
    """创建 job（每个 expert 请求一个；write 类工具执行时升级 job_type='write'）。"""
    job_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    try:
        with _write_lock:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO task_jobs (job_id, session_id, request_id, job_type, "
                    "status, current_step, progress_summary, result_path, error, "
                    "created_at, updated_at, finished_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, session_id, request_id, job_type, "running", "init",
                     "", "", "", now, now, ""),
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"[Jobs] create_job failed: {e}")
    return job_id


def update_job(job_id: str, *, force: bool = False, **fields) -> bool:
    """更新 job 字段（status/job_type/current_step/progress_summary/result_path/error）。

    status -> completed/failed/cancelled 时自动写 finished_at。
    **终态守卫**：已处于终态的 job 不允许被改成别的状态（否则会出现
    "已 cancelled 又被 finally 写成 completed"），需显式 `force=True` 才覆盖。
    返回是否实际写入。
    """
    if not job_id:
        return False
    allowed = {"job_type", "status", "current_step", "progress_summary",
               "result_path", "error", "stop_reason", "stop_note",
               "terminal_snapshot"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if updates.get("status") in TERMINAL_STATUS:
        updates["finished_at"] = datetime.now().isoformat()
    set_clauses = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values())
    try:
        with _write_lock:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT status FROM task_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None:
                    return False
                current = row[0]
                new_status = updates.get("status")
                if (
                    not force
                    and new_status
                    and current in TERMINAL_STATUS
                    and new_status != current
                ):
                    logger.warning(
                        "[Jobs] refused terminal downgrade %s: %s -> %s",
                        job_id, current, new_status,
                    )
                    return False
                conn.execute(
                    f"UPDATE task_jobs SET {set_clauses}, updated_at=? WHERE job_id=?",
                    values + [datetime.now().isoformat(), job_id],
                )
                conn.commit()
                return True
    except Exception as e:
        logger.warning(f"[Jobs] update_job failed: {e}")
        return False


def request_cancel(job_id: str, reason: str = "user") -> bool:
    """标记"已收到停止请求"。只从 running/detached 迁入，绝不写终态。

    重复点击停止返回 False：状态已是 cancel_requested 时不该再改一次时间戳，
    调用方据此判定 accepted=False，避免"又一次接受停止"的假象。
    """
    if not job_id:
        return False
    now = datetime.now().isoformat()
    try:
        with _write_lock:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE task_jobs SET status='cancel_requested', "
                    "cancel_requested_at=?, stop_reason=?, updated_at=? "
                    "WHERE job_id=? AND status IN ('running','detached')",
                    (now, reason, now, job_id),
                )
                conn.commit()
                return bool(cur.rowcount)
    except Exception as e:
        logger.warning(f"[Jobs] request_cancel failed: {e}")
        return False


def mark_detached(job_id: str) -> bool:
    """观察者离开（断线/关页面），执行继续。不覆盖 cancel_requested。"""
    if not job_id:
        return False
    now = datetime.now().isoformat()
    try:
        with _write_lock:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE task_jobs SET status='detached', detached_at=?, updated_at=? "
                    "WHERE job_id=? AND status='running'",
                    (now, now, job_id),
                )
                conn.commit()
                return bool(cur.rowcount)
    except Exception as e:
        logger.warning(f"[Jobs] mark_detached failed: {e}")
        return False


def mark_stopped(job_id: str, reason: str = "user", note: str = "") -> bool:
    """进入终态 cancelled。只从 open 态迁入；reason 必须是
    `user / detached_timeout / shutdown / internal` 之一。"""
    if not job_id:
        return False
    reason = reason or "internal"
    now = datetime.now().isoformat()
    try:
        with _write_lock:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE task_jobs SET status='cancelled', stop_reason=?, stop_note=?, "
                    "finished_at=?, updated_at=? "
                    "WHERE job_id=? AND status IN ('running','detached','cancel_requested')",
                    (reason, note, now, now, job_id),
                )
                conn.commit()
                return bool(cur.rowcount)
    except Exception as e:
        logger.warning(f"[Jobs] mark_stopped failed: {e}")
        return False


def mark_completed_if_open(job_id: str, note: str = "", **fields) -> bool:
    """任务正常结束：仅在 open 态写入终态。

    若此前收到过停止请求（`cancel_requested`），本次完成视为"停止请求晚于完成"，
    记 `stop_note`，状态仍为 `completed`（这是唯一允许的 cancel_requested → completed 迁移）。
    """
    if not job_id:
        return False
    payload = {k: v for k, v in fields.items()
               if k in {"current_step", "progress_summary", "result_path", "error"}}
    payload["status"] = "completed"
    if note:
        payload["stop_note"] = note
    now = datetime.now().isoformat()
    payload["finished_at"] = now
    set_clauses = ", ".join(f"{k}=?" for k in payload)
    try:
        with _write_lock:
            with _connect() as conn:
                cur = conn.execute(
                    f"UPDATE task_jobs SET {set_clauses}, updated_at=? "
                    "WHERE job_id=? AND status IN ('running','detached','cancel_requested')",
                    list(payload.values()) + [now, job_id],
                )
                conn.commit()
                return bool(cur.rowcount)
    except Exception as e:
        logger.warning(f"[Jobs] mark_completed_if_open failed: {e}")
        return False


def durable_terminal(conn, job):
    """Return a matching done/snapshot pair, never an interrupted or reviewed-only answer."""
    from src.core import task_events
    event = task_events.current_terminal_event(conn, job["job_id"])
    if event is None:
        return None
    try:
        fact = json.loads(event["data"])
        envelope = fact.get("turn_envelope")
        if (fact.get("recovery") == "interrupted_terminal" or fact.get("job_id") != job["job_id"]
                or fact.get("session_id", job["session_id"]) != job["session_id"]
                or not isinstance(envelope, dict) or envelope != json.loads(job["terminal_snapshot"])
                or envelope.get("job_status") not in TERMINAL_STATUS
                or (job["status"] in TERMINAL_STATUS and envelope["job_status"] != job["status"])
                or type(envelope.get("history_saved")) is not bool):
            return None
        return envelope
    except (AttributeError, TypeError, ValueError):
        return None


_RESTART_SUMMARY = "服务进程在任务完成前停止；可显式申请恢复。"


def _mark_interrupted(conn, job_id, now):
    conn.execute("UPDATE task_jobs SET status='failed',current_step='interrupted',error='service_restart',"
                 "progress_summary=?,updated_at=?,finished_at=? WHERE job_id=?",
                 (_RESTART_SUMMARY, now, now, job_id))


def mark_interrupted_jobs() -> int:
    """Classify abandoned startup work in the job/segment/event transaction."""
    from src.core import task_events
    now = datetime.now().isoformat()
    count = 0
    with _write_lock, task_events.transaction() as conn:
        has_segments = conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_execution_segments'").fetchone()
        phase = "s.phase" if has_segments else "NULL AS phase"
        join = (" LEFT JOIN agent_execution_segments s USING(job_id)" if has_segments else "")
        for job in conn.execute(f"SELECT j.*,{phase} FROM task_jobs j{join} "
                                "WHERE j.status IN ('running','detached','cancel_requested')").fetchall():
            run_id = job["job_id"]
            terminal = durable_terminal(conn, job)
            if terminal:
                status = terminal["job_status"]
                conn.execute("UPDATE task_jobs SET status=?,current_step=?,updated_at=?,finished_at=? WHERE job_id=?",
                             (status, status, now, now, run_id))
                if job["phase"]:
                    conn.execute("UPDATE agent_execution_segments SET phase='terminal',generation=generation+1,token=? WHERE job_id=?",
                                 (uuid.uuid4().hex, run_id))
                continue
            if job["phase"] in {"parked", "cancel_parked"}:
                continue
            if job["phase"] and job["status"] == "cancel_requested":
                # Worker execution, if any, is checked by the coordinator before completion.
                conn.execute("UPDATE agent_execution_segments SET phase='cancel_parked',generation=generation+1,token=? WHERE job_id=?",
                             (uuid.uuid4().hex, run_id))
                continue
            if job["phase"] in {"parking", "resuming", "active", "restart_wait"}:
                # A protected wait can commit before the outer API records its
                # reference. Startup inspects the exact protected graph either way.
                conn.execute("UPDATE agent_execution_segments SET phase='restart_wait',generation=generation+1,token=? WHERE job_id=?",
                             (uuid.uuid4().hex, run_id))
                continue
            if job["phase"]:
                conn.execute("UPDATE agent_execution_segments SET phase='interrupted',generation=generation+1,token=? WHERE job_id=?",
                             (uuid.uuid4().hex, run_id))
            _mark_interrupted(conn, run_id, now)
            count += 1
        task_events.recover_interrupted(conn)
    if count:
        logger.warning("[Jobs] marked %d stale open jobs as interrupted", count)
    return count


def require_settled_operations(conn, job_id, *, reason="recovery_requires_reconciliation"):
    """Require durable ledger facts without treating any as authorization to replay."""
    from src.core.task_events import TaskConflict
    if conn.execute("SELECT 1 FROM task_operations WHERE job_id=? "
                    "AND (status IN ('running','interrupted','unknown') OR result='') LIMIT 1", (job_id,)).fetchone():
        raise TaskConflict(reason)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_batch_journal'").fetchone():
        for row in conn.execute("SELECT status,observation FROM agent_batch_journal WHERE run_id=?", (job_id,)):
            try:
                batch = json.loads(row["observation"])
            except (TypeError, ValueError):
                raise TaskConflict(reason) from None
            if row["status"] != "complete" or not isinstance(batch, dict) or batch.get("unknown_operations"):
                raise TaskConflict(reason)


def claim_agent_recovery(identity, *, answered: bool = False, account_epoch=None) -> dict:
    """CAS one authenticated startup-interrupted run back into its normal lifecycle.

    Called only after protected checkpoint validation and account/session checks.
    No new job, user turn, quota reservation, deadline, or budget is created.
    """
    from src.core import task_events
    from src.agent.identity import ExecutionIdentity
    if not isinstance(identity, ExecutionIdentity):
        raise task_events.TaskConflict("recovery_identity_required")
    with task_events.transaction() as conn:
        job = task_events._job(conn, identity.run_id)
        if (job["owner_user_id"] != identity.owner_user_id
                or job["session_id"] != identity.session_id
                or job["request_id"] != identity.user_turn_id):
            raise task_events.TaskConflict("recovery_identity_mismatch")
        if (job["status"] != "failed" or job["current_step"] != "interrupted"
                or job["error"] != "service_restart" or job["cancel_requested_at"]):
            raise task_events.TaskConflict("recovery_not_available")
        from src.core import chat_turn_queue
        queue_turn = chat_turn_queue._reopen_for_recovery(conn, job, account_epoch=account_epoch)
        other_statuses = "('running','detached','cancel_requested')" if queue_turn else "('running','detached','cancel_requested','queued')"
        if conn.execute("SELECT 1 FROM task_jobs WHERE owner_user_id=? AND session_id=? AND job_id<>? AND status IN " + other_statuses,
                        (identity.owner_user_id, identity.session_id, identity.run_id)).fetchone():
            raise task_events.TaskConflict("recovery_session_active")
        segment = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_execution_segments'").fetchone():
            segment = conn.execute("SELECT * FROM agent_execution_segments WHERE job_id=?", (identity.run_id,)).fetchone()
        claim = None
        if segment:
            if segment["phase"] != "interrupted" or segment["account_epoch"] != account_epoch:
                raise task_events.TaskConflict("recovery_segment_unavailable")
            from src.agent.execution_segments import ExecutionSegmentClaim
            claim = ExecutionSegmentClaim(identity, segment["generation"] + 1, uuid.uuid4().hex)
            conn.execute("UPDATE agent_execution_segments SET phase='recovering',generation=?,token=?,checkpoint_ref='',wait_ids='[]' WHERE job_id=?",
                         (claim.generation, claim.token, identity.run_id))
        terminal = task_events.current_terminal_event(conn, identity.run_id)
        if terminal is None or json.loads(terminal[2]).get("recovery") != "interrupted_terminal":
            raise task_events.TaskConflict("recovery_terminal_conflict")
        require_settled_operations(conn, identity.run_id)
        budget = conn.execute("SELECT * FROM task_budgets WHERE job_id=?", (identity.run_id,)).fetchone()
        if budget is None:
            raise task_events.TaskConflict("recovery_budget_missing")
        if not answered and (budget["deadline"] <= time.time() or budget["calls"] >= budget["max_calls"]
                             or budget["tokens"] >= budget["max_tokens"]):
            raise task_events.TaskConflict("recovery_budget_exhausted")
        changed = conn.execute("UPDATE task_jobs SET status='running',current_step='recovering',error='',finished_at='',terminal_snapshot='',updated_at=? WHERE job_id=? AND status='failed' AND current_step='interrupted' AND error='service_restart'",
                               (datetime.now().isoformat(), identity.run_id)).rowcount
        if changed != 1:
            raise task_events.TaskConflict("recovery_claim_lost")
        # Only an authorized claim reopens restart-deferred task updates. The
        # host watermark prevents already checkpointed instructions duplicating.
        reactivated_updates = [dict(row) for row in conn.execute(
            "SELECT version,instruction FROM task_updates WHERE job_id=? AND status='not_applied' ORDER BY version",
            (identity.run_id,))]
        conn.execute("UPDATE task_updates SET status='pending' WHERE job_id=? AND status='not_applied'", (identity.run_id,))
        event = task_events._append(conn, identity.run_id, "agent_recovery", {
            "job_id": identity.run_id, "request_id": identity.user_turn_id,
            "status": "admitted", "previous_terminal_seq": terminal[0],
            "reactivated_updates": reactivated_updates})
        # Old pending questions are no longer a runnable decision. Expiry
        # owns pin deletion, so a rollback cannot expose a still-current pin.
        conn.execute("UPDATE task_questions SET status='expired', "
                     "pin_until=CASE WHEN pin_until>0 THEN MIN(pin_until,?) ELSE 0 END "
                     "WHERE job_id=? AND recovery_cursor<? AND status='pending'",
                     (time.time(), identity.run_id, event["seq"]))
        return {"budget": dict(budget), "recovery_cursor": event["seq"], "claim": claim}


def get_job(job_id: str) -> Optional[dict]:
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM task_jobs WHERE job_id=?",
                               (job_id,)).fetchone()
            if row is None:
                return None
            return dict(row)
    except Exception as e:
        logger.warning(f"[Jobs] get_job failed: {e}")
        return None


def bind_native_context_scope(identity, scope: str) -> bool:
    with closing(_connect()) as conn, conn:
        updated = conn.execute(
            "UPDATE task_jobs SET native_context_scope=? WHERE job_id=? AND owner_user_id=? "
            "AND session_id=? AND request_id=? AND native_context_scope IN ('',?)",
            (scope, identity.run_id, identity.owner_user_id, identity.session_id, identity.user_turn_id, scope))
        return updated.rowcount == 1


def save_terminal_snapshot(job_id: str, snapshot: dict) -> bool:
    """Persist the minimal terminal view for detached/reconnect recovery."""
    if not job_id or not isinstance(snapshot, dict):
        return False
    try:
        payload = json.dumps(snapshot, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("[Jobs] terminal snapshot encode failed: %s", exc)
        return False
    # Once the durable done frame exists it owns the stamped terminal sequence.
    # API cleanup may still hold the pre-stamp envelope; it must not overwrite it.
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            from src.core.task_events import current_terminal_event
            row = current_terminal_event(conn, job_id)
            if row is not None:
                envelope = json.loads(row[2]).get("turn_envelope")
                if isinstance(envelope, dict):
                    payload = json.dumps(envelope, ensure_ascii=False)
            cursor = conn.execute("UPDATE task_jobs SET terminal_snapshot=?,updated_at=? WHERE job_id=?",
                                  (payload, datetime.now().isoformat(), job_id))
            return bool(cursor.rowcount)
    except Exception as exc:
        logger.warning("[Jobs] terminal snapshot save failed: %s", exc)
        return False


def decode_terminal_snapshot(job: Optional[dict]) -> dict:
    """Parse the stored snapshot; missing/corrupt data is an empty view."""
    raw = str((job or {}).get("terminal_snapshot") or "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    from src.core.turn_envelope import public_terminal_snapshot
    return public_terminal_snapshot(value) if isinstance(value, dict) else {}


def list_for_session(session_id: str, limit: int = 10, *,
                     include_active: bool = False,
                     owner_user_id: int | None = None, visible_only: bool = False, conn=None) -> list:
    """Recent jobs, optionally retaining every live or waiting task as well."""
    if owner_user_id is not None and (type(owner_user_id) is not int or owner_user_id <= 0):
        return []
    scope = "session_id=?"
    params = [session_id]
    if owner_user_id is not None:
        scope += " AND owner_user_id=?"
        params.append(owner_user_id)
    if visible_only:
        scope += " AND history_hidden=0"
    query = f"SELECT * FROM task_jobs WHERE {scope}"
    if include_active:
        active = sorted(OPEN_STATUS | {"queued", "awaiting_input"})
        placeholders = ",".join("?" for _ in active)
        query += (f" AND (status IN ({placeholders}) OR job_id IN ("
                  f"SELECT job_id FROM task_jobs WHERE {scope} "
                  "ORDER BY created_at DESC LIMIT ?)) ORDER BY created_at DESC")
        params = [*params, *active, *params, limit]
    else:
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
    try:
        with (read_transaction() if conn is None else nullcontext(conn)) as reader:
            return [dict(row) for row in reader.execute(query, params)]
    except sqlite3.Error as exc:
        if conn is not None:
            raise
        logger.warning("[Jobs] list_for_session failed: %s", type(exc).__name__)
        return []


def sessions_with_jobs(session_ids: list[str], owner_user_id: int, *,
                       visible_only: bool = False) -> set[str]:
    """Batch task presence for an owner's listed sessions, including empty ones."""
    if type(owner_user_id) is not int or owner_user_id <= 0:
        return set()
    keys = list(dict.fromkeys(sid for sid in session_ids if isinstance(sid, str) and sid))
    if not keys:
        return set()
    result = set()
    with closing(_connect()) as conn:
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            result.update(row[0] for row in conn.execute(
                f"SELECT DISTINCT session_id FROM task_jobs WHERE owner_user_id=? "
                f"AND session_id IN ({placeholders})" + (" AND history_hidden=0" if visible_only else ""),
                [owner_user_id, *batch]))
    return result


def purge_protocol_for_owner(owner_user_id: int) -> None:
    """Purge task payloads before account deletion removes the parent jobs."""
    from pathlib import Path
    if not Path(_DB_PATH).is_file():
        return
    # Account cleanup must inspect an existing store, never bootstrap a partial
    # sessions database for an account that has not submitted a task.
    with closing(sqlite3.connect(str(_DB_PATH), timeout=30)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "task_jobs" not in tables:
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_jobs)")}
        if "owner_user_id" not in columns:
            return
        for table in ("task_events", "task_update_requests", "task_updates", "task_approvals", "task_operations",
                      "task_model_attempts", "task_budgets", "task_questions",
                      "agent_execution_segments"):
            if table in tables:
                conn.execute(f"DELETE FROM {table} WHERE job_id IN ("
                             "SELECT job_id FROM task_jobs WHERE owner_user_id=?)", (int(owner_user_id),))


def find_chat_job_id(session_id: str, owner_user_id: int, client_request_id: str, *, conn=None, visible_only=False) -> str | None:
    """Read the original chat claim without admission, recovery or dispatch."""
    if conn is None:
        with read_transaction() as reader:
            return find_chat_job_id(session_id, owner_user_id, client_request_id, conn=reader, visible_only=visible_only)
    row = conn.execute(
        "SELECT job_id FROM task_jobs WHERE owner_user_id=? AND client_request_id=? "
        "AND session_id=? AND job_type='chat'" + (" AND history_hidden=0" if visible_only else ""),
        (owner_user_id, client_request_id, session_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def jobs_for_turns(session_id: str, owner_user_id: int, request_ids: list[str], *,
                   visible_only: bool = False) -> dict[str, dict]:
    """Resolve exact owner/session turn identities, never recent jobs or prose.

    client_request_id has a durable unique index. Legacy request_id is accepted
    only when unambiguous; duplicate legacy rows produce no guessed match.
    """
    if type(owner_user_id) is not int or owner_user_id <= 0:
        return {}
    keys = list(dict.fromkeys(x for x in request_ids if isinstance(x, str) and x))
    if len(keys) > 500:
        raise ValueError("at most 500 turn identities")
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM task_jobs WHERE session_id=? AND owner_user_id=? AND "
            f"(client_request_id IN ({placeholders}) OR request_id IN ({placeholders}))"
            + (" AND history_hidden=0" if visible_only else ""),
            [session_id, owner_user_id, *keys, *keys])]
    result = {}
    for key in keys:
        exact = [r for r in rows if r["client_request_id"] == key]
        candidates = exact or [r for r in rows if r["request_id"] == key]
        if len(candidates) == 1:
            result[key] = candidates[0]
    return result


def is_write_job(job_id: str) -> bool:
    """断连保活判定：仅 write 类任务继续后台执行（S1 起普通问答也不再因断线取消）。"""
    job = get_job(job_id)
    return bool(job and job.get("job_type") == "write")
