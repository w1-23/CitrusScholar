"""Task observation with real routes and disposable SQLite state."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

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
        with patch.object(self.sessions, 'get_display_history', side_effect=OSError('internal detail')):
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
