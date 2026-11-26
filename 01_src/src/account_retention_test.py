"""End-to-end retention with only synthetic, tmp_path-bound account stores."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "01_src" / "scripts" / "manage_account.py"
EXPORT = REPO / "04_scripts" / "export_8043_turns.py"
PASSWORD = "synthetic-passphrase"
SUBJECT = "SYNTHETIC_ERASURE_MARKER_701796"


def python(root, *args, expected=0):
    env = os.environ | {"CITRUSQA_PROJECT_ROOT": str(root), "CITRUSQA_ALLOW_EMPTY_DATA": "1",
                        "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, *map(str, args)], env=env, cwd=REPO,
                            text=True, encoding="utf-8", capture_output=True, timeout=40)
    assert result.returncode == expected, (result.stdout, result.stderr)
    return result


def run(root, code):
    return python(root, "-c", textwrap.dedent(code)).stdout.strip()


SETUP = '''
import asyncio
import sqlite3
from src import auth
from src.core import chat_turn_queue as queue, jobs, tool_results
from src.session import files, manager

auth.init_store()
manager = manager.SessionManager()

def session(sid, uid):
    asyncio.run(manager.get_or_create_session(sid, owner_user_id=uid))

def turn(sid, uid, key, query):
    return queue.enqueue_turn(owner_user_id=uid, session_id=sid, client_request_id=key,
                              bounded_payload={'query': query})['root_job_id']

def cancel(sid, uid, key):
    queue.cancel_queued(owner_user_id=uid, session_id=sid, client_request_id=key)
'''


def test_delete_scope_restart_and_export(tmp_path):
    root = tmp_path
    run(root, SETUP + f'''
old = auth.create_local_account('synthetic_subject', {PASSWORD!r})
other = auth.create_local_account('synthetic_neighbor', {PASSWORD!r})
session('session_old_00', old)
session('session_live00', other)
old_job = turn('session_old_00', old, 'shared_key', {SUBJECT!r})
other_job = turn('session_live00', other, 'shared_key', 'neighbor-visible')
extra_job = turn('unbound_old_00', old, 'orphan_key', {SUBJECT!r})
turn('unbound_queued', old, 'undispatched_key', {SUBJECT!r})
for sid, owner, key in [('session_old_00', old, 'shared_key'), ('session_live00', other, 'shared_key'),
                        ('unbound_old_00', old, 'orphan_key')]:
    cancel(sid, owner, key)

from sqlite3 import connect
import json
from pathlib import Path
with connect(jobs._DB_PATH) as db:
    db.row_factory = sqlite3.Row
    legacy = dict(db.execute('SELECT * FROM chat_turn_queue WHERE root_job_id=?', (extra_job,)).fetchone())
    (auth.PROJECT_ROOT / 'fixture.json').write_text(json.dumps([old, old_job, other, other_job, legacy]))
    db.execute('UPDATE chat_turn_queue SET terminal_evidence=? WHERE owner_user_id=?', ({SUBJECT!r}, old))

tool_results.ensure_schema()
with connect(jobs._DB_PATH) as db:
    db.executemany('INSERT INTO tool_results(tool_call_id,session_id,request_id,result_text) VALUES (?,?,?,?)', [
        ('scoped', 'session_old_00', 'not-a-job', {SUBJECT!r}),
        ('detached', 'unbound_old_00', extra_job, {SUBJECT!r}),
        ('neighbor', 'session_live00', other_job, 'neighbor-receipt')])
files.init_store()
with connect(files.DB_PATH) as db:
    db.execute("INSERT INTO session_file_resource_refs VALUES (?,'chat_turn_input','pin:orphan','orphan', 'hash',1)", (old,))
    db.execute("INSERT INTO session_file_resource_refs VALUES (?,'chat_turn_input','pin:neighbor','neighbor','hash',1)", (other,))
unindexed = files.FILES_ROOT / f'user_{{old}}' / 'unindexed'
unindexed.mkdir(parents=True, exist_ok=True)
(unindexed / 'private.txt').write_text({SUBJECT!r})

logs = auth.PROJECT_ROOT / 'logs'
logs.mkdir(exist_ok=True)
(logs / 'agent.log').write_text(f'now | ERROR | job={{other_job}} supplied historical prose: ' + {SUBJECT!r} + '\\n')
(logs / 'business.log').write_text(f'now | job={{other_job}} | total_ms=9 | request_id=shared_key | url=' + {SUBJECT!r} + '\\n')
(logs / 'prior-terminal.txt').write_text({SUBJECT!r})
before = {{p: p.read_bytes() for p in logs.iterdir() if p.is_file()}}
assert auth.delete_user_account(old, raise_if_busy=True)
assert auth.user_by_id(old) is None
assert {{p: p.read_bytes() for p in before}} == before
''')
    historical = {p: p.read_bytes() for p in root.joinpath("logs").glob("**/*") if p.is_file()}
    output = root / "synthetic-exports"
    result = python(root, EXPORT, "--today", "--runtime", root, "--out-dir", output)
    assert SUBJECT not in result.stdout + result.stderr
    text = next(output.glob("*.txt")).read_text(encoding="utf-8")
    assert SUBJECT not in text
    assert "neighbor-visible" not in text and '"total_ms": 9' in text
    assert "仅元数据" in text
    assert {p: p.read_bytes() for p in historical} == historical

    run(root, SETUP + f'''
import json
old, old_job, other, other_job, legacy = json.loads((auth.PROJECT_ROOT / 'fixture.json').read_text())
with sqlite3.connect(jobs._DB_PATH) as db:
    for table in ('chat_turn_queue', 'chat_turn_queue_seq', 'task_jobs'):
        assert db.execute(f'SELECT DISTINCT owner_user_id FROM {{table}}').fetchall() == [(other,)]
    assert db.execute('SELECT tool_call_id FROM tool_results').fetchall() == [('neighbor',)]
    # Simulate an orphan left by the older release, independent of the new purge.
    keys = ','.join(legacy)
    slots = ','.join('?' for _ in legacy)
    db.execute(f'INSERT INTO chat_turn_queue ({{keys}}) VALUES ({{slots}})', list(legacy.values()))
with sqlite3.connect(files.DB_PATH) as db:
    assert db.execute('SELECT owner_user_id FROM session_file_resource_refs').fetchall() == [(other,)]
assert not (files.FILES_ROOT / f'user_{{old}}').exists()
assert queue.get_turn(owner_user_id=old, session_id='session_old_00', client_request_id='shared_key') is None
new = auth.create_local_account('synthetic_subject', {PASSWORD!r})
assert new not in (old, other)
session('session_new_00', new)
new_job = turn('session_new_00', new, 'shared_key', 'new-owner-content')
assert new_job not in (old_job, other_job)
cancel('session_new_00', new, 'shared_key')
''')
    python(root, EXPORT, "--today", "--runtime", root, "--out-dir", output)
    text = next(output.glob("*.txt")).read_text(encoding="utf-8")
    assert all(marker not in text for marker in (SUBJECT, "neighbor-visible", "new-owner-content"))
    assert {p: p.read_bytes() for p in historical} == historical


def test_clear_blocks_pending_then_hides_only_old_history(tmp_path):
    run(tmp_path, SETUP + f'''
from src.session.manager import SessionHistoryBusy
owner = auth.create_local_account('synthetic_clear', {PASSWORD!r})
sid = 'session_clear_00'
session(sid, owner)
old_job = turn(sid, owner, 'old_key', {SUBJECT!r})
with sqlite3.connect(jobs._DB_PATH) as db:
    db.execute("INSERT INTO messages(session_id,msg_type,content) VALUES (?,'human',?)", (sid,{SUBJECT!r}))
    db.execute("UPDATE task_jobs SET status='cancel_requested' WHERE job_id=?", (old_job,))
try:
    manager._clear_session_sync(sid, owner)
    raise AssertionError('pending work was not protected')
except SessionHistoryBusy:
    pass
with sqlite3.connect(jobs._DB_PATH) as db:
    assert db.execute('SELECT content FROM messages').fetchall() == [({SUBJECT!r},)]
    db.execute("UPDATE task_jobs SET status='queued' WHERE job_id=?", (old_job,))
cancel(sid, owner, 'old_key')
manager._clear_session_sync(sid, owner)
assert jobs.get_job(old_job)['history_hidden'] == 1
assert jobs.list_for_session(sid, owner_user_id=owner, visible_only=True) == []
assert jobs.jobs_for_turns(sid, owner, ['old_key'], visible_only=True) == {{}}
assert jobs.sessions_with_jobs([sid], owner, visible_only=True) == set()
assert queue.list_turns(owner_user_id=owner, session_id=sid, visible_only=True) == []
assert queue.list_turns(owner_user_id=owner, session_id=sid)[0]['job_id'] == old_job
with sqlite3.connect(jobs._DB_PATH) as db:
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone() == (0,)
new_job = turn(sid, owner, 'new_key', 'new')
cancel(sid, owner, 'new_key')
assert [row['job_id'] for row in queue.list_turns(owner_user_id=owner, session_id=sid, visible_only=True)] == [new_job]
''')


def test_abandoned_deletion_requires_fresh_proof_and_records_resume(tmp_path):
    run(tmp_path, f'''
        import os
        from src import auth
        from src.core.account_admission import current_store
        auth.init_store()
        owner = auth.create_local_account('synthetic_claim_subject', {PASSWORD!r})
        assert owner == 1
        assert current_store().claim_deletion(owner) is not None
        os._exit(0)
    ''')
    run(tmp_path, '''
        from src import auth
        from src.core.account_admission import AccountDeletionPending
        auth.init_store()
        try:
            auth.delete_user_account(1, raise_if_busy=True)
            raise AssertionError('blind takeover was accepted')
        except AccountDeletionPending:
            pass
        assert auth.user_by_id(1)['active'] is False
    ''')
    pending = python(tmp_path, CLI, "deletion-status", "--check-pending", expected=1)
    assert "synthetic_claim_subject" not in pending.stdout
    status = python(tmp_path, CLI, "deletion-status", "--user-id", 1)
    snapshot = status.stdout.split()[-1]
    assert "synthetic_claim_subject" not in status.stdout
    args = [CLI, "resume-deletion", "--user-id", 1, "--incident", "OPS-701796"]
    python(tmp_path, *args, "--snapshot", snapshot, expected=2)
    python(tmp_path, *args, "--snapshot", "0" * 64, "--confirmed-writer-stopped", expected=2)
    assert status.stdout == python(tmp_path, CLI, "deletion-status", "--user-id", 1).stdout
    finished = python(tmp_path, *args, "--snapshot", snapshot, "--confirmed-writer-stopped")
    assert "synthetic_claim_subject" not in finished.stdout + finished.stderr
    python(tmp_path, CLI, "deletion-status", "--check-pending")
    run(tmp_path, '''
        from src import auth
        from contextlib import closing
        assert auth.user_by_id(1) is None
        with closing(auth._connect()) as db:
            rows = db.execute('SELECT event_type,metadata_safe_json FROM account_security_events ORDER BY id').fetchall()
        assert [row[0] for row in rows] == ['account_created_local', 'account_deletion_resumed_local',
                                           'account_deletion_completed']
        assert 'OPS-701796' in rows[1][1]
        assert 'claim_token' not in rows[1][1]
    ''')


def test_export_fails_closed_if_account_closes_before_publish(tmp_path):
    run(tmp_path, SETUP + f'''
import importlib.util
from pathlib import Path
from src.core.account_admission import current_store
spec = importlib.util.spec_from_file_location('offline_turn_export', Path({str(EXPORT)!r}))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
owner = auth.create_local_account('synthetic_publication', {PASSWORD!r})
sid = 'session_export_00'
session(sid, owner)
job = turn(sid, owner, 'export_key', {SUBJECT!r})
cancel(sid, owner, 'export_key')
output = auth.PROJECT_ROOT / 'test-output'
output.mkdir(mode=0o700)
original = output / 'previous.txt'
original.write_text('previous archive')
with sqlite3.connect(jobs._DB_PATH) as db:
    db.row_factory = sqlite3.Row
    selected = db.execute('SELECT * FROM chat_turn_queue WHERE root_job_id=?', (job,)).fetchall()
    assert current_store().close_admission(owner)
    try:
        exporter.save_export(db, auth.PROJECT_ROOT, selected, output, 'retention-', filename=original.name)
        raise AssertionError('stale scope was published')
    except exporter.ExportScopeChanged:
        pass
assert original.read_text() == 'previous archive'
assert set(output.iterdir()) == {{original}}
''')


def test_claim_operator_does_not_drop_admitted_work(tmp_path):
    run(tmp_path, f'''
        import os
        from src import auth
        from src.core.account_admission import current_store
        auth.init_store()
        owner = auth.create_local_account('synthetic_abandoned_work', {PASSWORD!r})
        store = current_store()
        store.acquire(owner)
        assert store.claim_deletion(owner) is not None
        os._exit(0)
    ''')
    status = python(tmp_path, CLI, "deletion-status", "--user-id", 1)
    snapshot = status.stdout.split()[-1]
    python(tmp_path, CLI, "resume-deletion", "--user-id", 1, "--incident", "OPS-701797",
           "--snapshot", snapshot, "--confirmed-writer-stopped", expected=2)
    assert status.stdout == python(tmp_path, CLI, "deletion-status", "--user-id", 1).stdout
    run(tmp_path, '''
        from src import auth
        from src.core.account_admission import current_store
        assert current_store().active_count(1) == 1
        assert not auth.user_by_id(1)['active']
    ''')


def test_legacy_audit_deleted_owner_hidden_before_count_and_exact_cleanup_retries(tmp_path):
    run(tmp_path, SETUP + f'''
from datetime import datetime, timezone
from src import admin_audit
from src.core.account_admission import current_store
from src.core import turn_logs
from pathlib import Path
import time
admin_audit.init_store()
old = auth.create_local_account('synthetic_audit_old', {PASSWORD!r})
neighbor = auth.create_local_account('synthetic_audit_other', {PASSWORD!r})
session('session_old_audit', old)
session('session_other_audit', neighbor)
job = turn('session_old_audit', old, 'same_key', {SUBJECT!r})
other = turn('session_other_audit', neighbor, 'same_key', 'neighbor query')
cancel('session_old_audit', old, 'same_key')
cancel('session_other_audit', neighbor, 'same_key')
with sqlite3.connect(jobs._DB_PATH) as db:
    finish, = db.execute('SELECT finished_at FROM chat_turn_queue WHERE root_job_id=?', (job,)).fetchone()
day = datetime.fromtimestamp(finish, timezone.utc).date().isoformat()
root = auth.PROJECT_ROOT / 'logs' / 'turns'
target = root / day / (job + '.txt')
other_target = root / day / (other + '.txt')
assert target.is_file() and other_target.is_file()
(target.parent / 'unrelated.txt').write_text('unrelated')
next_day = root / '1999-01-01'
next_day.mkdir()
(next_day / target.name).write_text('different exact date')
with sqlite3.connect(admin_audit.DB_PATH) as db:
    db.execute('INSERT INTO request_audit(request_id,user_id,username,session_id,query,answer,created_at) '
               'VALUES(?,?,?,?,?,?,?)', ('legacy_req', old, 'synthetic_audit_old', 'session_old_audit',
                                         {SUBJECT!r}, {SUBJECT!r}, time.time()))
    db.execute('INSERT INTO request_audit(request_id,user_id,username,session_id,query,created_at) '
               'VALUES(?,?,?,?,?,?)', ('orphan_req', 99999, 'gone', 'gone', {SUBJECT!r}, time.time()))
admin_audit.start_request(request_id='neighbor_req', user_id=neighbor, username='synthetic_audit_other',
    session_id='session_other_audit', query={SUBJECT!r}, attachments=[{{'name':{SUBJECT!r},'size_bytes':5}}])
admin_audit.finish_request(request_id='neighbor_req', answer={SUBJECT!r},
    retrieval_trace={{'query':{SUBJECT!r},'total_ms':9}})
assert admin_audit.list_requests()['total'] == 2
assert admin_audit.list_requests(query={SUBJECT!r})['total'] == 1
assert current_store().close_admission(old)
assert admin_audit.list_requests(query={SUBJECT!r})['total'] == 0
page = admin_audit.list_requests(limit=1)
assert page['total'] == 1 and len(page['items']) == 1
assert {SUBJECT!r} not in repr(page) and page['items'][0]['attachments'] == [{{'approx_bytes':5}}]
assert admin_audit.list_requests(offset=1)['items'] == []
# A non-file at the exact path must keep the account fenced and retryable.
target.unlink()
target.mkdir()
assert not auth.delete_user_account(old)
assert auth.user_by_id(old) is not None and not auth.user_by_id(old)['active']
with sqlite3.connect(jobs._DB_PATH) as db:
    assert db.execute('SELECT 1 FROM chat_turn_queue WHERE root_job_id=?', (job,)).fetchone()
target.rmdir()
target.write_text({SUBJECT!r})
assert auth.delete_user_account(old, raise_if_busy=True)
assert not target.exists() and other_target.is_file() and (next_day / target.name).is_file()
assert (target.parent / 'unrelated.txt').is_file()
with sqlite3.connect(admin_audit.DB_PATH) as db:
    assert db.execute('SELECT request_id FROM request_audit ORDER BY request_id').fetchall() == [('neighbor_req',), ('orphan_req',)]
''')


def test_export_correlates_exactly_and_requires_private_destination_for_full(tmp_path):
    python(tmp_path, EXPORT, '--today', '--full', '--runtime', tmp_path, expected=2)
    run(tmp_path, rf'''
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('offline_turn_export', {str(EXPORT)!r})
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
from src.logger import redact_log_text
assert redact_log_text('secret=example-value\nuser\x1b') == '[REDACTED] user '
root = Path({str(tmp_path)!r})
valid, wrong = '0123456789ab', 'abcabcabcabc'
(root / 'business.log').write_text(f'now | job={{valid}} | elapsed_ms=8 \n'
    f'now | job={{valid}}ff | elapsed_ms=99 \n'
    f'now | job={{valid}} | job={{wrong}} | elapsed_ms=88 \n')
rows, errors = exporter.correlated_logs(root, {{valid: valid}})
assert not errors and len(rows[valid]) == 1
assert '"elapsed_ms": 8' in next(iter(rows[valid]))
''')
