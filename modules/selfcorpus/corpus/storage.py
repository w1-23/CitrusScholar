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
            elif version != 1:
                raise RuntimeError('Unsupported corpus database schema')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, write=False):
        with self.connection() as db:
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
