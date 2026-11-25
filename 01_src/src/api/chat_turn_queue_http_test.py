"""Queue HTTP and execution lifecycle with temporary stores and a controlled graph."""
import asyncio
from contextlib import asynccontextmanager
from time import monotonic, sleep
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
import pytest

from src import auth
from src.api import main
from src.core import chat_turn_queue as queue, jobs, user_questions
from src.core.account_leases import current_account_lease
from src.core.tracing import get_job_id
from src.core.chat_capacity import ChatCapacity
from src.graph import graph
from src.session import files, manager


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(auth, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(jobs, "_DB_PATH", db)
    monkeypatch.setattr(manager, "DB_PATH", db)
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)
    monkeypatch.setattr(manager.SessionManager, "_instance", None)
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.sqlite3")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    monkeypatch.setattr(main, "chat_capacity", ChatCapacity())
    sessions = manager.SessionManager()
    monkeypatch.setattr(main, "session_manager", sessions)
    auth.init_store()
    files.init_store()
    password = "Fixture-only-8043!"
    owner = auth.create_local_account("queue-owner", password)
    other_owner = auth.create_local_account("queue-other", password)
    sid = "queue-http-session"
    asyncio.run(sessions.get_or_create_session(sid, owner_user_id=owner))
    queue.ensure_schema()

    started = []
    released = {"legacy", "last"}
    asking, hold_cleanup, held = set(), set(), {}
    graph_errors = set()

    class ControlledGraph:
        async def astream(self, state, **_):
            cid = state["client_request_id"]
            started.append(cid)
            deadline = monotonic() + 8
            while cid not in released:
                if monotonic() > deadline:
                    raise TimeoutError("fixture release missing")
                await asyncio.sleep(0.01)
            if cid in graph_errors:
                raise RuntimeError("fixture private provider detail")
            answer = "answer to " + state["query"]
            output = {"answer": answer, "turn_outcome": {"status": "completed"}}
            if cid in asking:
                question = user_questions.create_question(get_job_id(), owner_user_id=owner, session_id=sid,
                    prompt="Which option?", options=[], allow_text=True, context={"query": state["query"]})
                output.update(awaiting_input=True, pending_question=question)
                if cid in hold_cleanup:
                    held[cid] = current_account_lease().retain()
            yield {"supervisor": output}
            await sessions.save_messages(state["session_id"],
                [HumanMessage(content=state["query"]), AIMessage(content=answer)],
                idempotency_key=state["idempotency_key"])
            yield {"session_save": {"_trace": {"history_saved": True}}}

    monkeypatch.setattr(graph, "build_graph", lambda _: ControlledGraph())

    @asynccontextmanager
    async def lifespan(app):
        await main._reconcile_turn_queue_startup()
        app.state.turn_queue_wake = asyncio.Event()
        app.state.turn_queue_consumer = asyncio.create_task(main._consume_chat_turns(app.state.turn_queue_wake))
        try:
            yield
        finally:
            app.state.turn_queue_consumer.cancel()
            await asyncio.gather(app.state.turn_queue_consumer, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)
    app.middleware("http")(main.account_auth_guard)
    root = "/api/v2/session/{session_id}"
    for path, handler, method in (
        ("/api/v2/sessions", main.list_sessions, "GET"),
        (root + "/messages", main.session_messages, "GET"),
        ("/api/v1/session/{session_id}/clear", main.clear_session_endpoint, "POST"),
        ("/sessions/{session_id}/jobs", main.list_session_jobs, "GET"),
        (root + "/turn-queue/{client_request_id}/reconcile", main.reconcile_chat_turn, "POST"),
        ("/api/v2/chat/jobs/{job_id}/recover-agent", main.recover_chat_agent, "POST"),
        (root + "/turn-queue", main.enqueue_chat_turn, "POST"),
        (root + "/turn-queue", main.list_chat_turns, "GET"),
        (root + "/turn-queue/{client_request_id}", main.cancel_chat_turn, "DELETE"),
        (root + "/task-by-request/{client_request_id}", main.discover_chat_job, "GET"),
        ("/api/v2/chat", main.chat_v2, "POST"),
        ("/jobs/{job_id}/events", main.get_job_events, "GET"),
        ("/api/v2/chat/jobs/{job_id}/questions", main.get_task_questions, "GET"),
        ("/api/v2/chat/jobs/{job_id}/questions/{question_id}/answer", main.answer_task_question, "POST"),
    ):
        app.add_api_route(path, handler, methods=[method])
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        _, session_token = auth.login("queue-owner", password)
        client.cookies.set("citrus_auth", session_token)
        try:
            yield SimpleNamespace(client=client, sid=sid, started=started, released=released,
                                  owner=owner, path=f"/api/v2/session/{sid}/turn-queue",
                                  other_owner=other_owner, asking=asking, held=held, hold_cleanup=hold_cleanup,
                                  graph_errors=graph_errors,
                                  other=auth.login("queue-other", password)[1])
        finally:
            for lease in held.values():
                lease.release()


def wait_for(predicate):
    deadline = monotonic() + 6
    while not predicate():
        if monotonic() >= deadline:
            pytest.fail("expected queue transition did not happen")
        sleep(0.025)


def send(runtime, cid, query):
    response = runtime.client.post(runtime.path, json={"query": query, "client_request_id": cid})
    assert response.status_code == 202, response.text
    return response.json()


def state(runtime, cid):
    row = queue.get_turn(owner_user_id=runtime.owner, session_id=runtime.sid, client_request_id=cid)
    return row["state"] if row else None


def test_background_fifo_cancel_and_replay(runtime):
    first = send(runtime, "first", "first message")
    wait_for(lambda: runtime.started == ["first"])
    middle = send(runtime, "middle", "middle message")
    last = send(runtime, "last", "last message")
    assert middle["queue_seq"] + 1 == last["queue_seq"]
    blocker = {"job_id": first["job_id"], "kind": "running", "queue_seq": first["queue_seq"]}
    assert middle["blocked_by"] == last["blocked_by"] == blocker
    refreshed = {turn["client_request_id"]: turn for turn in runtime.client.get(runtime.path).json()["turns"]}
    assert refreshed["last"]["blocked_by"] == blocker
    assert state(runtime, "last") == "queued"
    cancelled = runtime.client.delete(runtime.path + "/middle")
    assert cancelled.status_code == 200, cancelled.text
    assert state(runtime, "first") == "running"
    runtime.released.add("first")
    wait_for(lambda: state(runtime, "last") == "terminal")
    assert runtime.started == ["first", "last"]
    for turn in (first, last):
        events = runtime.client.get(f'/jobs/{turn["job_id"]}/events').json()["events"]
        terminal = [row for row in events if row["event"] == "done"]
        assert len(terminal) == 1
        assert terminal[0]["data"]["history_saved"] is True


def test_rediscovery_idempotency_and_owner_scope(runtime):
    first = send(runtime, "last", "original")
    assert send(runtime, "last", "original")["job_id"] == first["job_id"]
    conflict = runtime.client.post(runtime.path, json={"query": "changed", "client_request_id": "last"})
    assert conflict.status_code == 409, conflict.text
    discover = runtime.path.replace("turn-queue", "task-by-request") + "/last"
    assert runtime.client.get(discover).json()["job_id"] == first["job_id"]
    runtime.client.cookies.set("citrus_auth", runtime.other)
    assert runtime.client.get(discover).status_code == 404
    assert runtime.client.get(runtime.path).status_code in {403, 404}
    assert runtime.client.get(f'/jobs/{first["job_id"]}/events').status_code == 404


def test_legacy_stream_observes_one_persistent_job(runtime):
    body = {"query": "legacy question", "session_id": runtime.sid, "client_request_id": "legacy"}
    response = runtime.client.post("/api/v2/chat", json=body)
    assert response.status_code == 200, response.text
    assert "event: done" in response.text
    replay = runtime.client.post("/api/v2/chat", json=body)
    assert replay.json()["replayed"] is True
    assert runtime.started == ["legacy"]
    assert replay.json()["job_id"] in response.text



def answer_question(runtime, job_id, *, cancelled=False):
    url = f"/api/v2/chat/jobs/{job_id}/questions"
    questions = runtime.client.get(url).json()["questions"]
    assert len(questions) == 1
    question = questions[0]
    response = runtime.client.post(f'{url}/{question["id"]}/answer', json={
        "version": question["version"], "client_request_id": "fixture-answer",
        "text": "a clarification" if not cancelled else "", "cancelled": cancelled,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_two_questions_preserve_original_queue_position_through_cleanup(runtime):
    runtime.asking.add("first")
    runtime.hold_cleanup.add("first")
    root = send(runtime, "first", "question task")
    follower = send(runtime, "last", "independent task")
    runtime.released.add("first")
    wait_for(lambda: jobs.get_job(root["job_id"])["status"] == "awaiting_input")
    assert state(runtime, "first") == "running"
    answer = answer_question(runtime, root["job_id"])
    assert answer["dispatch_pending"] is True
    assert answer_question(runtime, root["job_id"])["child_job_id"] == answer["child_job_id"]
    child = answer["child_job_id"]
    child_cid = jobs.get_job(child)["client_request_id"]
    runtime.asking.add(child_cid)
    assert jobs.get_job(child)["status"] == "queued"
    assert runtime.started == ["first"]
    runtime.held.pop("first").release()
    wait_for(lambda: runtime.started == ["first", child_cid])
    assert state(runtime, "last") == "queued"
    runtime.released.add(child_cid)
    wait_for(lambda: state(runtime, "first") == "awaiting_input")
    reply = answer_question(runtime, child)
    grandchild = reply["child_job_id"]
    grandchild_cid = jobs.get_job(grandchild)["client_request_id"]
    runtime.released.add(grandchild_cid)
    wait_for(lambda: state(runtime, "last") == "terminal")
    assert runtime.started == ["first", child_cid, grandchild_cid, "last"]
    turns = {t["job_id"]: t for t in runtime.client.get(runtime.path).json()["turns"]}
    assert turns[root["job_id"]]["state"] == turns[follower["job_id"]]["state"] == "terminal"
    assert turns[root["job_id"]]["active_child_job_id"] == grandchild
    events = runtime.client.get(f"/jobs/{grandchild}/events").json()["events"]
    terminal = [row for row in events if row["event"] == "done"]
    assert len(terminal) == 1 and terminal[0]["data"]["history_saved"] is True


def test_cancel_question_releases_head_without_creating_child(runtime):
    runtime.asking.add("first")
    root = send(runtime, "first", "question task")
    send(runtime, "last", "independent task")
    runtime.released.add("first")
    wait_for(lambda: state(runtime, "first") == "awaiting_input")
    assert not answer_question(runtime, root["job_id"], cancelled=True)["child_job_id"]
    wait_for(lambda: state(runtime, "last") == "terminal")
    assert runtime.started == ["first", "last"]
    settled = queue.get_turn(owner_user_id=runtime.owner, session_id=runtime.sid, client_request_id="first")
    assert settled["state"] == "terminal" and settled["clear_reason"] == "question_cancelled"


@pytest.mark.parametrize("claim_before_restart", [False, True])
def test_startup_recovers_durable_answer_or_fences_unknown_dispatch(runtime, monkeypatch, claim_before_restart):
    from time import time

    advance = main._advance_question_turn

    async def pause_dispatch(*_):
        return

    monkeypatch.setattr(main, "_advance_question_turn", pause_dispatch)
    runtime.asking.add("first")
    root = send(runtime, "first", "question task")
    runtime.released.add("first")
    wait_for(lambda: state(runtime, "first") == "awaiting_input")
    send(runtime, "last", "after restart")
    child = answer_question(runtime, root["job_id"])["child_job_id"]
    child_cid = jobs.get_job(child)["client_request_id"]
    runtime.released.add(child_cid)
    assert jobs.get_job(child)["status"] == "queued"
    if claim_before_restart:
        waiting = queue.find_turn_by_job(owner_user_id=runtime.owner, session_id=runtime.sid, job_id=root["job_id"])
        queue.resume_continuation(**main._turn_fence(waiting), active_child_job_id=child, deadline=time() + 60)
        jobs.mark_interrupted_jobs()
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    monkeypatch.setattr(main, "_advance_question_turn", advance)
    if claim_before_restart:
        assert state(runtime, "first") == "reconcile_required"
        assert state(runtime, "last") == "queued"
        assert runtime.started == ["first"]
    else:
        wait_for(lambda: state(runtime, "last") == "terminal")
        assert runtime.started == ["first", child_cid, "last"]


def test_startup_settles_completed_child(runtime, monkeypatch):
    settled_child = []
    settle = main._settle_dispatched_turn

    def interrupt_settlement(turn, job_id):
        if turn["active_child_job_id"] == job_id:
            settled_child.append(job_id)
        else:
            settle(turn, job_id)

    monkeypatch.setattr(main, "_settle_dispatched_turn", interrupt_settlement)
    runtime.asking.add("first")
    root = send(runtime, "first", "question task")
    runtime.released.add("first")
    wait_for(lambda: state(runtime, "first") == "awaiting_input")
    follower = send(runtime, "last", "after restart")
    child = answer_question(runtime, root["job_id"])["child_job_id"]
    child_cid = jobs.get_job(child)["client_request_id"]
    runtime.released.add(child_cid)
    wait_for(lambda: settled_child == [child])
    assert state(runtime, "first") == "running"
    assert state(runtime, "last") == "queued"
    assert jobs.decode_terminal_snapshot(jobs.get_job(child))["history_saved"] is True
    monkeypatch.setattr(main, "_settle_dispatched_turn", settle)
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    wait_for(lambda: state(runtime, "last") == "terminal")
    assert state(runtime, "first") == "terminal"
    assert runtime.started == ["first", child_cid, "last"]
    for job_id in (child, follower["job_id"]):
        events = runtime.client.get(f"/jobs/{job_id}/events").json()["events"]
        assert sum(row["event"] == "done" for row in events) == 1


def test_graph_exception_commits_one_failed_terminal_and_advances_queue(runtime):
    runtime.graph_errors.add("provider-error")
    runtime.released.update({"provider-error", "after-error"})
    failed = send(runtime, "provider-error", "failing question")
    following = send(runtime, "after-error", "following question")
    wait_for(lambda: state(runtime, "provider-error") == "terminal")
    wait_for(lambda: state(runtime, "after-error") == "terminal")

    failed_job = jobs.get_job(failed["job_id"])
    assert failed_job["status"] == "failed"
    assert failed_job["error"] == "RuntimeError"
    assert jobs.get_job(following["job_id"])["status"] == "completed"
    response = runtime.client.get(f"/jobs/{failed['job_id']}/events")
    assert response.status_code == 200
    assert "fixture private provider detail" not in response.text
    done = [event for event in response.json()["events"] if event["event"] == "done"]
    assert len(done) == 1
    assert done[0]["data"]["turn_status"] == "failed"
    assert done[0]["data"]["answer"] == ""
    assert done[0]["data"]["history_saved"] is False
    assert runtime.started == ["provider-error", "after-error"]


def test_clear_blocks_active_work_then_hides_task_projections(runtime, monkeypatch):
    async def pause_dispatch(*_):
        return

    monkeypatch.setattr(main, "_dispatch_queued_turn", pause_dispatch)
    sid = runtime.sid
    clear = f"/api/v1/session/{sid}/clear"
    messages = f"/api/v2/session/{sid}/messages"
    jobs_url = f"/sessions/{sid}/jobs"
    old = send(runtime, "old-visible", "synthetic old question")
    assert state(runtime, "old-visible") == "queued"
    assert runtime.client.post(clear).status_code == 409
    assert runtime.client.delete(runtime.path + "/old-visible").status_code == 200
    assert any(t.get("job_id") == old["job_id"] for t in runtime.client.get(messages).json()["turns"])
    cleared = runtime.client.post(clear)
    assert cleared.status_code == 200, cleared.text
    assert runtime.client.get(messages).json()["turns"] == []
    assert runtime.client.get(runtime.path).json()["turns"] == []
    assert runtime.client.get(jobs_url).json()["jobs"] == []
    assert runtime.client.get("/api/v2/sessions").json()["sessions"][0]["has_tasks"] is False
    assert jobs.get_job(old["job_id"])["history_hidden"] == 1
    new = send(runtime, "new-visible", "synthetic new question")
    assert runtime.client.delete(runtime.path + "/new-visible").status_code == 200
    assert [t["job_id"] for t in runtime.client.get(messages).json()["turns"]] == [new["job_id"]]
    assert [t["job_id"] for t in runtime.client.get(runtime.path).json()["turns"]] == [new["job_id"]]


def test_restart_before_outer_dispatch_releases_claim(runtime, monkeypatch):
    async def pause(*_):
        return

    dispatch = main._dispatch_queued_turn
    monkeypatch.setattr(main, "_dispatch_queued_turn", pause)
    turn = send(runtime, "last", "queued during restart")
    assert queue.claim_head(owner_user_id=runtime.owner, session_id=runtime.sid)["state"] == "claimed"
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    assert state(runtime, "last") == "queued"
    monkeypatch.setattr(main, "_dispatch_queued_turn", dispatch)
    wait_for(lambda: state(runtime, "last") == "terminal")
    assert runtime.started == ["last"] and jobs.get_job(turn["job_id"])["status"] == "completed"


def test_original_reviewed_answer_reopens_same_expired_segment(runtime, monkeypatch, tmp_path):
    import time
    from src.agent import recovery
    from src.agent.execution_segments import ExecutionSegments
    from src.agent.identity import ExecutionIdentity
    from src.core import task_events

    async def pause(*_):
        return

    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main, "_dispatch_queued_turn", pause)
    admitted = send(runtime, "first", "already reviewed")
    row = queue.claim_head(owner_user_id=runtime.owner, session_id=runtime.sid)
    queue.mark_dispatch_intent(**main._turn_fence(row), current_step="init")
    job = jobs.get_job(admitted["job_id"])
    identity = ExecutionIdentity(runtime.owner, runtime.sid, job["request_id"], job["job_id"])
    original_deadline = time.time() - 5
    task_events.init_budget(identity.run_id, original_deadline, max_calls=6)
    segments = ExecutionSegments(identity)
    old_claim = segments.start()
    segments.begin_finalize(old_claim)
    jobs.mark_interrupted_jobs()
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    assert state(runtime, "first") == "reconcile_required"
    assert segments.snapshot()["phase"] == "interrupted"

    async def mock_protected_answer(root, bound, *_, **__):
        assert bound == identity
        return {"job_id": bound.run_id, "request_id": bound.user_turn_id, "session_id": bound.session_id,
                "user_id": bound.owner_user_id, "client_request_id": "first", "idempotency_key": "crid:first",
                "query": "already reviewed", "agent_resume": True, "agent_recovery_answered": True}

    monkeypatch.setattr(recovery, "load_recovery_state", mock_protected_answer)
    runtime.released.add("first")
    response = runtime.client.post(f"/api/v2/chat/jobs/{identity.run_id}/recover-agent", json={})
    assert response.status_code == 200, response.text
    wait_for(lambda: state(runtime, "first") == "terminal")
    assert jobs.get_job(identity.run_id)["status"] == "completed"
    assert segments.snapshot()["phase"] == "terminal" and runtime.started == ["first"]
    with task_events.transaction() as conn:
        assert conn.execute("SELECT deadline FROM task_budgets WHERE job_id=?", (identity.run_id,)).fetchone()[0] == original_deadline
        assert task_events.current_terminal_event(conn, identity.run_id)["event"] == "done"


def test_interactive_reconciliation_closes_active_child_and_retains_unknown_ledger(runtime, monkeypatch, tmp_path):
    import time
    from src.agent.execution_segments import ExecutionSegments
    from src.agent.identity import ExecutionIdentity
    from src.core import task_events

    async def pause(*_):
        return

    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main, "_advance_question_turn", pause)
    runtime.asking.add("first")
    root = send(runtime, "first", "needs a question")
    runtime.released.add("first")
    wait_for(lambda: state(runtime, "first") == "awaiting_input")
    child = answer_question(runtime, root["job_id"])["child_job_id"]
    row = queue.find_turn_by_job(owner_user_id=runtime.owner, session_id=runtime.sid, job_id=root["job_id"])
    queue.resume_continuation(**main._turn_fence(row), active_child_job_id=child, deadline=time.time()+60)
    child_job = jobs.get_job(child)
    segments = ExecutionSegments(ExecutionIdentity(runtime.owner, runtime.sid, child_job["request_id"], child))
    segments.start()
    operation, _ = task_events.start_operation(child, runtime.owner, "fixture_write", {})
    jobs.mark_interrupted_jobs()
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    assert state(runtime, "first") == "reconcile_required"
    row = queue.find_turn_by_job(owner_user_id=runtime.owner, session_id=runtime.sid, job_id=child)
    response = runtime.client.post(runtime.path + "/first/reconcile")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "terminal" and response.json()["active_child_job_id"] == child
    assert jobs.get_job(root["job_id"])["status"] == "awaiting_input"
    assert jobs.get_job(child)["current_step"] == "reconciled" and segments.snapshot()["phase"] == "quarantined"
    current = queue.find_turn_by_job(owner_user_id=runtime.owner, session_id=runtime.sid, job_id=child)
    assert current["generation"] > row["generation"] and not queue.can_release_inputs(**main._turn_fence(current))
    with task_events.transaction() as conn:
        assert conn.execute("SELECT status FROM task_operations WHERE operation_id=?", (operation,)).fetchone()[0] == "interrupted"
    send(runtime, "last", "later task")
    wait_for(lambda: state(runtime, "last") == "terminal")
    assert runtime.started == ["first", "last"]


def test_consumer_scan_failure_degrades_readiness_then_recovers(monkeypatch):
    from types import SimpleNamespace

    failures = 0

    def once(*args, **kwargs):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise RuntimeError("injected queue scan failure")
        return {"turns": [], "has_more": False}

    async def scenario():
        health = {"last_scan": None, "consecutive_failures": 0}
        wake = asyncio.Event()
        monkeypatch.setattr(queue, "scan_open_turns", once)
        task = asyncio.create_task(main._consume_chat_turns(wake, health=health))
        # Compare the diagnostic to the actual coroutine rather than an old flag.
        state_view = SimpleNamespace(turn_queue_consumer=task, turn_queue_health=health)
        try:
            for _ in range(100):
                if health["consecutive_failures"]:
                    break
                await asyncio.sleep(0.005)
            assert main._turn_queue_health(state_view)["reason_code"] == "scan_retry"
            assert not task.done()
            for _ in range(150):
                if health["last_scan"] is not None:
                    break
                await asyncio.sleep(0.005)
            assert main._turn_queue_health(state_view)["ready"]
            assert main._turn_queue_health(state_view, now=health["last_scan"]+20)["reason_code"] == "scan_stalled"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert main._turn_queue_health(state_view)["reason_code"] == "consumer_stopped"

    asyncio.run(scenario())


def test_ready_requires_consumer_and_worker_is_only_diagnostic(monkeypatch):
    from src.code_execution import presence
    from types import SimpleNamespace

    snapshot = dict(stores=True, retrieval=True, models={"ready": True}, worker={"verified": True},
                    agent={"ready": True}, figure_skill={"prompt_loaded": True})
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.delenv("CITRUS_LOCAL_FULL_STARTUP", raising=False)
    monkeypatch.delenv("CITRUS_REQUIRE_FIGURE_WORKER", raising=False)
    attempts = []

    def failing_worker(_):
        attempts.append(True)
        raise RuntimeError("optional Worker store unavailable")

    monkeypatch.setattr(presence, "worker_available", failing_worker)
    healthy_task = SimpleNamespace(done=lambda: False)
    monkeypatch.setattr(main.app.state, "turn_queue_consumer", healthy_task, raising=False)
    healthy = asyncio.run(main._readiness_details(snapshot=snapshot, include_code_worker=False))
    assert healthy["ready"] and not attempts
    diagnostic = asyncio.run(main._readiness_details(snapshot=snapshot))
    assert diagnostic["ready"] and diagnostic["status"] == "degraded"
    assert diagnostic["code_worker"]["enabled"] and not diagnostic["code_worker"]["available"]
    healthy_task.done = lambda: True
    unhealthy = asyncio.run(main._readiness_details(snapshot=snapshot, include_code_worker=False))
    assert unhealthy["ready"] is False and unhealthy["turn_queue"]["reason_code"] == "consumer_stopped"


def test_startup_preserves_committed_done_and_retries_terminal_pin_release(runtime, monkeypatch):
    import time
    from src.agent.execution_segments import ExecutionSegments
    from src.agent.identity import ExecutionIdentity
    from src.api.turn_envelope import RESOURCE_KIND, turn_input_resource_id
    from src.core import task_events
    from src.core.turn_envelope import build_turn_envelope

    async def pause(*_):
        return

    release = files.release_resource_references
    attempts = []

    def injected(owner, kind, resource):
        attempts.append((owner, kind, resource))
        if len(attempts) < 3:
            raise RuntimeError("simulated File DB interruption")
        return release(owner, kind, resource)

    monkeypatch.setattr(main, "_dispatch_queued_turn", pause)
    monkeypatch.setattr(files, "release_resource_references", injected)
    turn = send(runtime, "first", "answer already delivered")
    claimed = queue.claim_head(owner_user_id=runtime.owner, session_id=runtime.sid)
    queue.mark_dispatch_intent(**main._turn_fence(claimed), current_step="init")
    job = jobs.get_job(turn["job_id"])
    identity = ExecutionIdentity(runtime.owner, runtime.sid, job["request_id"], job["job_id"])
    task_events.init_budget(identity.run_id, time.time()+60, max_calls=6)
    segments = ExecutionSegments(identity)
    claim = segments.start()
    segments.begin_finalize(claim)
    envelope = build_turn_envelope(job_status="completed", display_status="completed", history_saved=True)
    task_events.append_event(identity.run_id, "done", {"job_id": identity.run_id, "session_id": runtime.sid,
        "answer": "already delivered", "turn_status": "completed", "history_saved": True,
        "turn_envelope": envelope.as_dict()})
    jobs.mark_interrupted_jobs()
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    assert state(runtime, "first") == "terminal" and runtime.started == []
    assert segments.snapshot()["phase"] == "terminal" and jobs.get_job(identity.run_id)["status"] == "completed"
    pending = queue.pending_input_cleanup()
    assert {row["client_request_id"] for row in pending} == {"first"}
    runtime.client.portal.call(main._reconcile_turn_queue_startup)
    assert queue.pending_input_cleanup() == []
    assert attempts == [(runtime.owner, RESOURCE_KIND, turn_input_resource_id(runtime.sid, "first"))] * 3
    with task_events.transaction() as conn:
        events = conn.execute("SELECT event,data FROM task_events WHERE job_id=? AND event='done'",
                              (identity.run_id,)).fetchall()
        assert len(events) == 1 and "already delivered" in events[0]["data"]


def test_reconcile_parked_checkpoint_conflict_requires_terminal_worker(runtime, monkeypatch, tmp_path):
    import time
    from src.agent.execution_coordinator import ExecutionCompletionCoordinator
    from src.agent.execution_segments import ExecutionSegments
    from src.agent.identity import ExecutionIdentity
    from src.code_execution.service import ExecutionService
    from src.core import task_events

    async def pause(*_):
        return

    monkeypatch.setattr(main, "_dispatch_queued_turn", pause)
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "code_executions.sqlite3").touch()
    turn = send(runtime, "first", "blocked wait")
    claimed = queue.claim_head(owner_user_id=runtime.owner, session_id=runtime.sid)
    queue.mark_dispatch_intent(**main._turn_fence(claimed), current_step="init")
    job = jobs.get_job(turn["job_id"])
    identity = ExecutionIdentity(runtime.owner, runtime.sid, job["request_id"], job["job_id"])
    task_events.init_budget(identity.run_id, time.time()+60, max_calls=6)
    segments = ExecutionSegments(identity)
    claim = segments.start()
    segments.prepare_park(claim, checkpoint_ref="a"*32, wait_ids=("b"*32,))
    segments.release_park(claim)
    ExecutionCompletionCoordinator._blocked(identity, "code_checkpoint_reconciliation_required")
    assert state(runtime, "first") == "reconcile_required"
    seen = []

    def result(service):
        seen.append(service.identity)
        return [{"status": "running" if len(seen) == 1 else "failed"}]

    monkeypatch.setattr(ExecutionService, "run_executions", result)
    first = runtime.client.post(runtime.path + "/first/reconcile")
    assert first.status_code == 409 and first.json()["detail"]["code"] == "execution_wait_reconciliation_pending"
    assert state(runtime, "first") == "reconcile_required" and segments.snapshot()["phase"] == "parked"
    second = runtime.client.post(runtime.path + "/first/reconcile")
    assert second.status_code == 200, second.text
    assert state(runtime, "first") == "terminal" and segments.snapshot()["phase"] == "quarantined"
    assert seen == [identity, identity] and runtime.started == []
