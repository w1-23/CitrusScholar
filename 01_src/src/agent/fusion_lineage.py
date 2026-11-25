"""Authorized, owner-bound historical admissions for accepted Fusion reports."""
from __future__ import annotations

from .identity import ExecutionIdentity


def prior_report_jobs(identity):
    """Bounded verified admission lineage, never assistant or model history."""
    from src.core.task_events import transaction

    owner, sid = identity.owner_user_id, identity.session_id
    with transaction() as conn:
        rows = conn.execute("""WITH RECURSIVE
            current_line(job_id,parent_job_id) AS (
              SELECT job_id,parent_job_id FROM task_jobs WHERE job_id=? AND owner_user_id=? AND session_id=?
              UNION SELECT p.job_id,p.parent_job_id FROM task_jobs p JOIN current_line c ON p.job_id=c.parent_job_id
              WHERE p.owner_user_id=? AND p.session_id=?),
            current_turn AS (
              SELECT q.queue_seq FROM chat_turn_queue q JOIN current_line c ON c.job_id=q.root_job_id
              WHERE q.owner_user_id=? AND q.session_id=? AND q.started_at>0),
            prior_turns(root) AS (
              SELECT root_job_id FROM chat_turn_queue WHERE owner_user_id=? AND session_id=?
              AND queue_seq<(SELECT queue_seq FROM current_turn) AND started_at>0 AND state='terminal'
              ORDER BY queue_seq DESC LIMIT 32),
            prior_line(job_id) AS (
              SELECT root FROM prior_turns UNION
              SELECT j.job_id FROM task_jobs j JOIN prior_line p ON j.parent_job_id=p.job_id
              WHERE j.owner_user_id=? AND j.session_id=?),
            authorized(job_id) AS (
              SELECT job_id FROM prior_line UNION SELECT job_id FROM current_line WHERE EXISTS (SELECT 1 FROM current_turn))
            SELECT j.job_id,j.request_id FROM task_jobs j JOIN authorized a ON a.job_id=j.job_id
            WHERE j.owner_user_id=? AND j.session_id=? AND j.job_type='chat' AND j.job_id<>?
            AND j.status IN ('completed','failed','awaiting_input') ORDER BY j.created_at DESC LIMIT 65""",
            (identity.run_id, owner, sid, owner, sid, owner, sid, owner, sid, owner, sid, owner, sid, identity.run_id)).fetchall()
    if len(rows) > 64:
        raise LookupError("external_report_lineage_bound_exceeded")
    return {ExecutionIdentity(owner, sid, turn, run) for run, turn in rows}
