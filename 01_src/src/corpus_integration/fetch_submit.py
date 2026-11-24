"""Bounded, best-effort handoff of an already returned Fetch observation."""
from contextvars import copy_context
from dataclasses import dataclass
import logging
import os
from queue import Full, Queue
import sqlite3
from threading import Lock, Thread

from .settings import growth_enabled

logger = logging.getLogger(__name__)
MAX_OBSERVATION_CHARS = 1024 * 1024
_pending = Queue(maxsize=8)
_start_lock = Lock()
_consumer = None


@dataclass(frozen=True)
class SavedOriginal:
    """An original staged and authorized for this account by the host."""
    owner: int
    inbox_path: str
    sha256: str


def submit(winner, *, owner, doi="", title="", saved_original=None):
    if not growth_enabled() or not owner:
        return None
    root = os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    if not root:
        return {"retention_status": "handoff_unavailable"}
    if saved_original is not None:
        return _submit_saved(root, saved_original, owner, doi, title, winner.get("url") or "")
    content = winner.get("content")
    if winner.get("truncated") or type(content) is not str or not 0 < len(content) <= MAX_OBSERVATION_CHARS:
        return {"retention_status": "source_out_of_bounds"}
    if not _ensure_consumer():
        return {"retention_status": "handoff_busy"}
    # Passing the immutable string reference leaves byte encoding, disk and DB work to the consumer.
    try:
        _pending.put_nowait((copy_context(), owner, str(doi or "")[:512],
                             str(winner.get("url") or "")[:2048],
                             str(title or winner.get("title") or "")[:512], content))
    except Full:
        return {"retention_status": "queue_full"}
    return {"retention_status": "handoff_unconfirmed"}


def _submit_saved(root, receipt, owner, doi, title, url):
    if type(receipt) is not SavedOriginal or receipt.owner != owner:
        return {"retention_status": "receipt_denied"}
    from citrus_corpus.intake import Intake
    from citrus_corpus.store import Store

    try:
        job = Intake(Store(root, read_timeout=0.025)).submit_local(
            receipt.inbox_path, expected_sha256=receipt.sha256, actor=f"account-{owner}",
            doi=doi, url=url, title=title, timeout=0.025)
    except sqlite3.OperationalError:
        return {"retention_status": "handoff_busy"}
    except (OSError, ValueError) as exc:
        state = "queue_full" if str(exc) == "corpus_queue_full" else "handoff_unavailable"
        return {"retention_status": state}
    return {"retention_status": "accepted", "intake_id": job["id"], "intake_state": job["state"]}


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
            logger.warning("Isolated Fetch retention unavailable: %s", type(exc).__name__)
        finally:
            _pending.task_done()


def _persist(owner, doi, url, title, content):
    from citrus_corpus.intake import Intake
    from citrus_corpus.store import Store
    from src.auth import user_by_id
    from src.core.capability_policy import allows

    if not growth_enabled() or not allows("web_fetch"):
        logger.info("Isolated Fetch handoff rejected: policy_denied")
        return
    user = user_by_id(int(owner)) or {}
    if not user.get("active") or user.get("approval_status") != "approved":
        logger.info("Isolated Fetch handoff rejected: account_denied")
        return
    root = os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    if not root:
        logger.info("Isolated Fetch handoff rejected: root_unavailable")
        return
    prefix = content[:120].lstrip().lower()
    fmt = "xml" if prefix.startswith("<?xml") else "html" if prefix.startswith("<") else "markdown"
    Intake(Store(root)).submit_observed(content, fmt=fmt, actor=f"account-{owner}", doi=doi, url=url, title=title)
