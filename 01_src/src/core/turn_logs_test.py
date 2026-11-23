"""Operator TXT turn logs: real 12-char root ids, turn scoping, never fatal."""
import asyncio
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import config, logger as citrus_logger
from src.core import chat_turn_queue as queue
from src.core import jobs
from src.core import turn_logs
from src.session import manager


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(jobs, "_DB_PATH", state / "sessions.db")
    monkeypatch.setattr(manager, "STATE_DIR", state)
    monkeypatch.setattr(manager, "DB_PATH", state / "sessions.db")
    monkeypatch.setattr(manager.SessionManager, "_instance", None)
    monkeypatch.setattr(citrus_logger, "_log_dir", lambda: logs)
    sessions = manager.SessionManager()
    monkeypatch.setattr(manager, "session_manager", sessions)
    queue.ensure_schema()
    return {"root": tmp_path, "logs": logs, "sessions": sessions}


def _drive_terminal(owner=7, sid="s-log", reason="done"):
    turn = queue.enqueue_turn(owner_user_id=owner, session_id=sid,
                              client_request_id="cr-" + uuid.uuid4().hex,
                              bounded_payload={"query": "本轮问题"})
    turn = queue.claim_head(owner_user_id=owner, session_id=sid)
    turn = queue.mark_dispatch_intent(owner_user_id=owner, session_id=sid,
                                      client_request_id=turn["client_request_id"],
                                      generation=turn["generation"])
    turn = queue.mark_started(owner_user_id=owner, session_id=sid,
                              client_request_id=turn["client_request_id"],
                              generation=turn["generation"])
    turn = queue.begin_settling(owner_user_id=owner, session_id=sid,
                                client_request_id=turn["client_request_id"],
                                generation=turn["generation"],
                                history_saved=True, clear_reason=reason)
    return queue.finalize_terminal(owner_user_id=owner, session_id=sid,
                                   client_request_id=turn["client_request_id"],
                                   generation=turn["generation"],
                                   history_saved=True, reason=reason)


def _save_turn(env, sid, request_id, question, answer):
    asyncio.run(env["sessions"].save_messages(
        sid, [HumanMessage(content=question), AIMessage(content=answer)],
        idempotency_key=f"crid:{request_id}"))


def test_12_char_root_job_writes_one_log(log_env):
    sid = "s-log"
    turn = queue.enqueue_turn(owner_user_id=7, session_id=sid,
                              client_request_id="cr-" + uuid.uuid4().hex,
                              bounded_payload={"query": "十二位日志"})
    job_id = turn["root_job_id"]
    assert len(job_id) == 12
    _save_turn(log_env, sid, turn["client_request_id"], "十二位日志", "回答")
    turn = queue.claim_head(owner_user_id=7, session_id=sid)
    turn = queue.mark_dispatch_intent(owner_user_id=7, session_id=sid,
                                      client_request_id=turn["client_request_id"],
                                      generation=turn["generation"])
    turn = queue.mark_started(owner_user_id=7, session_id=sid,
                              client_request_id=turn["client_request_id"],
                              generation=turn["generation"])
    turn = queue.begin_settling(owner_user_id=7, session_id=sid,
                                client_request_id=turn["client_request_id"],
                                generation=turn["generation"],
                                history_saved=True, clear_reason="done")
    queue.finalize_terminal(owner_user_id=7, session_id=sid,
                            client_request_id=turn["client_request_id"],
                            generation=turn["generation"],
                            history_saved=True, reason="done")
    files = list((log_env["logs"] / "turns").rglob(f"{job_id}.txt"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "十二位日志" in body
    assert "本轮对话" in body


def test_log_contains_only_this_turn(log_env):
    sid = "s-scope"
    a = queue.enqueue_turn(owner_user_id=7, session_id=sid,
                           client_request_id="req-a", bounded_payload={"query": "A问题"})
    b = queue.enqueue_turn(owner_user_id=7, session_id=sid,
                           client_request_id="req-b", bounded_payload={"query": "B问题"})
    _save_turn(log_env, sid, "req-a", "A问题唯一正文", "A回答唯一正文")
    _save_turn(log_env, sid, "req-b", "B问题唯一正文", "B回答唯一正文")
    turn_logs.write_turn_log({
        "owner_user_id": 7, "session_id": sid, "root_job_id": a["root_job_id"],
        "client_request_id": "req-a", "finished_at": time.time(),
        "history_saved": True, "clear_reason": "done",
    })
    files = list((log_env["logs"] / "turns").rglob(f"{a['root_job_id']}.txt"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "A问题唯一正文" in body and "A回答唯一正文" in body
    assert "B问题唯一正文" not in body and "B回答唯一正文" not in body


def test_log_rejects_traversal_and_bad_ids(log_env):
    for bad in ("../../escape", "ABCDEF123456", "short"):
        turn_logs.write_turn_log({
            "owner_user_id": 7, "session_id": "s", "root_job_id": bad,
            "client_request_id": "req", "finished_at": time.time(),
            "history_saved": True, "clear_reason": "done",
        })
    assert list((log_env["logs"] / "turns").rglob("*.txt")) == []


def test_log_failure_never_changes_terminal_state(log_env, monkeypatch):
    sid = "s-fail"
    def boom(_turn):
        raise RuntimeError("disk full")
    monkeypatch.setattr(turn_logs, "write_turn_log", boom)
    view = _drive_terminal(owner=7, sid=sid)
    assert view["state"] == queue.TERMINAL
    assert view["history_saved"] is True


def test_cancel_and_reconcile_paths_also_write_log(log_env):
    sid = "s-cancel"
    turn = queue.enqueue_turn(owner_user_id=7, session_id=sid,
                              client_request_id="req-cancel",
                              bounded_payload={"query": "取消问题"})
    queue.cancel_queued(owner_user_id=7, session_id=sid,
                        client_request_id="req-cancel")
    assert list((log_env["logs"] / "turns").rglob(f"{turn['root_job_id']}.txt"))

    sid2 = "s-reconcile"
    turn2 = queue.enqueue_turn(owner_user_id=7, session_id=sid2,
                               client_request_id="req-rec",
                               bounded_payload={"query": "对账问题"})
    turn2 = queue.claim_head(owner_user_id=7, session_id=sid2)
    turn2 = queue.mark_dispatch_intent(owner_user_id=7, session_id=sid2,
                                       client_request_id=turn2["client_request_id"],
                                       generation=turn2["generation"])
    queue.mark_reconcile_required(owner_user_id=7, session_id=sid2,
                                  client_request_id="req-rec", reason="unknown")
    queue.resolve_reconcile(owner_user_id=7, session_id=sid2,
                            client_request_id="req-rec", reason="turn_reconciled_by_user")
    assert list((log_env["logs"] / "turns").rglob(f"{turn2['root_job_id']}.txt"))
