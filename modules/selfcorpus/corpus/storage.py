from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(data):
    return sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def ref(prefix, *parts):
    return prefix + '_' + (digest(dumps(parts))[:40] if parts else uuid.uuid4().hex)


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                db.executescript((Path(__file__).parent/'migrations/001_initial.sql').read_text())
                version = 1
            if version != 1:
                raise RuntimeError('Unsupported corpus database schema')
            # The core schema number is a rollback compatibility boundary for 0.1.2/0.1.3.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='corpus_extensions' AND type='table'").fetchone():
                db.executescript((Path(__file__).parent/'migrations/002_evidence.sql').read_text())
            if not db.execute("SELECT 1 FROM corpus_extensions WHERE name='evidence_refs' AND version=1").fetchone():
                raise RuntimeError('Unsupported evidence extension schema')

    @contextmanager
    def connection(self, *, timeout=15):
        db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, write=False, timeout=15):
        with self.connection(timeout=timeout) as db:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def event(db, scope, actor, action, detail=None, request_ref=None):
    db.execute('INSERT INTO events(request_ref,scope,actor,action,detail,at) VALUES(?,?,?,?,?,?)',
               (request_ref, scope, actor, action, dumps(detail or {}), now()))
