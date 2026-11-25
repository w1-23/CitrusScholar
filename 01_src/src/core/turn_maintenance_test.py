import asyncio
from contextvars import ContextVar

from src.core import turn_maintenance


def test_queue_work_takes_priority_over_after_turn_maintenance(monkeypatch):
    from src import auth
    from src.core import chat_turn_queue, jobs, task_events

    writes = []
    outstanding = []
    account = {"active": True, "approval_status": "approved"}
    turn = {"owner_user_id": 1, "session_id": "sid", "client_request_id": "req",
            "state": chat_turn_queue.TERMINAL}
    job = {"owner_user_id": 1, "session_id": "sid", "status": "completed"}
    monkeypatch.setattr(auth, "user_by_id", lambda owner: account)
    monkeypatch.setattr(chat_turn_queue, "get_turn", lambda **kwargs: turn)
    monkeypatch.setattr(chat_turn_queue, "session_blocker", lambda **kwargs: None)
    monkeypatch.setattr(chat_turn_queue, "list_turns", lambda **kwargs: outstanding)
    monkeypatch.setattr(jobs, "get_job", lambda job_id: job)
    monkeypatch.setattr(jobs, "decode_terminal_snapshot", lambda job: {"history_saved": True})
    monkeypatch.setattr(task_events, "append_event", lambda *args: writes.append(args))

    async def verify():
        source_context = ContextVar("private request", default=False)
        source_context.set(True)
        entered = asyncio.Event()
        permit_finish = asyncio.Event()
        lock = asyncio.Lock()
        calls = []

        async def maintain(session_lock, is_busy):
            assert session_lock is lock
            assert not source_context.get()
            calls.append(1)
            entered.set()
            await permit_finish.wait()
            return "busy" if await is_busy() else "compacted"

        async def complete_turn():
            turn_maintenance.offer(maintain)
            return "answer"

        offers = []
        assert await turn_maintenance.capture(complete_turn(), offers) == "answer"
        assert offers == [maintain] and not entered.is_set()
        schedule = lambda: turn_maintenance.schedule(turn, "job", callback=offers[0], session_lock=lock)
        outstanding.append({"state": "queued"})
        schedule()
        await turn_maintenance.background.drain()
        assert not calls and not writes
        outstanding.clear()
        schedule()
        await asyncio.wait_for(entered.wait(), timeout=2)
        schedule()
        outstanding.append({"state": "queued"})
        permit_finish.set()
        await turn_maintenance.background.drain()
        assert calls == [1] and not writes
        outstanding.clear()
        schedule()
        await turn_maintenance.background.drain()
        assert calls == [1, 1]
        assert writes[0][:2] == ("job", "context_maintenance")
        assert set(writes[0][2]) == {"status", "elapsed_ms"}

    asyncio.run(verify())
