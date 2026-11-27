"""Owner-scoped durable figures with atomic compare-and-swap revisions."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    sha TEXT NOT NULL, body BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS figures (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    asset TEXT REFERENCES assets(id) NOT NULL, head INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    figure TEXT REFERENCES figures(id) NOT NULL, rev INTEGER NOT NULL,
                    doc TEXT NOT NULL, overrides TEXT NOT NULL, preview BLOB NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(figure,rev)
                );
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, figure TEXT NOT NULL, rev INTEGER NOT NULL,
                    format TEXT NOT NULL, params TEXT NOT NULL, hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (figure,rev) REFERENCES revisions(figure,rev)
                );
                CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS owner_figures ON figures(owner,id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_asset(self, owner, name, body):
        ref = f"asset_{uuid4().hex}"
        with self.connect() as db:
            db.execute("INSERT INTO assets VALUES (?,?,?,?,?)", (ref, owner, name, digest(body), body))
        return ref

    def asset(self, owner, ref):
        with self.connect() as db:
            row = db.execute("SELECT id,name,sha,body FROM assets WHERE owner=? AND id=?", (owner, ref)).fetchone()
        if not row:
            raise Missing
        return row

    def create(self, owner, doc, preview):
        with self.connect() as db:
            db.execute("INSERT INTO figures VALUES (?,?,?,?,1)",
                       (doc.figure_id, owner, doc.name, doc.source.source_ref))
            self._insert(db, doc, {}, preview)

    @staticmethod
    def _insert(db, doc, overrides, preview):
        db.execute("INSERT INTO revisions(figure,rev,doc,overrides,preview) VALUES (?,?,?,?,?)",
                   (doc.figure_id, doc.revision, doc.model_dump_json(), json.dumps(overrides), preview))

    def save(self, owner, doc, overrides, preview, expected):
        with self.connect() as db:
            found = db.execute("UPDATE figures SET head=? WHERE id=? AND owner=? AND head=?",
                               (doc.revision, doc.figure_id, owner, expected))
            if found.rowcount != 1:
                raise Conflict
            self._insert(db, doc, overrides, preview)

    def revision(self, owner, ref, revision=None):
        with self.connect() as db:
            row = db.execute("""SELECT r.doc,r.overrides,r.preview,r.rev,f.asset
                FROM figures f JOIN revisions r ON r.figure=f.id AND r.rev=COALESCE(?,f.head)
                WHERE f.owner=? AND f.id=?""", (revision, owner, ref)).fetchone()
        if not row:
            raise Missing
        return row

    def figures(self, owner):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id AS figure_id,name,head AS revision FROM figures WHERE owner=? ORDER BY rowid DESC LIMIT 200", (owner,))]

    def history(self, owner, ref):
        with self.connect() as db:
            rows = list(db.execute("""SELECT r.rev AS revision,r.created_at FROM figures f
                JOIN revisions r ON r.figure=f.id WHERE f.owner=? AND f.id=? ORDER BY r.rev DESC LIMIT 200""",
                                   (owner, ref)))
        if not rows:
            raise Missing
        return [dict(r) for r in rows]

    def export_record(self, ref, rev, fmt, params, output):
        export_id = f"export_{uuid4().hex}"
        sha = digest(output)
        with self.connect() as db:
            db.execute("INSERT INTO exports(id,figure,rev,format,params,hash) VALUES (?,?,?,?,?,?)",
                       (export_id, ref, rev, fmt, json.dumps(params), sha))
        return export_id, sha

    def consume_ticket(self, ticket_id, current, expires):
        try:
            with self.connect() as db:
                db.execute("DELETE FROM tickets WHERE expires < ?", (current,))
                db.execute("INSERT INTO tickets VALUES (?,?)", (ticket_id, expires))
        except sqlite3.IntegrityError:
            raise Conflict from None
