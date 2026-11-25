"""Bounded, best-effort backend handoff from the dedicated Fetch Paper tool."""
from contextvars import copy_context
import logging
import os
from queue import Full, Queue
from threading import Lock, Thread

from .settings import growth_enabled

logger = logging.getLogger(__name__)
_pending = Queue(maxsize=8)
_start_lock = Lock()
_consumer = None


def submit(*, owner: int | None, doi: str, pmid: str, winner_url: str = "") -> None:
    from .paper_source import identifiers

    if not growth_enabled() or type(owner) is not int or owner <= 0:
        return
    root = os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    if not root:
        return
    doi, pmid = identifiers(doi, pmid, winner_url)
    if not (doi or pmid) or not _ensure_consumer():
        return
    # Nothing from the search tool's body, headers, title or license enters the
    # queue. Items are not durable until the worker verifies and registers them.
    try:
        _pending.put_nowait((copy_context(), owner, root, doi, pmid))
    except Full:
        logger.debug("Isolated Fetch Paper handoff unavailable: queue_full")


def _ensure_consumer():
    global _consumer
    if _consumer is not None and _consumer.is_alive():
        return True
    if not _start_lock.acquire(blocking=False):
        return False
    try:
        if _consumer is None or not _consumer.is_alive():
            _consumer = Thread(target=_drain, name="isolated-corpus-submission", daemon=True)
            _consumer.start()
        return True
    finally:
        _start_lock.release()


def _drain():
    while True:
        context, *receipt = _pending.get()
        try:
            context.run(_persist, *receipt)
        except Exception as exc:
            logger.warning("Isolated Fetch Paper handoff unavailable: %s", type(exc).__name__)
        finally:
            _pending.task_done()


def _persist(owner: int, root: str, doi: str, pmid: str):
    from src.auth import user_by_id
    from src.core.capability_policy import allows
    from src.core.tracing import get_user_id

    from .paper_source import resolve

    def allowed():
        # Network capabilities also require external_data_network and the
        # captured request's web opt-in, and consult live revocations.
        return (growth_enabled() and root == os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
                and get_user_id() == owner and allows("web_fetch") and allows("bibliographic_lookup"))

    def approved():
        user = user_by_id(owner) or {}
        return (user.get("user_id") == owner and user.get("active") is True
                and user.get("approval_status") == "approved")

    if not allowed() or not approved():
        return
    record = resolve(doi, pmid, allowed=allowed)
    if record is None or not allowed() or not approved():
        return
    from citrus_corpus.auto_intake import AutoIntake
    from citrus_corpus.store import Store

    return AutoIntake(Store(root)).register(**record)
