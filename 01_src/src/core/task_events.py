"""Durable task protocol on the existing jobs database and coordinator.

Events are facts, queues are bounded wakeups. Reading a terminal view never
reopens a job. Explicit agent recovery creates a new event generation in the
same run; resuming one explicitly safe operation remains a separate action.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import closing, contextmanager, nullcontext

PROTOCOL = "citrus-task/1"
LIVE_QUEUE_SIZE = 128
OPERATION_LEASE_SECONDS = 600
SAFE_RECOVERY_MODES = frozenset({"local_read", "pure_computation"})


class TaskConflict(ValueError):
    pass


def init_schema(conn):
    from src.core.user_questions import init_schema as init_questions
    init_questions(conn)
    for statement in (
        "CREATE TABLE IF NOT EXISTS task_events (job_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(job_id,seq))",
        "CREATE INDEX IF NOT EXISTS idx_task_events_type ON task_events(job_id,event)",
        "CREATE TABLE IF NOT EXISTS task_updates (job_id TEXT NOT NULL, version INTEGER NOT NULL, instruction TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, applied_at REAL, PRIMARY KEY(job_id,version))",
        "CREATE TABLE IF NOT EXISTS task_update_requests (job_id TEXT NOT NULL, request_id TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(job_id,request_id), FOREIGN KEY(job_id,version) REFERENCES task_updates(job_id,version) ON DELETE CASCADE)",
        "CREATE TABLE IF NOT EXISTS task_approvals (approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, tool_name TEXT NOT NULL, args_hash TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, decided_at REAL)",
        "CREATE TABLE IF NOT EXISTS task_operations (operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL, tool_name TEXT NOT NULL, input_hash TEXT NOT NULL, inputs TEXT NOT NULL, input_refs TEXT NOT NULL, recovery_mode TEXT NOT NULL, status TEXT NOT NULL, lease TEXT NOT NULL, lease_until REAL NOT NULL, attempts INTEGER NOT NULL, result TEXT NOT NULL DEFAULT '', result_refs TEXT NOT NULL DEFAULT '[]', outcome TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_task_operations_job ON task_operations(job_id)",
        "CREATE TABLE IF NOT EXISTS task_budgets (job_id TEXT PRIMARY KEY, deadline REAL NOT NULL, max_calls INTEGER NOT NULL, max_tokens INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS task_model_attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL, reserved_tokens INTEGER NOT NULL, usage TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)",
    ):
        conn.execute(statement)
    operation_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_operations)")}
    if "retry_of" not in operation_columns:
        conn.execute("ALTER TABLE task_operations ADD COLUMN retry_of TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_task_operation_retry "
                 "ON task_operations(retry_of) WHERE retry_of IS NOT NULL")
    if "tool_call_id" not in operation_columns:
        conn.execute("ALTER TABLE task_operations ADD COLUMN tool_call_id TEXT NOT NULL DEFAULT ''")
    if "logical_key" not in operation_columns:
        conn.execute("ALTER TABLE task_operations ADD COLUMN logical_key TEXT NOT NULL DEFAULT ''")
    if "dispatch_attempt" not in operation_columns:
        conn.execute("ALTER TABLE task_operations ADD COLUMN dispatch_attempt INTEGER NOT NULL DEFAULT 1")
    if "host_observation" not in operation_columns:
        conn.execute("ALTER TABLE task_operations ADD COLUMN host_observation TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_task_operation_call "
                 "ON task_operations(job_id,tool_call_id) WHERE logical_key <> ''")
    attempt_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_model_attempts)")}
    if "budget_scope" not in attempt_columns:
        conn.execute("ALTER TABLE task_model_attempts ADD COLUMN budget_scope TEXT NOT NULL DEFAULT 'execution'")
    if "observation" not in attempt_columns:
        conn.execute("ALTER TABLE task_model_attempts ADD COLUMN observation TEXT NOT NULL DEFAULT '{}'")
    budget_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_budgets)")}
    for name in ("final_tokens", "final_calls"):
        if name not in budget_columns:
            conn.execute(f"ALTER TABLE task_budgets ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")


@contextmanager
def transaction():
    from src.core import jobs
    with closing(jobs._connect()) as conn, conn:
        conn.row_factory = __import__('sqlite3').Row
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def read_transaction(conn=None):
    from src.core.jobs import read_transaction as read_jobs
    return read_jobs() if conn is None else nullcontext(conn)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def input_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _job(conn, job_id):
    row = conn.execute("SELECT * FROM task_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise TaskConflict("job_not_found")
    return row


def recovery_cursor(conn, job_id):
    """Latest explicit admission starts a new event generation, never a new run."""
    return conn.execute("SELECT COALESCE(MAX(seq),0) FROM task_events WHERE job_id=? AND event='agent_recovery'",
                        (job_id,)).fetchone()[0]


def current_terminal_event(conn, job_id):
    return conn.execute(
        "SELECT seq,event,data,created_at FROM task_events WHERE job_id=? AND event='done' AND seq>? ORDER BY seq LIMIT 1",
        (job_id, recovery_cursor(conn, job_id))).fetchone()


def _append(conn, job_id, event, data):
    _job(conn, job_id)
    from src.core.progress_bus import stamp_sse_event, _encode_event
    from src.core.public_delivery import project_public_event
    if event != "task_state":
        data = project_public_event(event, data)
    if event == "text" and "content" not in data:
        return None
    if event == "assistant_progress":
        if not data.get("status") or data.get("job_id") != job_id:
            return None
        # The native tool update may be observed again after recovery.
        if conn.execute("SELECT 1 FROM task_events WHERE job_id=? AND event='assistant_progress' "
                "AND json_extract(data, '$.origin')='langgraph' "
                "AND json_extract(data, '$.note_id')=? LIMIT 1",
                (job_id, data["note_id"])).fetchone():
            return None
    terminal = current_terminal_event(conn, job_id)
    if terminal is not None and event not in {"operation", "agent_recovery", "context_maintenance"}:
        # One terminal frame per explicitly admitted event generation. Earlier
        # terminal facts remain unchanged; safe-step recovery emits operation facts.
        return {"seq": terminal[0], "event": terminal[1], "data": project_public_event(terminal[1], json.loads(terminal[2])), "created_at": terminal[3]}
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM task_events WHERE job_id=?", (job_id,)).fetchone()[0]
    # task_state is also the model's private durable snapshot. Only its
    # public event view is reduced, without changing the event or sequence.
    now = time.time()
    if event == "done":
        data["turn_timings"] = _terminal_timings(conn, job_id, data, now)
        if isinstance(data.get("turn_envelope"), dict):
            data["turn_envelope"]["turn_timings"] = data["turn_timings"]
    frame = stamp_sse_event(_encode_event(event, data), seq)
    payload = json.loads(frame["data"])
    conn.execute("INSERT INTO task_events VALUES (?,?,?,?,?)", (job_id, seq, event, canonical(payload), now))
    if event == "done" and isinstance(payload.get("turn_envelope"), dict):
        conn.execute("UPDATE task_jobs SET terminal_snapshot=? WHERE job_id=?", (canonical(payload["turn_envelope"]), job_id))
    return {"seq": seq, "event": event,
            "data": project_public_event(event, payload) if event == "task_state" else payload,
            "created_at": now}


def _terminal_timings(conn, job_id, payload, now):
    from src.core.public_delivery import TurnTimingEvidence
    turn = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='chat_turn_queue' AND type='table'").fetchone():
        turn = conn.execute("SELECT accepted_at,started_at FROM chat_turn_queue WHERE root_job_id=?",
                            (job_id,)).fetchone()
    evidence = TurnTimingEvidence(accepted_at=turn[0] if turn else None, started_at=turn[1] if turn else None)
    for purpose, observation, started_at in conn.execute(
            "SELECT purpose,observation,created_at FROM task_model_attempts WHERE job_id=? ORDER BY created_at",
            (job_id,)):
        evidence.attempt(purpose, json.loads(observation), started_at=started_at)
    for event, data, created_at in conn.execute(
            "SELECT event,data,created_at FROM task_events WHERE job_id=? AND event IN "
            "('answer_start','text','text_reset','agent_recovery','done','tool_result') ORDER BY seq", (job_id,)):
        evidence.observe(event, json.loads(data), created_at=created_at)
    evidence.observe("done", payload, created_at=now)
    return evidence.values()


def public_working_notes(job_id):
    from src.core.public_delivery import project_public_event
    with read_transaction() as conn:
        rows = conn.execute("SELECT seq,data FROM task_events WHERE job_id=? AND event='assistant_progress' "
                            "AND json_extract(data, '$.origin')='langgraph' "
                            "AND json_extract(data, '$.source')='write_todos' "
                            "ORDER BY seq DESC LIMIT ?", (job_id, LIVE_QUEUE_SIZE)).fetchall()
    notes = []
    for row in reversed(rows):
        data = project_public_event("assistant_progress", json.loads(row["data"]))
        if data.get("status"):
            notes.append({"seq": row["seq"], "event": "assistant_progress", "data": data | {"seq": row["seq"]}})
    return notes


def append_event(job_id, event, data):
    with transaction() as conn:
        return _append(conn, job_id, event, data)


def _latest_context_snapshot(conn, job_id):
    row = conn.execute("SELECT seq,data FROM task_events WHERE job_id=? AND event='context_snapshot' ORDER BY seq DESC LIMIT 1", (job_id,)).fetchone()
    if not row:
        return None
    from src.core.public_delivery import project_public_event
    return project_public_event("context_snapshot", dict(json.loads(row["data"]), seq=row["seq"]))


def latest_context_snapshot(job_id):
    """Read the same identified event consumed by live SSE and replay."""
    with read_transaction() as conn:
        _job(conn, job_id)
        return _latest_context_snapshot(conn, job_id)


def replay(job_id, after=0, limit=200, *, conn=None):
    after, limit = max(0, int(after)), min(500, max(1, int(limit)))
    with read_transaction(conn) as conn:
        job = dict(_job(conn, job_id))
        rows = conn.execute("SELECT * FROM task_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, after, limit + 1)).fetchall()
        events = [dict(row) | {"data": json.loads(row["data"])} for row in rows[:limit]]
        from src.core.public_delivery import project_public_event
        # Old previews were written as unmarked text and later reset. Keep
        # their cursors. The terminal frame itself still supplies the legacy
        # final answer, including when the reset lies beyond this page.
        for event in events:
            payload = event["data"]
            if (event["event"] == "text" and isinstance(payload, dict)
                    and payload.get("delivery") != "committed"):
                payload = {}
            event["data"] = project_public_event(event["event"], payload) | {"seq": event["seq"]}
        return {"protocol": PROTOCOL, "job_id": job_id, "events": events,
                "next_cursor": events[-1]["seq"] if events else after,
                "has_more": len(rows) > limit, "status": job["status"],
                "recovery_cursor": recovery_cursor(conn, job_id),
                "terminal_snapshot": public_job_terminal(job),
                "context_snapshot": _latest_context_snapshot(conn, job_id)}


def _public_terminal(envelope):
    from src.core.progress_bus import public_tool_reason, public_tool_resolutions
    from src.core.public_delivery import private_record, project_artifact_delivery, project_fields, timing_fields
    if not isinstance(envelope, dict) or private_record(envelope):
        return {}
    visible = project_fields(envelope,
        ids="display_status execution_status acceptance_status acceptance_reason job_status stop_reason",
        numbers="schema_version verified_artifacts terminal_seq", bools="history_saved")
    if isinstance(envelope.get("turn_timings"), dict):
        visible["turn_timings"] = timing_fields(envelope["turn_timings"])
    if isinstance(envelope.get("artifact_ids"), (list, tuple)):
        visible["artifact_ids"] = [item for item in envelope["artifact_ids"]
                                  if project_fields({"id": item}, ids="id").get("id")]
    if isinstance(envelope.get("artifact_delivery"), dict):
        visible["artifact_delivery"] = project_artifact_delivery(envelope["artifact_delivery"])
    if "tool_resolutions" in envelope:
        visible["tool_resolutions"] = public_tool_resolutions(envelope["tool_resolutions"])
    snapshot = envelope.get("task_snapshot")
    if isinstance(snapshot, dict):
        outcome = snapshot.get("turn_outcome")
        visible["task_snapshot"] = {"turn_outcome": {key: public_tool_reason(outcome[key])
            for key in ("status", "reason_code", "stop_reason") if key in outcome
        }} if isinstance(outcome, dict) else {}
    # Notices may embed requirement labels or model-supplied reasons. Keep only
    # system-owned notices and never replay arbitrary prose as progress.
    if isinstance(envelope.get("notices"), (list, tuple)):
        visible["notices"] = [note for note in envelope["notices"] if isinstance(note, str) and note in (
            "本轮已停止", "本轮回答已完成，但历史写入未确认；刷新后可能无法恢复。",
            "服务进程在任务完成前停止；可恢复已验证的安全步骤。",
            "主模型尚未配置，请联系管理员配置模型后重试。附件仍可在资料库中查看。",
            "处理请求时发生内部错误，请稍后重试",
        )]
    return visible


def public_job_terminal(job):
    from src.core.jobs import decode_terminal_snapshot
    return _public_terminal(decode_terminal_snapshot(dict(job)))


_JOB_SUMMARIES = {
    "queued": "正在排队", "preparing_attachments": "正在安全解析本轮附件",
    "starting": "正在启动本轮请求", "waiting_code": "正在等待代码执行",
    "code_cancel_pending": "等待执行队列确认停止", "awaiting_input": "正在等待你的选择",
    "interrupted": "服务进程在任务完成前停止；可显式申请恢复。",
    "running": "正在处理任务", "detached": "任务在后台处理",
    "cancel_requested": "正在停止任务", "cancelled": "任务已停止",
    "completed": "任务已完成", "failed": "任务未完成",
}


def public_job_record(job):
    """The status/list surface must not copy persisted job prose or paths."""
    from src.core.progress_bus import public_tool_reason
    job = dict(job)
    visible = {key: job[key] for key in (
        "job_id", "session_id", "request_id", "client_request_id", "job_type", "status",
        "current_step", "created_at", "updated_at", "finished_at", "owner_user_id",
        "parent_job_id", "question_id", "cancel_requested_at", "detached_at",
    ) if key in job}
    return visible | {"terminal_snapshot": public_job_terminal(job),
                      "progress_summary": _JOB_SUMMARIES.get(job.get("current_step"),
                                          _JOB_SUMMARIES.get(job.get("status"), "")),
                      "error": (public_tool_reason(job.get("error")) or "task_failed") if job.get("error") else "",
                      "stop_reason": public_tool_reason(job.get("stop_reason"))}


def projection(job_id, *, include_job=False, conn=None):
    from src.core.progress_bus import public_progress_payload, public_result_refs, public_tool_outcome
    with read_transaction(conn) as conn:
        job = _job(conn, job_id)
        status = public_job_record(job) if include_job else {}
        updates = [dict(r) for r in conn.execute(
            "SELECT job_id,version,status,created_at,applied_at FROM task_updates WHERE job_id=? ORDER BY version", (job_id,))]
        operations = []
        for row in conn.execute("SELECT * FROM task_operations WHERE job_id=? AND owner_user_id=? "
                                "AND owner_user_id>0 ORDER BY updated_at", (job_id, job["owner_user_id"])):
            item = public_progress_payload("operation", dict(row))
            item.update({key: row[key] for key in ("job_id", "recovery_mode", "attempts", "updated_at")})
            item["status"] = row["status"]
            item["result_available"] = bool(row["result"])
            item["result_url"] = f"/jobs/{job_id}/operations/{item['operation_id']}/result" if item["result_available"] else ""
            item["result_refs"] = public_result_refs(json.loads(row["result_refs"]))
            outcome = json.loads(row["outcome"])
            item["outcome"] = public_tool_outcome(outcome) if outcome else {}
            item["resumable"] = (job["current_step"] == "interrupted"
                                 and job["status"] == "failed"
                                 and job["owner_user_id"] is not None
                                 and item["recovery_mode"] in SAFE_RECOVERY_MODES
                                 and bool(row["tool_call_id"]) and item["status"] == "interrupted")
            operations.append(item)
        budget_row = conn.execute("SELECT * FROM task_budgets WHERE job_id=?", (job_id,)).fetchone()
        budget = dict(budget_row) if budget_row else {}
        if budget:
            budget.update(_usage_totals(conn, job_id))
        if budget and not __import__('math').isfinite(budget["deadline"]):
            budget["deadline"] = None
        return {**status,
                "recovery_cursor": recovery_cursor(conn, job_id),
                "event_cursor": conn.execute("SELECT COALESCE(MAX(seq),0) FROM task_events WHERE job_id=?", (job_id,)).fetchone()[0],
                "task_version": updates[-1]["version"] if updates else 0,
                "applied_version": max((u["version"] for u in updates if u["status"] == "applied"), default=0),
                "pending_updates": [u for u in updates if u["status"] == "pending"],
                "operations": operations,
                "context_snapshot": _latest_context_snapshot(conn, job_id),
                "approvals": [public_progress_payload("permission_decision", dict(row)) |
                              {"created_at": row["created_at"], "decided_at": row["decided_at"]}
                              for row in conn.execute("SELECT * FROM task_approvals WHERE job_id=? ORDER BY created_at", (job_id,))],
                "budget": budget}


def _update_request(conn, job_id, request_id):
    return conn.execute("SELECT task_updates.version, instruction, status FROM task_update_requests "
                        "JOIN task_updates USING (job_id,version) WHERE job_id=? AND request_id=?",
                        (job_id, request_id)).fetchone()


def update_request_status(job_id, request_id, *, conn=None):
    with read_transaction(conn) as conn:
        _job(conn, job_id)
        existing = _update_request(conn, job_id, request_id)
        return ({"job_id": job_id, "version": existing["version"], "status": existing["status"]}
                if existing is not None else None)


def submit_update(job_id, expected_version, instruction, *, request_id=None):
    instruction = str(instruction).strip()
    if not instruction or len(instruction) > 8000:
        raise ValueError("instruction must contain 1 to 8000 characters")
    with transaction() as conn:
        job = _job(conn, job_id)
        if request_id is not None:
            existing = _update_request(conn, job_id, request_id)
            if existing is not None:
                if existing["version"] != expected_version + 1 or existing["instruction"] != instruction:
                    raise TaskConflict("task_update_request_conflict")
                return {"job_id": job_id, "version": existing["version"], "status": existing["status"]}
        if job["status"] not in {"running", "detached"} or job["current_step"] == "finalizing":
            raise TaskConflict("task_not_running")
        version = conn.execute("SELECT COALESCE(MAX(version),0) FROM task_updates WHERE job_id=?", (job_id,)).fetchone()[0]
        if expected_version != version:
            raise TaskConflict("task_version_conflict")
        version += 1
        conn.execute("INSERT INTO task_updates VALUES (?,?,?,'pending',?,NULL)", (job_id, version, instruction, time.time()))
        if request_id is not None:
            conn.execute("INSERT INTO task_update_requests VALUES (?,?,?)", (job_id, request_id, version))
        _append(conn, job_id, "task_update", {"version": version, "status": "pending"})
        return {"job_id": job_id, "version": version, "status": "pending"}


def fence_final_answer(identity, expected_version):
    """Atomically compare the task's update watermark and stop accepting updates."""
    with transaction() as conn:
        job = _job(conn, identity.run_id)
        if (job["owner_user_id"] != identity.owner_user_id or job["session_id"] != identity.session_id
                or job["request_id"] != identity.user_turn_id):
            raise TaskConflict("identity_conflict")
        if job["status"] not in {"running", "detached"} or job["cancel_requested_at"]:
            raise TaskConflict("task_not_running")
        version = conn.execute("SELECT COALESCE(MAX(version),0) FROM task_updates WHERE job_id=?",
                               (identity.run_id,)).fetchone()[0]
        if version != expected_version:
            return False
        conn.execute("UPDATE task_jobs SET current_step='finalizing' WHERE job_id=?", (identity.run_id,))
        return True


def apply_updates(job_id):
    if not job_id:
        return []
    with transaction() as conn:
        job = _job(conn, job_id)
        if job["status"] not in {"running", "detached"}:
            return []
        rows = [dict(r) for r in conn.execute("SELECT * FROM task_updates WHERE job_id=? AND status='pending' ORDER BY version", (job_id,))]
        for row in rows:
            conn.execute("UPDATE task_updates SET status='applied',applied_at=? WHERE job_id=? AND version=?", (time.time(), job_id, row["version"]))
            _append(conn, job_id, "task_update", {"version": row["version"], "status": "applied"})
        return rows


def updates_after(job_id, version):
    """Read updates without acknowledging them before the graph checkpoint commits."""
    with transaction() as conn:
        _job(conn, job_id)
        return [dict(row) for row in conn.execute(
            "SELECT * FROM task_updates WHERE job_id=? AND version>? AND status IN ('pending','applied') ORDER BY version",
            (job_id, int(version)))]


def acknowledge_updates(job_id, version):
    """Idempotent outbox acknowledgement after durable graph state includes updates."""
    with transaction() as conn:
        _job(conn, job_id)
        rows = list(conn.execute("SELECT version FROM task_updates WHERE job_id=? AND version<=? AND status='pending' ORDER BY version",
                                 (job_id, int(version))))
        for row in rows:
            conn.execute("UPDATE task_updates SET status='applied',applied_at=? WHERE job_id=? AND version=?",
                         (time.time(), job_id, row["version"]))
            _append(conn, job_id, "task_update", {"version": row["version"], "status": "applied"})


def create_approval(job_id, tool_name, args):
    approval_id = uuid.uuid4().hex
    with transaction() as conn:
        job = _job(conn, job_id)
        if job["status"] not in {"running", "detached"}:
            raise TaskConflict("task_not_running")
        conn.execute("INSERT INTO task_approvals VALUES (?,?,?,?,'pending',?,NULL)", (approval_id, job_id, tool_name, input_hash(args), time.time()))
    return approval_id


def decide_approval(job_id, approval_id, decision):
    if decision not in {"granted", "rejected", "expired", "cancelled"}:
        raise ValueError("invalid approval decision")
    with transaction() as conn:
        job = _job(conn, job_id)
        row = conn.execute("SELECT * FROM task_approvals WHERE approval_id=? AND job_id=?", (approval_id, job_id)).fetchone()
        if row is None:
            raise TaskConflict("approval_not_found")
        if row["status"] != "pending":
            if row["status"] != decision:
                raise TaskConflict("approval_already_decided")
            return dict(row)
        if decision == "granted" and job["status"] not in {"running", "detached"}:
            raise TaskConflict("task_not_running")
        conn.execute("UPDATE task_approvals SET status=?,decided_at=? WHERE approval_id=?", (decision, time.time(), approval_id))
        _append(conn, job_id, "permission_decision", {"approval_id": approval_id, "tool_name": row["tool_name"], "status": decision})
        return dict(row) | {"status": decision}


def approval_status(approval_id):
    with transaction() as conn:
        row = conn.execute("SELECT status FROM task_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return row[0] if row else "missing"


def start_operation(job_id, owner, tool_name, args, *, recovery_mode="never", input_refs=None,
                    lease_seconds=OPERATION_LEASE_SECONDS, tool_call_id="", attempt=1, retry_of=None, logical_key=""):
    refs = input_refs or []
    operation_id, lease = uuid.uuid4().hex, uuid.uuid4().hex
    digest = input_hash({"tool": tool_name, "args": args, "refs": refs})
    with transaction() as conn:
        job = _job(conn, job_id)
        if type(owner) is not int or owner <= 0 or job["owner_user_id"] != owner or job["status"] not in {"running", "detached"}:
            raise TaskConflict("operation_owner_or_state_conflict")
        if retry_of is not None:
            parent = conn.execute(
                "SELECT result FROM task_operations WHERE operation_id=? AND job_id=? "
                "AND owner_user_id=? AND tool_name=? AND status='failed' AND recovery_mode='never'",
                (retry_of, job_id, owner, tool_name)).fetchone()
            if (tool_name != 'browse_web' or not parent or not parent['result']
                    or conn.execute("SELECT 1 FROM task_operations WHERE retry_of=?", (retry_of,)).fetchone()):
                raise TaskConflict('browse_retry_rejected')
        if logical_key and (not tool_call_id or conn.execute(
                "SELECT 1 FROM task_operations WHERE job_id=? AND tool_call_id=? AND logical_key <> ''",
                (job_id, tool_call_id)).fetchone()):
            raise TaskConflict("recovery_requires_reconciliation")
        conn.execute("INSERT INTO task_operations (operation_id,job_id,owner_user_id,tool_name,input_hash,"
                     "inputs,input_refs,recovery_mode,status,lease,lease_until,attempts,updated_at,"
                     "retry_of,tool_call_id,logical_key,dispatch_attempt) "
                     "VALUES (?,?,?,?,?,?,?,?,'running',?,?,1,?,?,?,?,?)",
                     (operation_id, job_id, owner, tool_name, digest, canonical(args), canonical(refs),
                      recovery_mode, lease, time.time()+lease_seconds, time.time(), retry_of,
                      tool_call_id, logical_key, attempt))
        _append(conn, job_id, "operation", {"operation_id": operation_id, "tool_name": tool_name, "status": "running", "input_hash": digest, "tool_call_id": tool_call_id, "attempt": attempt})
    return operation_id, lease


def result_details(content, artifact):
    """Carry adapter-owned structured facts; never derive facts from prose."""
    sources = [artifact] if isinstance(artifact, dict) else []
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            sources.append(payload)
    details = {}
    for source in sources:
        explicit = source.get("_tool_outcome")
        for data in (source, explicit if isinstance(explicit, dict) else {}):
            if isinstance(data.get("facts"), (dict, list)):
                details["facts"] = data["facts"]
            if isinstance(data.get("result_refs"), list):
                details["result_refs"] = data["result_refs"]
    return details


def finish_operation(operation_id, lease, message, *, attempt=1):
    from src.tools.outcome import safe_tool_step
    content = str(message.content or "")
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    outcome = safe_tool_step(message.name or "", content, artifact)
    outcome.update(result_details(content, artifact))
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or row["lease"] != lease or row["status"] != "running" or row["lease_until"] < time.time():
            raise TaskConflict("operation_lease_lost")
        if row["tool_call_id"] and row["tool_call_id"] != message.tool_call_id:
            raise TaskConflict("operation_call_identity_conflict")
        job = _job(conn, row["job_id"])
        if job["owner_user_id"] != row["owner_user_id"] or row["owner_user_id"] <= 0:
            raise TaskConflict("operation_owner_or_state_conflict")
        # Resume commits are independent of the original interrupted terminal view.
        resuming = (job["status"] == "failed" and job["current_step"] == "interrupted"
                    and row["attempts"] > 1 and row["recovery_mode"] != "never")
        if job["status"] not in {"running", "detached"} and not resuming:
            raise TaskConflict("operation_cancelled_or_terminal")
        status = str(outcome.get("status") or "failed")
        from src.agent.batch_journal import observation_json
        try:
            if message.artifact is not None and not isinstance(message.artifact, dict):
                raise TypeError("unsupported_tool_artifact")
            result = observation_json({"content": content, "artifact": artifact})
            outcome = json.loads(observation_json(outcome))
        except (ValueError, TypeError, RecursionError):
            outcome = {"status": status, "reason_code": "batch_observation_not_stored"}
            for key in ("result_refs", "artifacts"):
                refs = result_details(content, artifact).get(key) or artifact.get(key)
                if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
                    outcome[key] = refs
            result = canonical({"content": content, "artifact": {}, "observation_status": "encoding_rejected"})
        conn.execute("UPDATE task_operations SET status=?,result=?,result_refs=?,outcome=?,lease_until=0,updated_at=? WHERE operation_id=?", (status, result, canonical(outcome.get("result_refs") or outcome.get("artifacts") or []), canonical(outcome), time.time(), operation_id))
        _append(conn, row["job_id"], "operation", {"operation_id": operation_id, "tool_name": row["tool_name"], "status": status, "outcome": outcome, "tool_call_id": message.tool_call_id, "attempt": attempt})
    return outcome


def retain_operation_observation(identity, operation_id, tool_call_id, logical_key, attempt, encoded):
    with transaction() as conn:
        job = _job(conn, identity.run_id)
        if (job["owner_user_id"] != identity.owner_user_id or job["session_id"] != identity.session_id
                or job["request_id"] != identity.user_turn_id):
            raise TaskConflict("operation_call_identity_conflict")
        changed = conn.execute(
            "UPDATE task_operations SET host_observation=? WHERE operation_id=? AND job_id=? "
            "AND owner_user_id=? AND tool_call_id=? AND logical_key=? AND dispatch_attempt=? "
            "AND result <> '' AND status NOT IN ('running','interrupted','unknown') "
            "AND (host_observation='' OR host_observation=?)",
            (encoded, operation_id, identity.run_id, identity.owner_user_id,
             tool_call_id, logical_key, attempt, encoded)).rowcount
        if changed != 1:
            raise TaskConflict("recovery_requires_reconciliation")


def abandon_operation(operation_id, lease):
    with transaction() as conn:
        conn.execute("UPDATE task_operations SET status='interrupted',lease_until=0,updated_at=? WHERE operation_id=? AND lease=? AND status='running'", (time.time(), operation_id, lease))


def operation_result(job_id, operation_id, *, include_retry=False, conn=None):
    with read_transaction(conn) as conn:
        job = _job(conn, job_id)
        row = conn.execute("SELECT result,outcome,input_hash,result_refs,retry_of FROM task_operations "
                           "WHERE job_id=? AND operation_id=? AND owner_user_id=? AND owner_user_id>0",
                           (job_id, operation_id, job["owner_user_id"])).fetchone()
        if row is None or not row["result"]:
            raise TaskConflict("operation_result_unavailable")
        return {"job_id": job_id, "operation_id": operation_id,
                "input_hash": row["input_hash"], "result": json.loads(row["result"]),
                "outcome": json.loads(row["outcome"]), "result_refs": json.loads(row["result_refs"]),
                **({"retry_of": row["retry_of"]} if include_retry else {})}


def claim_resume(job_id, operation_id, owner):
    with transaction() as conn:
        job = _job(conn, job_id)
        row = conn.execute("SELECT * FROM task_operations WHERE operation_id=? AND job_id=? AND owner_user_id=?", (operation_id, job_id, owner)).fetchone()
        if (type(owner) is not int or owner <= 0 or row is None or job["owner_user_id"] != owner
                or job["status"] != "failed" or job["current_step"] != "interrupted"
                or row["recovery_mode"] not in SAFE_RECOVERY_MODES or row["status"] != "interrupted"
                or not row["tool_call_id"] or row["lease_until"] > time.time()):
            raise TaskConflict("unsafe_or_unavailable_resume")
        lease, now = uuid.uuid4().hex, time.time()
        lease_until = now + OPERATION_LEASE_SECONDS
        attempt = row["attempts"] + 1
        conn.execute("UPDATE task_operations SET status='running',lease=?,lease_until=?,attempts=?,updated_at=? WHERE operation_id=?",
                     (lease, lease_until, attempt, now, operation_id))
        _append(conn, job_id, "operation", {"operation_id": operation_id, "tool_name": row["tool_name"],
            "status": "resuming", "recovery": "safe_step", "tool_call_id": row["tool_call_id"], "attempt": attempt})
        return dict(row) | {"lease": lease, "lease_until": lease_until, "attempts": attempt,
                           "inputs": json.loads(row["inputs"]), "input_refs": json.loads(row["input_refs"])}


def recover_interrupted(conn):
    """Called inside jobs' startup reconciliation transaction."""
    for job_id, approval_id, tool_name in conn.execute("SELECT job_id,approval_id,tool_name FROM task_approvals WHERE status='pending'").fetchall():
        _append(conn, job_id, "permission_decision", {"approval_id": approval_id, "tool_name": tool_name, "status": "expired"})
    has_segments = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_execution_segments'").fetchone()
    parked_guard = (" AND job_id NOT IN (SELECT job_id FROM agent_execution_segments WHERE phase='parked')"
                    if has_segments else "")
    for job_id, version in conn.execute("SELECT job_id,version FROM task_updates WHERE status='pending'" + parked_guard).fetchall():
        _append(conn, job_id, "task_update", {"version": version, "status": "not_applied", "reason": "service_restart"})
    conn.execute("UPDATE task_updates SET status='not_applied' WHERE status='pending'" + parked_guard)
    conn.execute("UPDATE task_operations SET status='interrupted',lease_until=0 WHERE status='running'")
    conn.execute("UPDATE task_approvals SET status='expired',decided_at=? WHERE status='pending'", (time.time(),))
    # A process died while provider usage was unknown. Keep every reserved call
    # and token charged; these are closed unknown attempts, not in-flight work.
    for row in conn.execute("SELECT attempt_id,job_id,purpose,reserved_tokens FROM task_model_attempts WHERE status='running'").fetchall():
        attempt_id, job_id, purpose, reserved_tokens = row
        conn.execute("UPDATE task_model_attempts SET status='unknown',error='service_restart' WHERE attempt_id=? AND status='running'", (attempt_id,))
        _append(conn, job_id, "model_attempt", {"attempt_id": attempt_id, "purpose": purpose,
                "status": "unknown", "usage_status": "unknown_reserved",
                "charged_tokens": reserved_tokens, "error": "service_restart"})
    from src.core.turn_envelope import build_turn_envelope
    for (job_id,) in conn.execute("SELECT job_id FROM task_jobs WHERE current_step='interrupted' AND status='failed' AND error='service_restart'").fetchall():
        if current_terminal_event(conn, job_id) is not None:
            continue
        envelope = build_turn_envelope(requested_status="failed", display_status="failed",
            job_status="failed", stop_reason="service_restart", history_saved=False,
            notices=["服务进程在任务完成前停止；可恢复已验证的安全步骤。"])
        _append(conn, job_id, "done", {"job_id": job_id, "turn_envelope": envelope.as_dict(),
                                       "recovery": "interrupted_terminal"})


class DurableEventQueue(asyncio.Queue):
    """Persist before notification; bounded queues never hold execution hostage."""
    def __init__(self, job_id, maxsize=LIVE_QUEUE_SIZE):
        super().__init__(maxsize=maxsize)
        self.job_id = job_id
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

    def put_nowait(self, item):
        if item is not None and "id" not in item:
            data = item.get("data") or {}
            if isinstance(data, str):
                data = json.loads(data)
            record = append_event(self.job_id, str(item.get("event") or "message"), data)
            if record is None:
                return
            from src.core.progress_bus import _encode_event
            item = _encode_event(record["event"], record["data"]) | {"id": str(record["seq"])}
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._event_loop is not None and current_loop is not self._event_loop:
            if not self._event_loop.is_closed():
                self._event_loop.call_soon_threadsafe(self._notify, item)
            return
        self._notify(item)

    def _notify(self, item):
        if self.full():
            self.get_nowait()
        super().put_nowait(item)

    async def put(self, item):
        self.put_nowait(item)


def model_input_limit(output_tokens):
    """Configured request input limit, shared by preflight and final admission."""
    from src.core.request_context import current_main_model_budget
    budget = current_main_model_budget()
    if budget is not None:
        return budget.input_limit(output_tokens)
    from src.config import settings
    return max(0, min(int(settings.CONTEXT_ACTIVE_MAX_INPUT_TOKENS),
        int(settings.RESOLVED_CONTEXT_BUDGET_MAX_TOKENS) - int(output_tokens)
        - int(settings.CONTEXT_SAFETY_MARGIN_TOKENS)))


_FINISH_REASONS = frozenset({"stop", "end_turn", "stop_sequence", "tool_calls", "function_call", "tool_use",
    "length", "max_tokens", "max_output_tokens", "content_filter", "error", "unknown", "aborted",
    "refusal", "pause_turn", "incomplete"})


def _attempt_provider(model):
    # Use only configured provider identifiers, not arbitrary endpoints/headers.
    from src.config import settings
    from src.core.provider_protocol import uses_deepseek_protocol
    primary = getattr(model, "primary", model)
    client = getattr(primary, "bound", primary)
    url = str(getattr(client, "openai_api_base", None) or "").rstrip("/")
    provider = settings.RESOLVED_PROVIDER_ID
    if (url and url == settings.RESOLVED_MAIN_BASE_URL.rstrip("/") and isinstance(provider, str)
            and 0 < len(provider) <= 40 and provider.replace("_", "").replace("-", "").isalnum()):
        return provider
    if uses_deepseek_protocol(getattr(client, "model_name", ""), url):
        return "deepseek_compatible"
    return "openai_compatible" if url else "unknown"


def _attempt_response(scope, response, error):
    from langchain_core.messages import AIMessage, AIMessageChunk
    meta = response.response_metadata if isinstance(response, AIMessage) else {}
    finish = meta.get("finish_reason")
    known = isinstance(finish, str) and finish in _FINISH_REASONS
    streaming = isinstance(response, AIMessageChunk) or scope.get("streaming", False)
    if error:
        terminal = "cancelled" if error == "CancelledError" else "failed"
    elif response is None:
        terminal = "no_response"
    else:
        terminal = "terminal_observed" if known else "terminal_unavailable"
    if isinstance(response, AIMessage) and response.invalid_tool_calls:
        response_type = "invalid_tool_calls"
    elif isinstance(response, AIMessage) and response.tool_calls:
        response_type = "tool_calls"
    elif isinstance(response, AIMessage):
        response_type = "answer" if response.content else "empty"
    else:
        response_type = "none" if response is None else "other"
    estimate = scope["estimate"]
    return {"provider_first_packet_ms": scope.get("provider_first_packet_ms"),
            "attempt_elapsed_ms": scope.get("attempt_elapsed_ms"),
            "provider": scope["provider"], "estimated_input_tokens": estimate["estimated_input_tokens"],
            "max_output_tokens": scope.get("actual_output_tokens"),
            "prepared_output_tokens": estimate["output_tokens"], "model_window": estimate.get("model_window"),
            "input_limit": model_input_limit(estimate["output_tokens"]),
            "output_limit_status": meta.get("citrus_output_limit_status"), "streaming": streaming,
            "stream_state": terminal if streaming else "not_streaming",
            "finish_reason": finish if known else None,
            "finish_reason_status": "reported" if known else "missing" if finish is None else "unrecognized",
            "response_type": response_type}


async def run_model_attempt(model, messages, invoke, *, purpose="supervisor", output_tokens=None, usage_session_id=None,
                            streaming=False):
    from src.core.tracing import get_job_id, get_session_id
    from src.core.cache_metrics import extract_usage, emit_usage_from_response
    from src.core.request_context import estimate_request, model_request, _observation, _epoch, _logical_call
    from src.core.context_snapshot import build_context_snapshot, emit_context_snapshot
    phase = _epoch.get()
    parent = _observation.get()
    logical_call_id = _logical_call.get() or (parent or {}).get("logical_call_id") or uuid.uuid4().hex
    payload, estimate = model_request(model, messages, output_tokens=output_tokens)
    limit = model_input_limit(estimate["output_tokens"])
    scope = {"purpose": purpose, "attempt_id": "", "logical_call_id": logical_call_id,
             "epoch": phase.number if phase else 0,
             "change_reason": phase.reason if phase else "initial",
             "history_load_status": phase.history_load_status if phase else "unknown",
             "history_load_reason": phase.history_load_reason if phase else "",
             "estimate": estimate, "model": str(payload.get("model", "")),
             "provider": _attempt_provider(model), "streaming": streaming,
             "parent_purpose": parent.get("purpose", "") if parent else ""}

    def publish_context(*, usage=None, status="running", error=""):
        snapshot = build_context_snapshot(scope["estimate"],
            logical_call_id=logical_call_id, attempt_id=scope["attempt_id"], purpose=purpose,
            model=scope["model"], epoch=scope["epoch"], change_reason=scope["change_reason"],
            history_load_status=scope["history_load_status"],
            history_load_reason=scope["history_load_reason"], input_limit=limit,
            usage=usage, status=status, error=error)
        snapshot["parent_purpose"] = scope["parent_purpose"]
        for key in ("actual_output_tokens", "attempt_elapsed_ms", "task_budget"):
            if scope.get(key) is not None:
                snapshot[key] = scope[key]
        scope["context_snapshot"] = snapshot
        emit_context_snapshot(snapshot)

    scope["publish_context"] = publish_context
    try:
        if estimate["estimated_input_tokens"] > limit:
            reason = "context_budget_exhausted"
            if purpose == "main_loop" and estimate["tool_message_count"]:
                # Attribute the overage using the converted request. This
                # hypothetical payload is never sent or used for admission.
                without_observations = {**payload, "messages": [
                    msg for msg in payload["messages"] if msg.get("role") not in {"tool", "function"}]}
                if estimate_request(without_observations)["estimated_input_tokens"] <= limit:
                    reason = "context_budget_exhausted:tool_observations"
            raise TaskConflict(reason)
        attempt_id = reserve_attempt(get_job_id(), purpose, estimate["estimated_total_tokens"])
    except TaskConflict as exc:
        publish_context(status="not_admitted", error=str(exc))
        raise
    scope["attempt_id"] = attempt_id or uuid.uuid4().hex
    scope["reservation_id"] = attempt_id
    publish_context()
    observation_token = _observation.set(scope)
    response, error = None, ""
    invoked_at = time.monotonic()
    try:
        remaining = remaining_seconds(get_job_id())
        response = (await invoke() if remaining == float("inf") else
                    await asyncio.wait_for(invoke(), timeout=remaining))
        return response
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        from src.core.public_delivery import duration_ms
        scope["attempt_elapsed_ms"] = duration_ms((time.monotonic() - invoked_at) * 1000)
        usage = extract_usage(response) or {}
        denied = scope.get("pre_transport_denial", "")
        error = denied or error
        counters = finish_attempt(attempt_id, usage, error=error, observation=_attempt_response(
            scope, response, error), unsent=bool(denied))
        if isinstance(counters, dict):
            scope["task_budget"] = counters
        if response is not None:
            emit_usage_from_response(usage_session_id or get_session_id(), purpose, response,
                attempt_id=scope["attempt_id"], logical_call_id=scope["logical_call_id"],
                epoch=scope["epoch"], parent_purpose=scope["parent_purpose"])
        if not scope.get("context_settled"):
            status = ("not_admitted" if denied else "cancelled" if error == "CancelledError"
                      else "failed" if error else "complete")
            publish_context(usage=usage, status=status, error=error)
            scope["context_settled"] = True
        _observation.reset(observation_token)
        snapshot = scope.get("snapshot")
        if snapshot:
            snapshot.update(estimate=scope["estimate"], parent_purpose=scope["parent_purpose"],
                            context_snapshot=scope.get("context_snapshot"))
            try:
                from src.core import turn_context
                from src.core.progress_bus import get_turn_scope
                from src.core.account_leases import current_account_lease, run_account_thread
                sid, rid = get_turn_scope()
                sid = sid or usage_session_id or get_session_id()
                if sid:
                    lease = current_account_lease()
                    write = (run_account_thread(lease, turn_context.record, snapshot,
                                session_id=sid, request_id=rid, usage=usage, error=error)
                             if lease is not None else asyncio.to_thread(turn_context.record, snapshot,
                                session_id=sid, request_id=rid, usage=usage, error=error))
                    await asyncio.wait_for(write, timeout=1.0)
            except Exception:
                pass


def init_budget(job_id, deadline, max_calls=64, max_tokens=None):
    if max_tokens is None:
        from src.config import settings
        max_tokens = settings.AGENT_MAX_MODEL_TOKENS
    with transaction() as conn:
        _job(conn, job_id)
        conn.execute("INSERT OR IGNORE INTO task_budgets(job_id,deadline,max_calls,max_tokens) VALUES (?,?,?,?)", (job_id, deadline, max_calls, max_tokens))


def reserve_attempt(job_id, purpose, estimated_tokens):
    """Reserve every attempt, including failures with unknown provider usage."""
    if not job_id:
        return ""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_budgets WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return ""
        tokens = max(1, int(estimated_tokens))
        # Actual exhaustion takes precedence over protected headroom.
        if row["deadline"] <= time.time():
            raise TaskConflict("model_budget_exhausted:deadline")
        if row["calls"] >= row["max_calls"]:
            raise TaskConflict(f"model_budget_exhausted:calls ({row['calls']}/{row['max_calls']})")
        if row["tokens"] + tokens > row["max_tokens"]:
            raise TaskConflict(f"model_budget_exhausted:tokens ({row['tokens']}+{tokens}>{row['max_tokens']})")
        if not str(purpose).startswith("final_synthesis"):
            from src.core.request_context import _observation
            parent = _observation.get() or {}
            is_final_fallback = str(parent.get("purpose", "")).startswith("final_synthesis")
            if not is_final_fallback and (row["calls"] + 1 + row["final_calls"] > row["max_calls"]
                    or row["tokens"] + tokens + row["final_tokens"] > row["max_tokens"]):
                if row["final_calls"] or row["final_tokens"]:
                    raise TaskConflict("model_budget_reserved:finalization")
        attempt_id = uuid.uuid4().hex
        conn.execute("UPDATE task_budgets SET calls=calls+1,tokens=tokens+? WHERE job_id=?", (tokens, job_id))
        conn.execute("INSERT INTO task_model_attempts(attempt_id,job_id,purpose,status,reserved_tokens,created_at) VALUES (?,?,?,'running',?,?)", (attempt_id, job_id, purpose, tokens, time.time()))
        _append(conn, job_id, "model_attempt", {"attempt_id": attempt_id, "purpose": purpose,
                "status": "running", "reserved_tokens": tokens})
        return attempt_id


def adjust_model_reservation(attempt_id, tokens):
    """Revalidate an increase between preflight and the actual outbound body."""
    if not attempt_id:
        return
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_model_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["status"] != "running":
            raise TaskConflict("model_attempt_not_active")
        extra = tokens - row["reserved_tokens"]
        if extra <= 0:
            return
        if row["budget_scope"] != "execution":
            raise TaskConflict("model_request_changed_after_admission")
        budget = conn.execute("SELECT * FROM task_budgets WHERE job_id=?", (row["job_id"],)).fetchone()
        if not budget or budget["deadline"] <= time.time():
            raise TaskConflict("model_budget_exhausted:deadline")
        if budget["tokens"] + extra > budget["max_tokens"]:
            raise TaskConflict("model_budget_exhausted:tokens")
        if (not row["purpose"].startswith("final_synthesis")
                and budget["final_tokens"] and budget["tokens"] + extra + budget["final_tokens"] > budget["max_tokens"]):
            raise TaskConflict("model_budget_reserved:finalization")
        conn.execute("UPDATE task_budgets SET tokens=tokens+? WHERE job_id=?", (extra, row["job_id"]))
        conn.execute("UPDATE task_model_attempts SET reserved_tokens=? WHERE attempt_id=?", (tokens, attempt_id))


def _budget_counters(conn, job_id):
    row = conn.execute("SELECT calls,max_calls,tokens,max_tokens FROM task_budgets WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def finish_attempt(attempt_id, usage=None, error="", *, observation=None, unsent=False):
    if not attempt_id:
        return
    usage = usage or {}
    reported = usage.get("total_tokens") is not None or all(
        usage.get(key) is not None for key in ("input_tokens", "output_tokens"))
    measured = max(0, int(usage.get("total_tokens") if usage.get("total_tokens") is not None else
                   int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)))
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_model_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["status"] != "running":
            return
        # Unknown usage retains the reservation instead of silently charging zero.
        if unsent:
            if row["budget_scope"] == "execution":
                conn.execute("UPDATE task_budgets SET calls=calls-1,tokens=tokens-? WHERE job_id=?",
                             (row["reserved_tokens"], row["job_id"]))
            conn.execute("UPDATE task_model_attempts SET status='not_sent',usage='{}',error=?,observation=? WHERE attempt_id=?",
                         (error, canonical(observation or {}), attempt_id))
            _append(conn, row["job_id"], "model_attempt", {"attempt_id": attempt_id,
                "purpose": row["purpose"], "status": "not_sent", "usage_status": "not_sent",
                "charged_tokens": 0, "error": error, "observation": observation or {}})
            return _budget_counters(conn, row["job_id"])
        if reported and row["budget_scope"] == "execution":
            conn.execute("UPDATE task_budgets SET tokens=tokens+? WHERE job_id=?", (measured-row["reserved_tokens"], row["job_id"]))
        observation = observation or {}
        conn.execute("UPDATE task_model_attempts SET status=?,usage=?,error=?,observation=? WHERE attempt_id=?",
                     ("failed" if error else "complete", canonical(usage), error, canonical(observation), attempt_id))
        _append(conn, row["job_id"], "model_attempt", {"attempt_id": attempt_id,
            "purpose": row["purpose"], "status": "failed" if error else "complete",
            "usage": usage, "usage_status": "reported" if reported else "unknown_reserved",
            "charged_tokens": measured if reported else row["reserved_tokens"], "error": error,
            "observation": observation})
        return _budget_counters(conn, row["job_id"])


def _usage_totals(conn, job_id):
    totals = {"reported_input_tokens": 0, "reported_output_tokens": 0,
              "reported_total_tokens": 0, "unknown_reserved_tokens": 0,
              "reported_attempts": 0, "unknown_attempts": 0,
              "in_flight_reserved_tokens": 0, "in_flight_attempts": 0}
    # Physical attempts retain their original job identity across continuations;
    # the logical task's reported usage includes every closed ancestor segment.
    for row in conn.execute("WITH RECURSIVE lineage(job_id) AS (SELECT ? UNION SELECT j.parent_job_id FROM task_jobs j JOIN lineage l ON j.job_id=l.job_id WHERE j.parent_job_id<>'') SELECT usage,reserved_tokens,status FROM task_model_attempts WHERE job_id IN (SELECT job_id FROM lineage)", (job_id,)):
        usage = json.loads(row["usage"] or "{}")
        if row["status"] == "not_sent":
            continue
        if row["status"] == "running":
            totals["in_flight_attempts"] += 1
            totals["in_flight_reserved_tokens"] += int(row["reserved_tokens"])
        elif usage.get("total_tokens") is not None or all(
                usage.get(key) is not None for key in ("input_tokens", "output_tokens")):
            totals["reported_attempts"] += 1
            totals["reported_input_tokens"] += int(usage.get("input_tokens") or 0)
            totals["reported_output_tokens"] += int(usage.get("output_tokens") or 0)
            totals["reported_total_tokens"] += int(usage.get("total_tokens") or
                int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0))
        else:
            totals["unknown_attempts"] += 1
            totals["unknown_reserved_tokens"] += int(row["reserved_tokens"])
    return totals


def release_finalization(job_id):
    if job_id:
        with transaction() as conn:
            conn.execute("UPDATE task_budgets SET final_tokens=0,final_calls=0 WHERE job_id=?", (job_id,))


def remaining_seconds(job_id):
    if not job_id:
        return float("inf")
    with transaction() as conn:
        row = conn.execute("SELECT deadline FROM task_budgets WHERE job_id=?", (job_id,)).fetchone()
        return max(0.0, row[0] - time.time()) if row else float("inf")
