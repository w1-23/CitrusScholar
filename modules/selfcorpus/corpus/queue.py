import json
import time

from .errors import CorpusError
from .storage import digest, dumps, event, now, ref

IN_PROGRESS = ('fetching', 'fetched', 'parsing', 'parsed', 'chunking', 'embedding', 'indexing', 'validating')
# Internal envelope emitted only by the dedicated signed API route.
VERIFIED_SHARED_JOB = 'verified_text_shared'
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def receipt(db, row):
    return {'request_ref': row['ref'], 'operation_id': row['ref'], 'scope_ref': row['scope'],
            'status': row['status'], 'attempt': row['attempt'], 'created_at': row['created_at'],
            'updated_at': row['updated_at'], 'output': json.loads(row['result']),
            'error': json.loads(row['error']) if row['error'] else None,
            'events': [{'action': e['action'], 'at': e['at']} for e in db.execute(
                'SELECT action,at FROM events WHERE request_ref=? ORDER BY id', (row['ref'],))]}


def pending_intake_doi(db, scope, doi):
    phases = ('queued', *IN_PROGRESS)
    marks = ','.join('?' for _ in phases)
    return db.execute(f"""SELECT 1 FROM requests r JOIN events e ON e.request_ref=r.ref
                          WHERE r.scope=? AND r.status IN ({marks})
                            AND e.action='queued' AND json_extract(e.detail, '$.verified_doi')=? LIMIT 1""",
                      (scope, *phases, doi)).fetchone()


def enqueue(db, subject, scope, key, spec, *, check=None, detail=None):
    encoded = dumps(spec)
    fingerprint = digest(encoded)
    row = db.execute('SELECT * FROM requests WHERE owner=? AND scope=? AND idem=?', (subject, scope, key)).fetchone()
    if row and row['spec_hash'] == fingerprint:
        return receipt(db, row)
    if check is not None:
        check()
    if row:
        raise CorpusError('idempotency_conflict', 'Key already belongs to another input.', 409)
    operation, stamp = ref('req'), now()
    db.execute('''INSERT INTO requests(ref,owner,scope,idem,spec_hash,spec,status,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,'queued',?,?)''', (operation, subject, scope, key, fingerprint, encoded, stamp, stamp))
    event(db, scope, subject, 'queued', detail, operation)
    return receipt(db, db.execute('SELECT * FROM requests WHERE ref=?', (operation,)).fetchone())


class Queue:
    def __init__(self, storage):
        self.storage = storage

    def claim(self):
        stamp = time.time()
        with self.storage.transaction(write=True) as db:
            marks = ','.join('?' for _ in IN_PROGRESS)
            for expired in db.execute(f'''SELECT * FROM requests WHERE status IN ({marks})
                    AND lease_until<=? AND attempt>=?''', (*IN_PROGRESS, stamp, MAX_ATTEMPTS)).fetchall():
                finish(db, expired, 'unknown', error={'code':'lease_exhausted','message':'Worker lost its lease.','retryable':False})
            row = db.execute(f'''SELECT * FROM requests WHERE (status='queued' AND not_before<=?)
                    OR (status IN ({marks}) AND lease_until<=? AND attempt<?)
                    ORDER BY created_at,ref LIMIT 1''', (stamp, *IN_PROGRESS, stamp, MAX_ATTEMPTS)).fetchone()
            if row is None:
                return None
            lease = ref('lease')
            db.execute('''UPDATE requests SET status='fetching',attempt=attempt+1,lease=?,lease_until=?,
                          error=NULL,updated_at=? WHERE ref=?''', (lease, stamp+LEASE_SECONDS, now(), row['ref']))
            event(db, row['scope'], 'worker', 'fetching', {'attempt':row['attempt']+1}, row['ref'])
            return db.execute('SELECT * FROM requests WHERE ref=?', (row['ref'],)).fetchone()

    def heartbeat(self, job):
        with self.storage.transaction(write=True) as db:
            stamp = time.time()
            return db.execute('UPDATE requests SET lease_until=? WHERE ref=? AND lease=? AND lease_until>?',
                              (stamp+LEASE_SECONDS, job['ref'], job['lease'], stamp)).rowcount == 1

    def phase(self, job, status):
        with self.storage.transaction(write=True) as db:
            assert_lease(db, job)
            db.execute('UPDATE requests SET status=?,updated_at=? WHERE ref=?', (status, now(), job['ref']))
            event(db, job['scope'], 'worker', status, request_ref=job['ref'])

    def fail(self, job, exc):
        with self.storage.transaction(write=True) as db:
            assert_lease(db, job)
            retry = exc.retryable and job['attempt'] < MAX_ATTEMPTS
            state = 'queued' if retry else ('unavailable' if exc.status in {403,404,429,502,503,504} else 'failed')
            finish(db, job, state, error={'code':exc.code, 'message':str(exc), 'retryable':exc.retryable},
                   not_before=time.time()+2**job['attempt'] if retry else 0)


def assert_lease(db, job):
    if not db.execute('SELECT 1 FROM requests WHERE ref=? AND lease=? AND lease_until>?',
                      (job['ref'], job['lease'], time.time())).fetchone():
        raise CorpusError('lost_lease', 'Worker no longer owns this request.', 409)


def finish(db, job, state, result=None, error=None, not_before=0):
    db.execute('''UPDATE requests SET status=?,result=?,error=?,not_before=?,lease=NULL,lease_until=NULL,
                  updated_at=?,spec=CASE WHEN ?='queued' THEN spec ELSE '{}' END WHERE ref=?''',
               (state, dumps(result or {}), dumps(error) if error else None, not_before, now(), state, job['ref']))
    event(db, job['scope'], 'worker', state, {'error':error} if error else {}, job['ref'])
