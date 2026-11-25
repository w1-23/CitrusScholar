"""Task observation with real routes and disposable SQLite state."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.api import main
from src.core import jobs, task_events
from src.session import manager


class TaskObservationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(jobs, '_DB_PATH', root / 'jobs.db'))
        self.enterContext(patch.object(manager, 'STATE_DIR', root))
        self.enterContext(patch.object(manager, 'DB_PATH', root / 'history.db'))
        self.enterContext(patch.object(manager.SessionManager, '_instance', None))
        self.sessions = manager.SessionManager()
        self.enterContext(patch.object(main, 'session_manager', self.sessions))
        self.owner = 111
        self.enterContext(patch.object(main, 'current_user', side_effect=lambda _: {'user_id': self.owner}))
        self.sid = 'observation-session'
        asyncio.run(self.sessions.get_or_create_session(self.sid, owner_user_id=self.owner))
        self.claim = jobs.claim_job(self.sid, 'original-server-request', owner_user_id=self.owner,
                                    client_request_id='original-client-request', payload={})
        app = FastAPI()
        app.add_api_route('/jobs/{job_id}', main.get_job_status)
        app.add_api_route('/jobs/{job_id}/events', main.get_job_events)
        app.add_api_route('/sessions/{session_id}/messages', main.session_messages)
        app.add_api_route('/sessions/{session_id}/task-by-request/{client_request_id}', main.discover_chat_job)
        self.client = self.enterContext(TestClient(app))

    def test_exact_request_discovery_by_owner_and_session(self):
        path = f'/sessions/{self.sid}/task-by-request/original-client-request'
        response = self.client.get(path)
        self.assertEqual(response.json(), {'job_id': self.claim.job_id})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get(path.replace(self.sid, 'other-session')).status_code, 404)
        self.assertEqual(self.client.get(path.replace('original-client-request', 'original-server-request')).status_code, 404)
        self.owner += 1
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(jobs.get_job(self.claim.job_id)['status'], 'running')

    def test_history_failure_still_discovers_tasks_and_signals_retry(self):
        with patch.object(self.sessions, 'get_display_history_page', side_effect=OSError('internal detail')):
            response = self.client.get(f'/sessions/{self.sid}/messages')
        self.assertEqual(response.status_code, 503, response.text)
        result = response.json()
        self.assertEqual(result['code'], 'history_unavailable')
        self.assertNotIn('messages', result)
        self.assertNotIn('internal detail', response.text)
        self.assertEqual([turn['job_id'] for turn in result['turns']], [self.claim.job_id])

    def test_historic_ids_are_aligned_even_with_a_synthetic_human_row(self):
        async def seed():
            for key, content in (('original-client-request', 'Question 1'), ('later-client', 'Question 2')):
                await self.sessions.save_messages(self.sid, [HumanMessage(content=content), AIMessage(content='Saved answer')],
                                                  idempotency_key='crid:' + key)
                if key == 'original-client-request':
                    with manager._connect_db(self.sessions.db_path) as conn:
                        conn.execute('INSERT INTO messages (session_id,msg_type,content) VALUES (?,?,?)',
                                     (self.sid, 'human', manager._SYNTH_FORCE_FINAL_MARK))
        asyncio.run(seed())
        msgs, row_ids, keys = asyncio.run(self.sessions.get_display_history(self.sid))
        self.assertEqual(len(msgs), len(row_ids))
        self.assertEqual(keys, ['original-client-request', '', 'later-client'])
        response = self.client.get(f'/sessions/{self.sid}/messages')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([turn['turn_request_id'] for turn in response.json()['turns']],
                         ['original-client-request', 'later-client'])

    def test_display_page_avoids_older_bodies_and_projects_only_proven_tool_status(self):
        old_secret = "synthetic older unprojected evidence " * 3000
        new_secret = "synthetic newest protected evidence " * 3000
        modern = ToolMessage(content=new_secret, tool_call_id="modern", name="read_tool_result",
            artifact={"_citrus_execution": {"status": "complete", "reason_code": "complete",
                        "completed_at_ms": 20, "elapsed_ms": 5, "result_id": "tr_" + "a" * 64}})
        legacy = ToolMessage(content="[ERR_TIMEOUT] synthetic legacy timeout", tool_call_id="legacy",
                             name="read_tool_result")

        async def save():
            for i in range(15):
                cid = f"old-{i}"
                await self.sessions.save_messages(self.sid, [HumanMessage(f"Question {i}"),
                    AIMessage(content="", tool_calls=[{"name": "read_tool_result", "id": cid, "args": {}}]),
                    ToolMessage(content=old_secret, name="read_tool_result", tool_call_id=cid),
                    AIMessage("Older answer")], idempotency_key=f"crid:old-{i}")
            await self.sessions.save_messages(self.sid, [HumanMessage("Newest question"),
                AIMessage(content="I am reading the selected evidence now.",
                    tool_calls=[{"name": "read_tool_result", "id": m.tool_call_id, "args": {}}
                                for m in (modern, legacy)]), modern, legacy,
                HumanMessage("A loop-only prompt", additional_kwargs={
                    "context_origin": "internal_control", "context_trusted": True}),
                AIMessage("Newest answer")], idempotency_key="crid:newest")

        asyncio.run(save())
        msgs, ids, keys = asyncio.run(self.sessions.get_display_history(self.sid))
        expected, expected_more, expected_cursor = main._slice_history(
            main._history_items_from_messages(msgs, ids, keys, {}), 1)
        read_tools = {}
        decode = self.sessions._decode_history_rows

        def observe_rows(rows, *args, **kwargs):
            materialized = list(rows)
            read_tools.update((row["tool_call_id"], row["content"]) for row in materialized if row["msg_type"] == "tool")
            return decode(materialized, *args, **kwargs)

        with patch.object(self.sessions, "_decode_history_rows", side_effect=observe_rows), \
                patch("src.session.files.list_turn_files", return_value={}) as attachments:
            response = self.client.get(f"/sessions/{self.sid}/messages", params={"limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        self.assertEqual((page["messages"], page["has_more"], page["next_before"]),
                         (expected, expected_more, expected_cursor))
        self.assertEqual(read_tools, {"modern": "", "legacy": legacy.content})
        attachments.assert_called_once_with(self.sid, self.owner, ["newest"])
        self.assertNotIn(new_secret, response.text)
        self.assertNotIn("A loop-only prompt", response.text)
        self.assertEqual(page["turns"][0]["answer"]["content"], "Newest answer")
        step = next(step for step in page["turns"][0]["trace"] if step.get("tool_call_id") == "modern")
        self.assertEqual(step["result_id"], "tr_" + "a" * 64)
        older = self.client.get(f"/sessions/{self.sid}/messages",
                                params={"limit": 1, "before": page["next_before"]}).json()
        self.assertEqual([t["turn_request_id"] for t in older["turns"]], ["old-14"])
        self.assertLess(max(m["message_id"] for m in older["messages"]), page["next_before"])

    def test_unkeyed_legacy_page_uses_real_user_boundary_and_exclusive_cursor(self):
        async def save():
            await self.sessions.save_messages(self.sid, [
                HumanMessage("Earlier legacy question"), AIMessage("Earlier legacy answer"),
                HumanMessage("Latest legacy question"),
                HumanMessage("上一轮回答因输出长度上限被截断，请重试。"),
                AIMessage("Latest legacy answer"),
            ])

        asyncio.run(save())
        newest = self.client.get(f"/sessions/{self.sid}/messages", params={"limit": 1}).json()
        self.assertEqual([m["content"] for m in newest["messages"]],
                         ["Latest legacy question", "Latest legacy answer"])
        self.assertTrue(newest["has_more"])
        older = self.client.get(f"/sessions/{self.sid}/messages", params={
            "limit": 1, "before": newest["next_before"]}).json()
        self.assertEqual([m["content"] for m in older["messages"]],
                         ["Earlier legacy question", "Earlier legacy answer"])
        self.assertFalse(older["has_more"])
        self.assertEqual(older["next_before"], 0)
        self.assertFalse(any(turn.get("history_pending") for turn in older["turns"]))

    def test_history_and_replay_use_the_same_public_note_identity(self):
        from src.core.progress_bus import PUBLIC_WORK_NOTES
        job_id = self.claim.job_id
        observed = task_events.append_event(job_id, "assistant_progress", {
            "job_id": job_id, "visibility": "public", "origin": "host", "category": "table_processed",
            "note_id": "fixture-progress", "text": "private fixture input"})
        task_events.append_event(job_id, "text_reset", {"job_id": job_id})
        live = self.client.get(f"/jobs/{job_id}/events").json()
        saved = self.client.get(f"/sessions/{self.sid}/messages").json()
        rows = [item for item in saved["turns"][0]["trace"] if item.get("event") == "assistant_progress"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seq"], observed["seq"])
        self.assertEqual(rows[0]["data"]["note_id"], "fixture-progress")
        self.assertEqual(rows[0]["data"]["text"], PUBLIC_WORK_NOTES["table_processed"])
        self.assertIn("fixture-progress", str(live))
        self.assertNotIn("private fixture input", str(live) + str(saved))

    def test_only_previously_published_notes_survive_history_and_replay(self):
        from src.core import progress_bus
        from src.core.public_progress import emit_public_progress, reset_emitted

        job_id = self.claim.job_id
        public_note = "我已识别两块数据，接下来用第二块绘图。"
        queue = task_events.DurableEventQueue(job_id)
        reset_emitted()
        self.addCleanup(reset_emitted)
        notify = queue._notify

        def check_notification(frame):
            data = json.loads(frame['data'])
            stored = task_events.public_working_notes(job_id)
            self.assertEqual([(row['seq'], row['data']) for row in stored], [(int(frame['id']), data)])
            self.assertEqual(jobs.get_job(job_id)['progress_summary'], data['text'])
            notify(frame)

        with patch.object(progress_bus, 'get_progress_queue', return_value=queue), \
                patch.object(queue, '_notify', side_effect=check_notification):
            # New emitters cannot publish model text. A previously persisted
            # public model event still projects consistently in old history.
            self.assertFalse(emit_public_progress(public_note, note_id="model-note", job_id='caller-claimed-id'))
            queue.put_nowait(progress_bus._encode_event('assistant_progress', {
                'origin': 'model', 'visibility': 'public', 'text': public_note,
                'note_id': 'model-note', 'job_id': 'caller-claimed-id'}))
        frame = queue.get_nowait()
        asyncio.run(self.sessions.save_messages(self.sid, [HumanMessage(content='Question'),
            AIMessage(content='Unidentified checkpoint narration', tool_calls=[{
                'id': 'fixture-tool', 'name': 'fixture_tool', 'args': {}}]), AIMessage(content='Saved answer')],
            idempotency_key='crid:original-client-request'))
        replay = self.client.get(f'/jobs/{job_id}/events').json()
        history = self.client.get(f'/sessions/{self.sid}/messages').json()
        saved = next(row for row in history['turns'][0]['trace'] if row.get('event') == 'assistant_progress')
        replayed = next(row for row in replay['events'] if row.get('event') == 'assistant_progress')
        self.assertEqual(saved['seq'], int(frame['id']))
        self.assertEqual(saved['data'], replayed['data'])
        self.assertEqual(saved['data'], json.loads(frame['data']))
        self.assertEqual(saved['data']['job_id'], job_id)
        self.assertEqual(saved['data']['note_id'], 'model-note')
        self.assertNotIn('Unidentified checkpoint narration', str(history))
        self.assertIn('Saved answer', str(history))
        self.assertNotIn('Unidentified checkpoint narration', str(replay))
        self.assertNotIn('private fixture', str(replay) + str(history))

    def test_verified_milestones_are_idempotent_across_reconnect_and_history(self):
        from src.core import progress_bus
        from src.core.public_progress import emit_public_progress, emit_retrieval_stage, reset_emitted

        job_id = self.claim.job_id
        queue = task_events.DurableEventQueue(job_id)
        self.addCleanup(reset_emitted)
        categories = ['agent_model_wait', 'retrieval_full_text_read', 'agent_review_evidence', 'agent_review_answer']
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue):
            self.assertTrue(emit_public_progress('provider draft fixture', category=categories[0], step_id='attempt-1',
                                                 note_id='attempt-1:wait', job_id=job_id))
            emit_retrieval_stage('full_text_read')
            emit_retrieval_stage('private_fixture_page')
            for category in categories[2:]:
                self.assertTrue(emit_public_progress('private tool fixture', category=category,
                                                     note_id=category, job_id=job_id))
            reset_emitted()  # Simulate a separate task context after reconnect.
            emit_retrieval_stage('full_text_read')
        frames = [json.loads(queue.get_nowait()['data']) for _ in categories]
        self.assertTrue(queue.empty())
        replay = self.client.get(f'/jobs/{job_id}/events').json()
        history = self.client.get(f'/sessions/{self.sid}/messages').json()
        replay_notes = [entry['data'] for entry in replay['events'] if entry['event'] == 'assistant_progress']
        history_notes = [entry['data'] for entry in history['turns'][0]['trace'] if entry.get('event') == 'assistant_progress']
        self.assertEqual(frames, replay_notes)
        self.assertEqual(frames, history_notes)
        self.assertEqual([frame['category'] for frame in frames], categories)
        assert all(frame['text'] == progress_bus.PUBLIC_WORK_NOTES[frame['category']] for frame in frames)
        self.assertNotIn('fixture', str(replay_notes) + str(history_notes))

    def test_rejected_progress_is_absent_from_queue_storage_replay_and_history(self):
        from src.core.progress_bus import _encode_event
        job_id = self.claim.job_id
        queue = task_events.DurableEventQueue(job_id)
        for text in ('<analysis>private fixture</analysis>', 'Bearer fixture-secret', '{"args": "private"}'):
            payload = {'origin': 'model', 'category': 'table_processed', 'text': text}
            self.assertIsNone(task_events.append_event(job_id, 'assistant_progress', payload))
            queue.put_nowait(_encode_event('assistant_progress', payload))
        self.assertTrue(queue.empty())
        self.assertEqual(task_events.public_working_notes(job_id), [])
        self.assertEqual(self.client.get(f'/jobs/{job_id}/events').json()['events'], [])
        history = self.client.get(f'/sessions/{self.sid}/messages').json()
        self.assertNotIn('assistant_progress', str(history))

    def test_terminal_snapshot_and_event_cursor_share_a_view(self):
        job_id = self.claim.job_id
        initial = task_events.append_event(job_id, 'job', {'job_id': job_id, 'status': 'running'})
        page = self.client.get(f'/jobs/{job_id}/events').json()
        self.assertEqual(page['next_cursor'], initial['seq'])
        done = task_events.append_event(job_id, 'done', {'job_id': job_id, 'answer': 'finished',
                                                        'turn_envelope': {'display_status': 'completed'}})
        jobs.update_job(job_id, status='completed')
        status = self.client.get(f'/jobs/{job_id}')
        self.assertEqual(status.headers['Cache-Control'], 'no-store')
        self.assertEqual(status.json()['event_cursor'], done['seq'])
        self.assertEqual(status.json()['terminal_snapshot']['display_status'], 'completed')

    def test_update_request_is_idempotent_and_finalization_fences_the_version(self):
        from src.agent.identity import ExecutionIdentity

        identity = ExecutionIdentity(self.owner, self.sid, 'original-server-request', self.claim.job_id)
        job_id = identity.run_id
        self.client.app.add_api_route('/jobs/{job_id}/updates', main.update_chat_task, methods=['POST'])
        self.client.app.add_api_route('/jobs/{job_id}/updates/{update_request_id}', main.chat_task_update_status)
        route = f'/jobs/{job_id}/updates'
        payload = {'expected_version': 0, 'instruction': 'Add PDF', 'update_request_id': 'update-1'}
        first = self.client.post(route, json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(task_events.fence_final_answer(identity, 0))
        self.assertTrue(task_events.fence_final_answer(identity, 1))
        self.assertEqual(self.client.post(route, json=payload).json(), first.json())
        self.assertEqual(self.client.post(route, json={**payload, 'instruction': 'Add XLSX'}).status_code, 409)
        self.assertEqual(self.client.post(route, json={**payload, 'expected_version': 1,
                                                       'update_request_id': 'update-2'}).status_code, 409)
        self.assertEqual(self.client.get(f'{route}/update-1').json(), first.json())
        self.assertEqual(self.client.get(f'{route}/update-2').status_code, 404)
        self.owner += 1
        self.assertEqual(self.client.get(f'{route}/update-1').status_code, 404)


def test_model_draft_is_not_a_public_answer(monkeypatch):
    from src.core import progress_bus
    from src.core.public_progress import PublicAnswerPreview

    events = []
    monkeypatch.setattr(progress_bus, "emit_answer_start", lambda: events.append("answer_start"))
    monkeypatch.setattr(progress_bus, "emit_text", lambda value: events.append(value))
    monkeypatch.setattr(progress_bus, "emit_text_reset", lambda: events.append("text_reset"))
    preview = PublicAnswerPreview()
    preview.append("This is a safe plain-text preview of the visible answer. ")
    preview.append("This remains in the final answer stream.")
    preview.clear()
    assert events == []
    for text in ('{"answer": "This is a private completion envelope. "}',
                 '<think>This is privately authored reasoning material.</think> ',
                 'One should never disclose a Bearer example-private-token. '):
        preview = PublicAnswerPreview()
        preview.append(text)
        preview.clear()
        assert events == []


def test_pinned_framework_runtime_passes_startup_readiness():
    from src.api.main import _agent_runtime_readiness

    assert _agent_runtime_readiness() == {"ready": True, "errors": []}
