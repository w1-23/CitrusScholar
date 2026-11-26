"""Offline recovery invariants with real application schemas and disposable SQLite."""
import asyncio
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from src.agent.identity import ExecutionIdentity
from src.core import chat_turn_queue, jobs, task_events, user_questions
from src.core.account_leases import bound_account_lease
from src.tools import registry

OWNER = 7
SESSION = "fixture-session"


@pytest.fixture
def identity(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "sessions.db")
    chat_turn_queue.ensure_schema()
    run = jobs.claim_job(SESSION, "fixture-request", owner_user_id=OWNER).job_id
    return ExecutionIdentity(OWNER, SESSION, "fixture-request", run)


def _question(identity, prompt):
    return user_questions.create_question(identity.run_id, owner_user_id=OWNER, session_id=SESSION,
                                          prompt=prompt, options=[], allow_text=True)


def test_recovered_run_has_one_new_question_and_keeps_old_done_immutable(identity):
    run = identity.run_id
    first = _question(identity, "An older draft question")
    assert first["recovery_cursor"] == 0
    task_events.init_budget(run, time.time() + 60)
    assert jobs.mark_interrupted_jobs() == 1
    before = task_events.replay(run)
    old_done, = [event for event in before["events"] if event["event"] == "done"]
    admission = jobs.claim_agent_recovery(identity)
    cursor = admission["recovery_cursor"]
    assert cursor > old_done["seq"]
    assert user_questions.list_questions(run, owner_user_id=OWNER, session_id=SESSION) == []
    second = _question(identity, "A new exact question after recovery")
    assert second["id"] != first["id"] and second["recovery_cursor"] == cursor
    assert _question(identity, second["prompt"])["id"] == second["id"]
    with pytest.raises(user_questions.QuestionConflict, match="question_already_exists"):
        _question(identity, "Not another question in this generation")
    assert task_events.projection(run)["recovery_cursor"] == cursor
    replay = task_events.replay(run)
    assert replay["events"][:len(before["events"])] == before["events"]
    assert replay["recovery_cursor"] == cursor
    current = [e for e in replay["events"] if e["event"] == "request_user_input" and e["seq"] > cursor]
    assert [e["data"]["question"]["id"] for e in current] == [second["id"]]
    scope = {"owner_user_id": OWNER, "session_id": SESSION}
    with pytest.raises(user_questions.QuestionConflict, match="question_version_conflict"):
        user_questions.answer_question(run, first["id"], **scope, version=1, text="stale", client_request_id="old")
    assert jobs.update_job(run, status="awaiting_input")
    answer = user_questions.answer_question(run, second["id"], **scope, version=1, text="fresh", client_request_id="new")
    assert user_questions.get_continuation(answer["child_job_id"], **scope)["question_answer"]["question"]["id"] == second["id"]
    action, child = user_questions.queue_wait_state(run, **scope)
    assert (action, child) == ("dispatch", answer["child_job_id"])


def test_question_read_is_side_effect_free_for_expired_row(identity, monkeypatch):
    run = identity.run_id
    first = _question(identity, "Expired only in read projection")
    with task_events.transaction() as conn:
        conn.execute("UPDATE task_questions SET expires_at=1 WHERE id=?", (first["id"],))
    monkeypatch.setattr(user_questions, "expire_questions", lambda: pytest.fail("GET attempted expiry writes"))
    view, = user_questions.list_questions(run, owner_user_id=OWNER, session_id=SESSION)
    assert view["status"] == "expired"
    with jobs.read_transaction() as conn:
        stored = conn.execute("SELECT status,pin_until FROM task_questions").fetchone()
        assert stored["status"] == "pending" and stored["pin_until"] > 1


def test_old_question_schema_migrates_transactionally_to_initial_generation(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    monkeypatch.setattr(jobs, "_DB_PATH", path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""CREATE TABLE task_questions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
            owner_user_id INTEGER NOT NULL,session_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,
            question TEXT NOT NULL,context TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '',answer_hash TEXT NOT NULL DEFAULT '',
            client_request_id TEXT NOT NULL DEFAULT '',child_job_id TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,
            expires_at REAL NOT NULL,pin_until REAL NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO task_questions VALUES ('old','job',7,'session',1,'pending','{}','{}','','','','',0,100,100)")
    chat_turn_queue.ensure_schema()
    chat_turn_queue.ensure_schema()
    with jobs.read_transaction() as conn:
        old = conn.execute("SELECT id,recovery_cursor,pin_until FROM task_questions").fetchone()
        assert tuple(old) == ("old", 0, 100)
    with task_events.transaction() as conn:
        conn.execute("INSERT INTO task_questions SELECT 'new',job_id,owner_user_id,session_id,version,status,question,context,"
                     "answer,answer_hash,client_request_id,child_job_id,created_at,expires_at,pin_until,44 FROM task_questions")
        assert conn.execute("SELECT COUNT(*) FROM task_questions WHERE job_id='job'").fetchone()[0] == 2


def test_safe_operation_resumes_with_original_call_id_and_same_lease(identity, monkeypatch):
    run = identity.run_id
    call_id = "original-model-call-id"
    operation_id, _ = task_events.start_operation(run, OWNER, "fixture_read", {"version": 2},
        recovery_mode="pure_computation", tool_call_id=call_id, logical_key="fixture")
    assert jobs.mark_interrupted_jobs() == 1
    spec = registry.ToolSpec(func=object(), recovery_mode="pure_computation")
    monkeypatch.setattr(registry, "get_tool_spec", lambda name: spec if name == "fixture_read" else None)
    dispatched = []

    async def invoke(tool, call, *, recovering, lease_seconds):
        dispatched.append((tool, call, recovering, lease_seconds))
        assert 0 < lease_seconds <= task_events.OPERATION_LEASE_SECONDS == 600
        with jobs.read_transaction() as conn:
            remaining = conn.execute("SELECT lease_until FROM task_operations WHERE operation_id=?",
                                     (operation_id,)).fetchone()[0] - time.time()
        assert abs(remaining - lease_seconds) < 0.5
        return ToolMessage(content="verified fixture", tool_call_id=call["id"], name=call["name"])

    monkeypatch.setattr(registry, "_execute_single_tool", invoke)
    with pytest.raises(task_events.TaskConflict, match="durable_operation_context_required"):
        asyncio.run(registry.resume_safe_operation(run, operation_id, OWNER))
    with bound_account_lease(SimpleNamespace(owner=OWNER)):
        result = asyncio.run(registry.resume_safe_operation(run, operation_id, OWNER))
    assert result["result"] == "verified fixture"
    assert dispatched[0][1]["id"] == call_id and dispatched[0][2] is True
    assert jobs.read_job(run)["status"] == "failed"
    events = [event["data"] for event in task_events.replay(run)["events"] if event["event"] == "operation"]
    assert [event["tool_call_id"] for event in events] == [call_id] * 3
    assert [event["attempt"] for event in events] == [1, 2, 2]


def test_safe_resume_rejects_orphan_owner_and_legacy_missing_call(identity):
    run = identity.run_id
    no_call, _ = task_events.start_operation(run, OWNER, "fixture_read", {}, recovery_mode="pure_computation")
    orphan, _ = task_events.start_operation(run, OWNER, "fixture_read", {},
        recovery_mode="pure_computation", tool_call_id="retained")
    jobs.mark_interrupted_jobs()
    with pytest.raises(task_events.TaskConflict, match="unsafe_or_unavailable_resume"):
        task_events.claim_resume(run, no_call, OWNER)
    with task_events.transaction() as conn:
        conn.execute("UPDATE task_jobs SET owner_user_id=NULL WHERE job_id=?", (run,))
    with pytest.raises(task_events.TaskConflict, match="unsafe_or_unavailable_resume"):
        task_events.claim_resume(run, orphan, OWNER)
    assert not any(op["resumable"] for op in task_events.projection(run)["operations"])
