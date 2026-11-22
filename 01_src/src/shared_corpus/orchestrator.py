"""Bounded processing of originals already acquired by authorized Fetch."""
import logging
import sqlite3
import time

from .runtime import MAX_INDEX_DOCUMENTS

logger = logging.getLogger(__name__)

REVIEW_TTL_SEC = 60 * 86400
DAILY_PREPARE_LIMIT = 24
BATCH_DOCUMENTS = 5
BATCH_WAIT_SEC = 300
MAX_PENDING = 100


def schedule(runtime, candidate_id, owner, *, verified_license=False, raw_bytes=0):
    from .prepare import MAX_SOURCE_BYTES

    if runtime is None:
        return "disabled"
    if raw_bytes > MAX_SOURCE_BYTES:
        return "source_too_large"
    try:
        with runtime.journal.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM shared_preparations WHERE candidate_id=?",
                                    (candidate_id,)).fetchone()
            stronger = existing is not None and verified_license > existing["verified_license"]
            if existing and existing["state"] in {"queued", "queue_full"}:
                if stronger:
                    conn.execute("UPDATE shared_preparations SET owner=?, verified_license=1 WHERE candidate_id=?",
                                 (owner, candidate_id))
                return existing["state"]
            if existing and not stronger:
                return existing["state"]
            pending = conn.execute("SELECT count(*) FROM shared_preparations WHERE state='queued'").fetchone()[0]
            state = "queued" if pending < MAX_PENDING else "queue_full"
            conn.execute("INSERT INTO shared_preparations (candidate_id,owner,state,created,verified_license) "
                         "VALUES (?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET owner=excluded.owner, "
                         "state=excluded.state,created=excluded.created,finished=NULL,"
                         "verified_license=excluded.verified_license",
                         (candidate_id, owner, state, time.time(), int(verified_license)))
    except sqlite3.Error as error:
        logger.warning("Shared literature preparation scheduling failed: %s", type(error).__name__)
        return "scheduling_unavailable"
    if state == "queued":
        runtime.notify()
    return state


def process_one(runtime):
    """At most one candidate per pass. The catalog operations are idempotent."""
    from src.auth import user_by_id

    now = time.time()
    with runtime.journal.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE shared_preparations SET state='expired',finished=? "
                     "WHERE state IN ('queued','queue_full') AND created<?", (now, now - REVIEW_TTL_SEC))
        completed = conn.execute("SELECT count(*) FROM shared_preparations WHERE finished>? "
                                 "AND state IN ('prepared','failed')", (now - 86400,)).fetchone()[0]
        if completed >= DAILY_PREPARE_LIMIT:
            return
        queued = conn.execute("SELECT count(*) FROM shared_preparations WHERE state='queued'").fetchone()[0]
        if queued < MAX_PENDING:
            conn.execute("UPDATE shared_preparations SET state='queued' WHERE candidate_id IN "
                         "(SELECT candidate_id FROM shared_preparations WHERE state='queue_full' "
                         "ORDER BY created,candidate_id LIMIT ?)", (MAX_PENDING - queued,))
        row = conn.execute("SELECT candidate_id,owner,verified_license FROM shared_preparations WHERE state='queued' "
                           "ORDER BY created,candidate_id LIMIT 1").fetchone()
    if row is None:
        return
    candidate, owner, verified_license = row
    user = user_by_id(owner)
    state = "expired"
    if user and user.get("active") and user.get("approval_status") == "approved":
        try:
            proposal = runtime.catalog.propose(candidate, owner)
            prepared = runtime.catalog.prepare(proposal["proposal_id"], owner)
            state = "failed" if prepared["state"] == "failed" else "prepared"
        except (LookupError, ValueError, OSError):
            state = "failed"
    with runtime.journal.connection() as conn:
        conn.execute("UPDATE shared_preparations SET state=?,finished=? WHERE candidate_id=? "
                     "AND state='queued' AND verified_license=?", (state, now, candidate, verified_license))


def maybe_build(runtime):
    if runtime.journal.pending():
        return
    # Automatic batches use the same deduplication and daily limits as manual ones.
    authorized = runtime.catalog.publishable()
    fresh = [item for item in authorized if item["version_id"] not in runtime.journal.active_versions(
        runtime.generation, item["document_id"])]
    if not fresh or len(authorized) > MAX_INDEX_DOCUMENTS:
        return
    # A lone new document is published after a short batching interval.
    from datetime import datetime
    oldest = min(datetime.fromisoformat(item["prepared_at"]).timestamp() for item in fresh)
    if len(fresh) < BATCH_DOCUMENTS and time.time() - oldest < BATCH_WAIT_SEC:
        return
    try:
        runtime.enqueue_build(0, authorized)
    except ValueError:
        return
