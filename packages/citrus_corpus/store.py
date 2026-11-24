"""Filesystem and SQLite boundaries for one independent corpus."""
from contextlib import closing, contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3

from .prepare import _sync_dir


_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, identity TEXT NOT NULL,
    request TEXT NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL,
    expected_sha256 TEXT, raw_sha256 TEXT, document_id TEXT, version_id TEXT,
    created REAL NOT NULL, updated REAL NOT NULL, not_before REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS intake_ready ON intake(state, not_before, created);
CREATE TABLE IF NOT EXISTS versions (
    seq INTEGER PRIMARY KEY, identity TEXT NOT NULL, document_id TEXT NOT NULL,
    version_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL, markdown_sha256 TEXT NOT NULL,
    chunk_count INTEGER NOT NULL, previous_version TEXT, source_format TEXT NOT NULL,
    rights TEXT NOT NULL, created REAL NOT NULL,
    UNIQUE(document_id, version_id), UNIQUE(identity, raw_sha256)
);
CREATE INDEX IF NOT EXISTS versions_latest ON versions(document_id, seq DESC);
CREATE TABLE IF NOT EXISTS rights_events (
    seq INTEGER PRIMARY KEY, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
    decision TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    seq INTEGER PRIMARY KEY, generation TEXT NOT NULL UNIQUE, selection TEXT NOT NULL,
    state TEXT NOT NULL, prior TEXT, at REAL NOT NULL, published REAL
);
CREATE TABLE IF NOT EXISTS publication_events (
    seq INTEGER PRIMARY KEY, generation TEXT, state TEXT NOT NULL, at REAL NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS corpus_state (
    id INTEGER PRIMARY KEY CHECK(id=1), last_build REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO corpus_state (id) VALUES (1);
"""


class Store:
    def __init__(self, root: str | Path):
        raw = Path(root).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise ValueError("corpus_root_invalid")
        self.root = raw.resolve()
        if self.root != raw or not self.root.is_dir():
            raise ValueError("corpus_root_invalid")
        self.db = self.root / "corpus.sqlite3"
        self.originals = self.root / "originals"
        self.documents = self.root / "documents"
        self.inbox = self.root / "inbox"
        self.lock = self.root / "writer.lock"
        if (not self.db.is_file() or self.db.is_symlink() or self.lock.is_symlink()
                or not self.lock.is_file()
                or any(not path.is_dir() or path.is_symlink()
                       for path in (self.originals, self.documents, self.inbox))):
            raise ValueError("corpus_root_uninitialized")
        with self.read() as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != 0x43534336:
                raise ValueError("corpus_database_invalid")

    @classmethod
    def initialize(cls, root: str | Path):
        path = Path(root).expanduser()
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("corpus_root_invalid")
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        if any(path.iterdir()):
            return cls(path)
        db = path / "corpus.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA application_id=1129530166")
            conn.executescript(_SCHEMA)
        db.chmod(0o600)
        for name in ("originals", "documents", "inbox"):
            (path / name).mkdir(mode=0o700)
        (path / "writer.lock").touch(mode=0o600, exist_ok=False)
        _sync_dir(path)
        return cls(path)

    @contextmanager
    def read(self):
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    @contextmanager
    def write(self, *, timeout=0.2):
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=rw", uri=True, timeout=timeout)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn

    @contextmanager
    def writer(self):
        """Admit a single worker without blocking a reader or submitter."""
        descriptor = os.open(self.lock, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("corpus_writer_busy") from exc
            yield
        finally:
            os.close(descriptor)
