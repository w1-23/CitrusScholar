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

    def test_flat_history_does_not_resurrect_legacy_narration(self):
        items = [{'role': 'user', 'content': 'Question', 'request_id': 'turn', 'message_id': 1},
                 {'role': 'assistant', 'content': 'Legacy fixed caption', 'progress': True,
                  'origin': 'host', 'request_id': 'turn', 'message_id': 2},
                 {'role': 'assistant', 'content': 'Saved answer', 'request_id': 'turn', 'message_id': 3}]
        [turn] = main.build_history_turns(items)
        self.assertEqual(turn['answer']['content'], 'Saved answer')
        self.assertNotIn('Legacy fixed caption', str(turn))
        [unfinished] = main.build_history_turns(items[:-1])
        self.assertIsNone(unfinished['answer'])
        self.assertNotIn('Legacy fixed caption', str(unfinished))

    def _native_progress(self, *, marker="a", text="核对两种实验方法", status="in_progress"):
        return {"job_id": self.claim.job_id, "origin": "langgraph", "source": "write_todos",
                "visibility": "public", "note_id": "todo:" + marker * 64, "status": status,
                **({"text": text} if status == "in_progress" else {})}

    def test_native_progress_is_durable_before_notification_and_deduplicated(self):
        from src.core import progress_bus
        job_id = self.claim.job_id
        queue = task_events.DurableEventQueue(job_id)
        notify = queue._notify
        frames = []

        def check_notification(frame):
            data = json.loads(frame['data'])
            self.assertEqual(task_events.public_working_notes(job_id)[-1]['data'], data)
            frames.append(data)
            notify(frame)

        active = self._native_progress()
        inactive = self._native_progress(marker="b", status="inactive")
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue), \
                patch.object(queue, '_notify', side_effect=check_notification):
            for payload in (active, active, inactive, inactive):
                progress_bus.emit_progress('assistant_progress', payload)
        self.assertEqual([row['status'] for row in frames], ['in_progress', 'inactive'])
        self.assertEqual([json.loads(queue.get_nowait()['data']) for _ in frames], frames)
        self.assertTrue(queue.empty())
        asyncio.run(self.sessions.save_messages(self.sid, [HumanMessage('Question'),
            AIMessage(content='Unidentified checkpoint narration', tool_calls=[{
                'id': 'fixture-tool', 'name': 'fixture_tool', 'args': {}}]), AIMessage('Saved answer')],
            idempotency_key='crid:original-client-request'))
        replay = self.client.get(f'/jobs/{job_id}/events').json()
        history = self.client.get(f'/sessions/{self.sid}/messages').json()
        for records in (history['turns'][0]['trace'], replay['events']):
            self.assertEqual([row['data'] for row in records if row.get('event') == 'assistant_progress'], frames)
        self.assertNotIn('Unidentified checkpoint narration', str(history) + str(replay))
        self.assertIn('Saved answer', str(history))

    def test_legacy_narrative_stays_stored_but_never_projects(self):
        job_id = self.claim.job_id
        with task_events.transaction() as conn:
            legacy = {"job_id": job_id, "origin": "host", "visibility": "public", "note_id": "legacy",
                      "category": "agent_model_wait", "text": "已向模型发送请求，正在等待首批响应。"}
            conn.execute('INSERT INTO task_events VALUES (?,?,?,?,?)',
                         (job_id, 1, 'assistant_progress', json.dumps(legacy), 1))
            conn.execute('UPDATE task_jobs SET progress_summary=? WHERE job_id=?', (legacy['text'], job_id))
        self.assertIsNone(task_events.append_event(job_id, 'assistant_progress', legacy))
        new = task_events.append_event(job_id, 'assistant_progress', self._native_progress())
        self.assertEqual(new['seq'], 2)
        replay = self.client.get(f'/jobs/{job_id}/events').json()
        self.assertEqual(replay['events'][0]['data'], {'seq': 1})
        self.assertEqual(replay['events'][1]['data'], new['data'])
        history = self.client.get(f'/sessions/{self.sid}/messages').json()
        self.assertEqual([row['data'] for row in history['turns'][0]['trace'] if row.get('event') == 'assistant_progress'],
                         [new['data']])
        status = self.client.get(f'/jobs/{job_id}').json()
        self.assertNotIn(legacy['text'], str(replay) + str(history) + str(status))
        self.assertEqual(jobs.get_job(job_id)['progress_summary'], legacy['text'])
        with task_events.read_transaction() as conn:
            self.assertEqual(json.loads(conn.execute(
                'SELECT data FROM task_events WHERE job_id=? AND seq=1', (job_id,)).fetchone()[0]), legacy)

    def test_rejected_progress_is_absent_from_queue_storage_replay_and_history(self):
        from src.core.progress_bus import _encode_event
        job_id = self.claim.job_id
        queue = task_events.DurableEventQueue(job_id)
        for override in ({'text': '<analysis>private fixture</analysis>'}, {'text': 'Bearer fixture-secret'},
                         {'text': '{"args": "private"}'}, {'text': '我认为先隐去原始信息'},
                         {'origin': 'model'}, {'job_id': 'wrong-job'}, {'visibility': 'private'},
                         {'status': 'inactive'}, {'reasoning_content': 'unpublished analysis'}, {'note_id': 'bad'}):
            payload = self._native_progress() | override
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


def test_pinned_framework_runtime_passes_startup_readiness():
    from src.api.main import _agent_runtime_readiness

    assert _agent_runtime_readiness() == {"ready": True, "errors": []}
