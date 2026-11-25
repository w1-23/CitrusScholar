"""Queue-bound scheduling for optional after-turn framework maintenance."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import Context, ContextVar

from src.core import background

logger = logging.getLogger(__name__)
BusyProbe = Callable[[], Awaitable[bool]]
Maintenance = Callable[[asyncio.Lock, BusyProbe], Awaitable[str]]
_capture: ContextVar[list[Maintenance] | None] = ContextVar("turn_maintenance", default=None)
_running: set[tuple[int, str]] = set()
_OUTCOMES = frozenset({"compacted", "busy", "stale", "ineligible", "failed"})


async def capture(operation: Awaitable, offers: list[Maintenance]):
    """Collect a candidate from the turn without persisting a callable in graph state."""
    token = _capture.set(offers)
    try:
        return await operation
    finally:
        _capture.reset(token)


def offer(callback: Maintenance) -> None:
    offered = _capture.get()
    if offered is not None:
        offered[:] = [callback]


def schedule(turn: dict, job_id: str, *, callback: Maintenance, session_lock: asyncio.Lock) -> None:
    owner, sid, rid = turn["owner_user_id"], turn["session_id"], turn["client_request_id"]
    key = (owner, sid)
    # Best effort and no queue: idle work must not pile up competing with
    # newly admitted requests at the provider, even across different sessions.
    if _running:
        return
    _running.add(key)

    def busy() -> bool:
        from src.auth import user_by_id
        from src.core import chat_turn_queue as queue, jobs
        account = user_by_id(owner)
        if not account or not account.get("active") or account.get("approval_status") != "approved":
            return True
        accepted = queue.get_turn(owner_user_id=owner, session_id=sid, client_request_id=rid)
        if not accepted or accepted["state"] != queue.TERMINAL:
            return True
        job = jobs.get_job(job_id)
        if not job or job["owner_user_id"] != owner or job["session_id"] != sid:
            return True
        if job["status"] not in jobs.TERMINAL_STATUS or not (jobs.decode_terminal_snapshot(job) or {}).get("history_saved"):
            return True
        if queue.session_blocker(owner_user_id=owner, session_id=sid):
            return True
        return bool(queue.list_turns(owner_user_id=owner, session_id=sid, include_terminal=False))

    async def is_busy() -> bool:
        return await asyncio.to_thread(busy)

    async def run() -> None:
        try:
            if await is_busy():
                return
            started = time.monotonic()
            outcome = await callback(session_lock, is_busy)
            if outcome in _OUTCOMES and outcome not in {"busy", "stale", "ineligible"}:
                from src.core.task_events import append_event
                await asyncio.to_thread(append_event, job_id, "context_maintenance", {
                    "status": outcome, "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("After-turn context maintenance deferred (%s)", type(exc).__name__)
        finally:
            _running.discard(key)

    # Do not inherit the request progress, budget, auth, or cancellation context.
    Context().run(background.spawn, run())
