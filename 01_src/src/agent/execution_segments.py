"""Durable admission for Host/API execution-wait segments.

Same job, turn, original budget and account epoch. A wake cannot claim a run
until the previous segment explicitly releases its OUTER cleanup barrier. These
primitives do not dispatch, poll, resume a model, or replace the OS run lock.
The API owns cleanup; the opt-in offline coordinator owns completion discovery.
Crashed in-flight phases require explicit reconciliation, never a TTL takeover.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import uuid

from src.core import jobs, task_events
from src.core.account_leases import acquire_account_lease
from .identity import ExecutionIdentity
from .runtime import validate_execution_waits

_SCHEMA = """CREATE TABLE IF NOT EXISTS agent_execution_segments (
 job_id TEXT PRIMARY KEY REFERENCES task_jobs(job_id),
 account_epoch INTEGER NOT NULL, generation INTEGER NOT NULL,
 token TEXT NOT NULL, phase TEXT NOT NULL,
 checkpoint_ref TEXT NOT NULL DEFAULT '', wait_ids TEXT NOT NULL DEFAULT '[]')"""


@dataclass(frozen=True)
class ExecutionSegmentClaim:
    identity: ExecutionIdentity
    generation: int
    token: str


class ExecutionSegments:
    def __init__(self, identity):
        if type(identity) is not ExecutionIdentity:
            raise TypeError('execution_segment_identity_required')
        self.identity = identity

    def _job(self, conn, *, active=True):
        job = task_events._job(conn,self.identity.run_id)
        if (job['owner_user_id'] != self.identity.owner_user_id
                or job['session_id'] != self.identity.session_id
                or job['request_id'] != self.identity.user_turn_id):
            raise task_events.TaskConflict('execution_segment_identity_conflict')
        if active and (job['status'] not in {'running','detached'} or job['cancel_requested_at']
                       or task_events.current_terminal_event(conn,self.identity.run_id) is not None):
            raise task_events.TaskConflict('execution_segment_not_active')
        return job

    @staticmethod
    def _settled(conn, run_id):
        jobs.require_settled_operations(conn, run_id, reason='execution_segment_requires_reconciliation')

    def _row(self, conn, *, active=True):
        conn.execute(_SCHEMA)
        self._job(conn, active=active)
        row = conn.execute('SELECT * FROM agent_execution_segments WHERE job_id=?',(self.identity.run_id,)).fetchone()
        if row is None:
            raise task_events.TaskConflict('execution_segment_missing')
        return row

    @staticmethod
    def _reference(checkpoint_ref):
        from src.code_execution.protocol import token
        token(checkpoint_ref)

    def _claim(self, row):
        return ExecutionSegmentClaim(self.identity,row['generation'],row['token'])

    def _owned(self, row, claim):
        if (type(claim) is not ExecutionSegmentClaim or claim.identity != self.identity
                or claim.generation != row['generation'] or claim.token != row['token']):
            raise task_events.TaskConflict('execution_segment_claim_lost')

    @contextmanager
    def _admit(self, *, active=True):
        with task_events.transaction() as conn:
            epoch = self._row(conn, active=active)['account_epoch']
        lease = acquire_account_lease(self.identity.owner_user_id,expected_epoch=epoch)
        try:
            yield
        finally:
            lease.release()

    def _step(self, conn, step, phase):
        conn.execute('UPDATE task_jobs SET current_step=?,updated_at=? WHERE job_id=?',
                     (step,datetime.now().isoformat(),self.identity.run_id))
        task_events._append(conn,self.identity.run_id,'execution_segment',{
            'job_id':self.identity.run_id,'phase':phase})

    @staticmethod
    def candidates(*, after="", limit=32, restart_only=False):
        """Bounded host-only discovery; never expose claim tokens to clients."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('execution_segment_page_invalid')
        with task_events.transaction() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_execution_segments'").fetchone() is None:
                return []
            phase = " AND s.phase='restart_wait'" if restart_only else ""
            return [dict(row) for row in conn.execute(
                "SELECT j.job_id,j.owner_user_id,j.session_id,j.request_id,j.status,s.phase "
                "FROM agent_execution_segments s JOIN task_jobs j ON j.job_id=s.job_id "
                "WHERE j.status IN ('running','detached','cancel_requested') AND j.job_id>? "
                + phase + " ORDER BY j.job_id LIMIT ?", (after, limit))]

    def snapshot(self):
        """Trusted lifecycle observation, including a pending cancellation."""
        with task_events.transaction() as conn:
            self._job(conn, active=False)
            row = conn.execute('SELECT * FROM agent_execution_segments WHERE job_id=?',
                               (self.identity.run_id,)).fetchone()
            if row is None:
                raise task_events.TaskConflict('execution_segment_missing')
            return dict(row)

    def claim_question_resume(self, *, account_epoch, deadline, expected, fence=None):
        """Reopen the same job, budget and queue head after an accepted answer."""
        import time
        from src.core import chat_turn_queue, user_questions

        if not deadline > time.time():
            raise ValueError("question_execution_deadline_required")
        with task_events.transaction() as conn:
            job = self._job(conn, active=False)
            segment = self._row(conn, active=False)
            terminal = task_events.current_terminal_event(conn, self.identity.run_id)
            question = user_questions.resume_question_row(conn, self.identity.run_id)
            value = expected["value"]
            if (job["status"] != "awaiting_input" or job["cancel_requested_at"] or terminal is None
                    or segment["phase"] != "terminal" or segment["account_epoch"] != account_epoch
                    or question is None or question["expires_at"] <= time.time()
                    or (question["id"], question["version"], question["interrupt_id"], question["checkpoint_ref"])
                       != (value["question_id"], value["version"], value["interrupt_id"], expected["checkpoint_ref"])):
                raise task_events.TaskConflict("question_resume_claim_conflict")
            self._settled(conn, self.identity.run_id)
            chat_turn_queue._resume_question(conn, job, account_epoch=account_epoch, fence=fence)
            run_id = self.identity.run_id
            if conn.execute("UPDATE task_budgets SET deadline=? WHERE job_id=?", (deadline, run_id)).rowcount != 1:
                raise task_events.TaskConflict("execution_segment_budget_missing")
            event = task_events._append(conn, run_id, "agent_recovery", {"job_id": run_id,
                "request_id": self.identity.user_turn_id, "status": "admitted", "previous_terminal_seq": terminal[0]})
            conn.execute("UPDATE task_questions SET resume_cursor=? WHERE id=?", (event["seq"], question["id"]))
            conn.execute("UPDATE task_jobs SET status='running',current_step='resuming_user_input',error='',"
                         "finished_at='',terminal_snapshot='',updated_at=? WHERE job_id=?",
                         (datetime.now().isoformat(), run_id))
            conn.execute("UPDATE agent_execution_segments SET phase='resuming',generation=generation+1,"
                         "token=?,checkpoint_ref=?,wait_ids='[]' WHERE job_id=?",
                         (uuid.uuid4().hex, expected["checkpoint_ref"], run_id))
            return {"claim": self._claim(self._row(conn)), "recovery_cursor": event["seq"]}

    def _repark_question(self, conn, row):
        from src.core import chat_turn_queue, user_questions

        run_id = self.identity.run_id
        question = user_questions.resume_question_row(conn, run_id, active=True)
        if question is None or row["checkpoint_ref"] != question["checkpoint_ref"] or row["wait_ids"] != "[]":
            return False
        cursor = task_events.recovery_cursor(conn, run_id)
        previous = conn.execute("SELECT data FROM task_events WHERE job_id=? AND event='done' AND seq<? "
                                "ORDER BY seq DESC LIMIT 1", (run_id, cursor)).fetchone()
        if previous is None or task_events.current_terminal_event(conn, run_id) is not None:
            raise task_events.TaskConflict("question_resume_previous_terminal_missing")
        payload = json.loads(previous[0])
        if (payload.get("turn_envelope") or {}).get("job_status") != "awaiting_input":
            raise task_events.TaskConflict("question_resume_previous_terminal_mismatch")
        self._settled(conn, run_id)
        conn.execute("UPDATE task_questions SET recovery_cursor=?,resume_cursor=0 WHERE id=?", (cursor, question["id"]))
        question = user_questions._view(conn.execute("SELECT * FROM task_questions WHERE id=?", (question["id"],)).fetchone())
        payload.update(question=question, pending_question=question)
        conn.execute("UPDATE task_jobs SET status='awaiting_input',current_step='awaiting_input',finished_at=?,updated_at=? "
                     "WHERE job_id=?", (datetime.now().isoformat(), datetime.now().isoformat(), run_id))
        conn.execute("UPDATE agent_execution_segments SET phase='terminal',generation=generation+1,token=? WHERE job_id=?",
                     (uuid.uuid4().hex, run_id))
        chat_turn_queue._repark_question(conn, run_id)
        task_events._append(conn, run_id, "done", payload)
        return True

    def repark_question(self, claim):
        """Requeue only a proven question wake before any model may run."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            self._owned(row, claim)
            if row["phase"] not in {"resuming", "active"} or not self._repark_question(conn, row):
                raise task_events.TaskConflict("question_resume_repark_conflict")

    def release_unstarted_resume(self, claim):
        """Return an unactivated admission; invalidate its token before reuse."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            self._owned(row, claim)
            if row['phase'] == 'recovering':
                from src.core import chat_turn_queue
                job_id = self.identity.run_id
                conn.execute("UPDATE agent_execution_segments SET phase='interrupted',generation=generation+1,token=? WHERE job_id=?",
                             (uuid.uuid4().hex, job_id))
                jobs._mark_interrupted(conn, job_id, datetime.now().isoformat())
                chat_turn_queue._freeze_unstarted_recovery(conn, job_id)
                from src.core.turn_envelope import build_turn_envelope
                envelope = build_turn_envelope(requested_status="failed", display_status="failed",
                    job_status="failed", stop_reason="service_restart", history_saved=False)
                task_events._append(conn, job_id, "done", {"job_id": job_id,
                    "turn_envelope": envelope.as_dict(), "recovery": "interrupted_terminal"})
                return 'interrupted'
            if row['phase'] != 'resuming':
                raise task_events.TaskConflict('execution_segment_phase_conflict')
            if self._repark_question(conn, row):
                return 'awaiting_input'
            self._settled(conn, self.identity.run_id)
            conn.execute("UPDATE agent_execution_segments SET phase='parked',generation=generation+1,token=? WHERE job_id=?",
                         (uuid.uuid4().hex, self.identity.run_id))
            self._step(conn, 'waiting_code', 'parked')
            return 'parked'

    def classify_restart_wait(self, snapshot, *, execution_wait=None, question_resume=False, question_pause=None):
        """Fenced startup classification after reading the old protected graph."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            if (row['phase'] != 'restart_wait' or row['generation'] != snapshot['generation']
                    or row['token'] != snapshot['token']):
                raise task_events.TaskConflict('execution_segment_claim_lost')
            if question_resume:
                if not self._repark_question(conn, row):
                    raise task_events.TaskConflict("question_resume_repark_conflict")
                return
            if question_pause is not None:
                self._adopt_question_pause(conn, row, question_pause)
                return
            if execution_wait:
                checkpoint_ref, waits = execution_wait["checkpoint_ref"], tuple(execution_wait["wait_ids"])
                self._reference(checkpoint_ref)
                validate_execution_waits(waits)
                if not waits:
                    raise task_events.TaskConflict('execution_wait_references_required')
                self._settled(conn, self.identity.run_id)
                conn.execute("UPDATE agent_execution_segments SET checkpoint_ref=?,wait_ids=? WHERE job_id=?",
                             (checkpoint_ref, json.dumps(waits, separators=(',', ':')), self.identity.run_id))
                self._step(conn, 'waiting_code', 'parked')
            else:
                jobs._mark_interrupted(conn, self.identity.run_id, datetime.now().isoformat())
                from src.core.turn_envelope import build_turn_envelope
                envelope = build_turn_envelope(requested_status='failed', display_status='failed',
                    job_status='failed', stop_reason='service_restart', history_saved=False)
                task_events._append(conn, self.identity.run_id, 'done', {'job_id': self.identity.run_id,
                    'turn_envelope': envelope.as_dict(), 'recovery': 'interrupted_terminal'})
            conn.execute('UPDATE agent_execution_segments SET phase=?,generation=generation+1,token=? WHERE job_id=?',
                         ('parked' if execution_wait else 'interrupted', uuid.uuid4().hex, self.identity.run_id))

    def _adopt_question_pause(self, conn, row, pause):
        from src.core import user_questions
        from src.core.turn_envelope import build_turn_envelope

        run_id = self.identity.run_id
        self._reference(pause["checkpoint_ref"])
        if row["wait_ids"] != "[]" or task_events.current_terminal_event(conn, run_id) is not None:
            raise task_events.TaskConflict("question_pause_adoption_conflict")
        self._settled(conn, run_id)
        receipt = conn.execute("SELECT 1 FROM task_operations WHERE operation_id=? AND job_id=? AND owner_user_id=? "
            "AND tool_call_id=? AND tool_name='request_user_input' AND status='complete' "
            "AND result<>'' AND host_observation<>''", (pause["operation_id"], run_id,
                self.identity.owner_user_id, pause["tool_call_id"])).fetchone()
        if receipt is None:
            raise task_events.TaskConflict("question_pause_receipt_missing")
        question = user_questions.adopt_question_pause(conn, self.identity, pause)
        envelope = build_turn_envelope(answer=question["prompt"], requested_status="awaiting_input",
            display_status="awaiting_input", job_status="awaiting_input", stop_reason="user_input_required",
            history_saved=False)
        payload = {"job_id": run_id, "session_id": self.identity.session_id, "answer": question["prompt"],
                   "turn_status": envelope.display_status, "turn_envelope": envelope.as_dict(),
                   "history_saved": False, "question": question, "pending_question": question}
        self._write_terminal(conn, self._job(conn), done_payload=payload, status="awaiting_input",
                             current_step="awaiting_input")

    def start(self):
        """Only a new init job may gain its first segment; retries cannot dispatch."""
        lease = acquire_account_lease(self.identity.owner_user_id)
        try:
            with task_events.transaction() as conn:
                conn.execute(_SCHEMA)
                job = self._job(conn)
                if job['current_step'] != 'init':
                    raise task_events.TaskConflict('execution_segment_initial_admission_lost')
                if conn.execute('SELECT 1 FROM task_budgets WHERE job_id=?',(self.identity.run_id,)).fetchone() is None:
                    raise task_events.TaskConflict('execution_segment_budget_missing')
                self._settled(conn,self.identity.run_id)
                changed = conn.execute("INSERT INTO agent_execution_segments(job_id,account_epoch,generation,token,phase) "
                    "VALUES(?,?,1,?,'active') ON CONFLICT(job_id) DO NOTHING",
                    (self.identity.run_id,lease.epoch,uuid.uuid4().hex)).rowcount
                if changed != 1:
                    raise task_events.TaskConflict('execution_segment_initial_admission_lost')
                self._step(conn,'segment_active','active')
                return self._claim(self._row(conn))
        finally:
            lease.release()

    def prepare_park(self, claim, *, checkpoint_ref, wait_ids):
        """After a real paired Harness checkpoint; old outer cleanup still owns run."""
        self._reference(checkpoint_ref)
        validate_execution_waits(wait_ids)
        if not wait_ids:
            raise ValueError('execution_wait_references_required')
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            self._owned(row,claim)
            encoded = json.dumps(wait_ids,separators=(',',':'))
            if row['phase'] == 'parking' and row['checkpoint_ref'] == checkpoint_ref and row['wait_ids'] == encoded:
                return
            if row['phase'] != 'active':
                raise task_events.TaskConflict('execution_segment_phase_conflict')
            self._settled(conn,self.identity.run_id)
            conn.execute("UPDATE agent_execution_segments SET phase='parking',checkpoint_ref=?,wait_ids=? WHERE job_id=?",
                         (checkpoint_ref,encoded,self.identity.run_id))
            self._step(conn,'parking_code','parking')

    def release_park(self, claim):
        """Only AFTER outer API capacity/progress/account cleanup is settled."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            self._owned(row,claim)
            if row['phase'] == 'parked':
                return
            if row['phase'] != 'parking':
                raise task_events.TaskConflict('execution_segment_phase_conflict')
            conn.execute("UPDATE agent_execution_segments SET phase='parked' WHERE job_id=?",(self.identity.run_id,))
            self._step(conn,'waiting_code','parked')

    def release_cancel(self, claim):
        """AFTER the same outer cleanup, retain cancellation for Worker settlement."""
        with task_events.transaction() as conn:
            self._job(conn, active=False)
            row = conn.execute('SELECT * FROM agent_execution_segments WHERE job_id=?',
                               (self.identity.run_id,)).fetchone()
            if row is None:
                raise task_events.TaskConflict('execution_segment_missing')
            self._owned(row, claim)
            job = self._job(conn, active=False)
            if job['status'] != 'cancel_requested':
                raise task_events.TaskConflict('execution_segment_not_cancelling')
            if row['phase'] not in {'active', 'parking', 'resuming', 'recovering', 'finalizing', 'cancel_parked'}:
                raise task_events.TaskConflict('execution_segment_phase_conflict')
            conn.execute("UPDATE agent_execution_segments SET phase='cancel_parked' WHERE job_id=?",
                         (self.identity.run_id,))
            self._step(conn, 'code_cancel_pending', 'cancel_parked')

    def finish_cancelled(self):
        """Trusted caller already proved all run executions terminal under the OS lock."""
        from src.core.turn_envelope import build_turn_envelope
        with task_events.transaction() as conn:
            job = self._job(conn, active=False)
            row = conn.execute('SELECT * FROM agent_execution_segments WHERE job_id=?',
                               (self.identity.run_id,)).fetchone()
            if (job['status'] != 'cancel_requested' or row is None
                    or row['phase'] not in {'parked', 'cancel_parked'}):
                raise task_events.TaskConflict('execution_segment_not_cancelling')
            self._settled(conn, self.identity.run_id)
            if task_events.current_terminal_event(conn, self.identity.run_id) is not None:
                return
            envelope = build_turn_envelope(display_status='cancelled', job_status='cancelled',
                requested_status='cancelled', stop_reason=job['stop_reason'] or 'user',
                history_saved=False, notices=['执行已停止；本轮未生成最终回答。']).as_dict()
            task_events._append(conn, self.identity.run_id, 'done', {
                'job_id': self.identity.run_id, 'session_id': self.identity.session_id,
                'answer': '', 'turn_status': 'cancelled', 'history_saved': False,
                'turn_envelope': envelope})
            now = datetime.now().isoformat()
            conn.execute("UPDATE task_jobs SET status='cancelled',current_step='cancelled',"
                "finished_at=?,updated_at=?,terminal_snapshot=? WHERE job_id=?",
                (now, now, json.dumps(envelope, ensure_ascii=False), self.identity.run_id))
            conn.execute("UPDATE agent_execution_segments SET phase='terminal',token=? WHERE job_id=?",
                         (uuid.uuid4().hex, self.identity.run_id))

    def claim_resume(self):
        """One CAS from fully parked to resuming. No new job/quota/model attempt."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            if row['phase'] != 'parked':
                raise task_events.TaskConflict('execution_segment_not_parked')
            self._settled(conn,self.identity.run_id)
            budget = conn.execute('SELECT * FROM task_budgets WHERE job_id=?',(self.identity.run_id,)).fetchone()
            if budget is None:
                raise task_events.TaskConflict('execution_segment_budget_missing')
            # Exhaustion still permits receipt-only reconciliation. Physical
            # model admission must enforce THIS original budget after resume.
            conn.execute("UPDATE agent_execution_segments SET phase='resuming',generation=generation+1,token=? WHERE job_id=?",
                         (uuid.uuid4().hex,self.identity.run_id))
            self._step(conn,'resuming_code','resuming')
            row = self._row(conn)
            return {'claim':self._claim(row),'checkpoint_ref':row['checkpoint_ref'],
                    'wait_ids':tuple(json.loads(row['wait_ids'])),'budget':dict(budget)}

    def activate(self, claim):
        """After protected checkpoint/identity validation, before model admission."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            self._owned(row,claim)
            if row['phase'] not in {'resuming', 'recovering'}:
                raise task_events.TaskConflict('execution_segment_phase_conflict')
            conn.execute("UPDATE agent_execution_segments SET phase='active' WHERE job_id=?",(self.identity.run_id,))
            self._step(conn,'segment_active','active')

    def begin_finalize(self, claim):
        """Fence outer finalization so a wake cannot overlap history/terminal save."""
        with self._admit(), task_events.transaction() as conn:
            row = self._row(conn)
            self._owned(row,claim)
            if row['phase'] != 'active':
                raise task_events.TaskConflict('execution_segment_phase_conflict')
            conn.execute("UPDATE agent_execution_segments SET phase='finalizing' WHERE job_id=?",(self.identity.run_id,))
            self._step(conn,'finalizing','finalizing')

    def finish_terminal(self, claim, *, done_payload, status, current_step,
                        progress_summary="", error="", stop_reason="", stop_note=""):
        """Close the segment in the same commit as the terminal job view.

        The durable ``done`` fact, the job's terminal status and the segment's
        ``terminal`` phase are one atomic unit under the live claim fence. A
        crash before commit leaves all three untouched, so the same claim can
        retry; a stale generation/token cannot close a newer segment.
        """
        if status not in {"completed", "failed", "cancelled", "awaiting_input"}:
            raise ValueError("execution_segment_terminal_status_invalid")
        if not isinstance(done_payload, dict) or not isinstance(done_payload.get("turn_envelope"), dict):
            raise ValueError("execution_segment_terminal_envelope_required")
        with self._admit(active=False), task_events.transaction() as conn:
            job = self._job(conn, active=False)
            row = conn.execute("SELECT * FROM agent_execution_segments WHERE job_id=?",
                               (self.identity.run_id,)).fetchone()
            if row is None:
                raise task_events.TaskConflict("execution_segment_missing")
            terminal = task_events.current_terminal_event(conn, self.identity.run_id)
            if row["phase"] == "terminal":
                # The terminal commit already closed this exact segment.
                if (type(claim) is not ExecutionSegmentClaim or claim.identity != self.identity
                        or claim.generation != row["generation"]):
                    raise task_events.TaskConflict("execution_segment_claim_lost")
                if terminal is None or job["status"] != status:
                    raise task_events.TaskConflict("execution_segment_terminal_conflict")
                return
            self._owned(row, claim)
            if status == "cancelled":
                phases = {"finalizing", "active", "resuming", "recovering", "parking"}
            elif status == "failed" and row["phase"] != "finalizing":
                # A graph exception can end the turn before finalization. Close
                # that segment only after proving no execution outcome is still
                # running or unknown; otherwise reconciliation keeps the claim.
                # A finalized turn keeps the existing behavior: its envelope is
                # the authoritative report of any unknown operations.
                self._settled(conn, self.identity.run_id)
                phases = {"active", "resuming", "recovering", "parking"}
            else:
                phases = {"finalizing"}
            if row["phase"] not in phases:
                raise task_events.TaskConflict("execution_segment_phase_conflict")
            if job["status"] not in {"running", "detached", "cancel_requested"}:
                raise task_events.TaskConflict("execution_segment_not_active")
            if terminal is not None:
                # Another path already owns the durable terminal fact. Never
                # rewrite it and never close the segment from a later claim.
                raise task_events.TaskConflict("execution_segment_terminal_conflict")
            self._write_terminal(conn, job, done_payload=done_payload, status=status,
                current_step=current_step, progress_summary=progress_summary, error=error,
                stop_reason=stop_reason, stop_note=stop_note)

    def _write_terminal(self, conn, job, *, done_payload, status, current_step,
                        progress_summary="", error="", stop_reason="", stop_note=""):
        # Record the phase fact before the terminal frame (later events are
        # suppressed once a done fact exists), then commit both together.
        task_events._append(conn, self.identity.run_id, "execution_segment",
                            {"job_id": self.identity.run_id, "phase": "terminal"})
        task_events._append(conn, self.identity.run_id, "done", done_payload)
        now = datetime.now().isoformat()
        guard = "job_id=? AND status IN ('running','detached','cancel_requested')"
        if status == "cancelled":
            # Same transition as jobs.mark_stopped: current_step, progress
            # summary and error are intentionally left untouched.
            changed = conn.execute(
                "UPDATE task_jobs SET status='cancelled',stop_reason=?,stop_note=?,"
                "finished_at=?,updated_at=? WHERE " + guard,
                (stop_reason or "user", stop_note, now, now, self.identity.run_id)).rowcount
        elif status == "completed":
            # Same transition as jobs.mark_completed_if_open: error and a
            # prior stop_note survive unless this completion carries one.
            note = stop_note or ("停止请求晚于完成" if job["status"] == "cancel_requested" else "")
            if note:
                changed = conn.execute(
                    "UPDATE task_jobs SET status='completed',current_step=?,"
                    "progress_summary=?,stop_note=?,finished_at=?,updated_at=? WHERE " + guard,
                    (current_step, progress_summary, note, now, now,
                     self.identity.run_id)).rowcount
            else:
                changed = conn.execute(
                    "UPDATE task_jobs SET status='completed',current_step=?,"
                    "progress_summary=?,finished_at=?,updated_at=? WHERE " + guard,
                    (current_step, progress_summary, now, now, self.identity.run_id)).rowcount
        else:
            changed = conn.execute(
                "UPDATE task_jobs SET status=?,current_step=?,progress_summary=?,error=?,stop_reason=?,"
                "finished_at=?,updated_at=? WHERE " + guard,
                (status, current_step, progress_summary, error, stop_reason, now, now,
                 self.identity.run_id)).rowcount
        if changed != 1:
            raise task_events.TaskConflict("execution_segment_terminal_conflict")
        conn.execute("UPDATE agent_execution_segments SET phase='terminal',token=? WHERE job_id=?",
                     (uuid.uuid4().hex, self.identity.run_id))
