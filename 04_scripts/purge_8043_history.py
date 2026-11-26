#!/usr/bin/env python3
"""Inspect or purge historical 8043 audit bodies and approved diagnostic TXT files."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time


_BODY = "COALESCE(query,'')<>'' OR COALESCE(answer,'')<>''"
_FIELDS = ("COALESCE(attachment_metadata_json,'[]')<>'[]' OR "
           "COALESCE(tool_trace_json,'[]')<>'[]' OR COALESCE(retrieval_trace_json,'{}')<>'{}'")
_RECEIPTS = "historical_audit_maintenance"
_TICKET = re.compile(r"[A-Z][A-Z0-9]{1,15}-[0-9]{1,16}")
_HASH = re.compile(r"[0-9a-f]{64}")
_SAFE_PATH = re.compile(r"[a-zA-Z0-9_.-]+")


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _regular(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.resolve(strict=True) != path or path.stat().st_nlink != 1:
        raise ValueError("maintenance database/manifest must be an unlinked regular path")
    return path


def _manifest(path):
    if path is None:
        return {}
    source = _regular(path.absolute())
    if source.stat().st_size > 512 * 1024:
        raise ValueError("archive manifest exceeds 512 KiB")
    items = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(items, list) or len(items) > 500:
        raise ValueError("archive manifest must contain at most 500 exact paths")
    entries = {}
    for row in items:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError("each manifest row requires path and sha256")
        name, digest = row["path"], row["sha256"]
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("invalid archive manifest row")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or relative.as_posix() != name or relative.suffix != ".txt"
                or any(part in {".", ".."} or not _SAFE_PATH.fullmatch(part) for part in relative.parts)
                or not _HASH.fullmatch(digest) or name in entries):
            raise ValueError("invalid or repeated archive path or hash")
        entries[name] = digest
    return entries


@contextmanager
def _archive_directory(root_fd, name):
    parts = PurePosixPath(name).parts
    with ExitStack() as stack:
        parent = root_fd
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
        yield parent, parts[-1]


@contextmanager
def _verified_archive(parent, name, digest):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        yield None
        return
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("archive entry must be a regular file with one link")
        if hashlib.file_digest(source, "sha256").hexdigest() != digest:
            raise ValueError("archive entry content hash changed")
        identity = _stat_identity(before)
        if identity != _stat_identity(os.fstat(fd)):
            raise ValueError("archive file changed while hashing")
        yield identity


def _stat_identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)


def _receipt(conn, incident, phase, scope, count):
    with conn:
        conn.execute(f"INSERT OR IGNORE INTO {_RECEIPTS} VALUES(?,?,?,?,?)",
                     (incident, phase, scope, count, time.time()))


def _has_intent(conn, incident, key):
    if not incident or not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_RECEIPTS,)).fetchone():
        return False
    return bool(conn.execute(f"SELECT 1 FROM {_RECEIPTS} WHERE incident=? AND phase='archive_intent' AND scope=?",
                             (incident, key)).fetchone())


def _archive_plan(conn, incident, root_fd, entries):
    plan = []
    root = os.fstat(root_fd)
    for name, digest in sorted(entries.items()):
        key = _digest([root.st_dev, root.st_ino, name, digest])
        with _archive_directory(root_fd, name) as (parent, basename):
            with _verified_archive(parent, basename, digest) as identity:
                pass
        if identity is None and not _has_intent(conn, incident, key):
            raise ValueError("archive entry missing without a matching removal receipt")
        plan.append((name, digest, key, identity))
    return plan


def _archive_apply(conn, incident, root_fd, plan):
    for name, digest, key, expected in plan:
        if expected is not None:
            with _archive_directory(root_fd, name) as (parent, basename):
                with _verified_archive(parent, basename, digest) as actual:
                    if actual != expected:
                        raise ValueError("archive identity changed after the maintenance plan")
                    _receipt(conn, incident, "archive_intent", key, 1)
                    if _stat_identity(os.stat(basename, dir_fd=parent, follow_symlinks=False)) != actual:
                        raise ValueError("archive entry changed before unlink")
                    os.unlink(basename, dir_fd=parent)
                    os.fsync(parent)
        _receipt(conn, incident, "archive_unlinked", key, 1)


def _truncated_wal(conn):
    busy, frames, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy or frames > 0:
        raise RuntimeError("WAL still in use, stop runtime readers and retry")


def _run(args):
    database = _regular(args.runtime.absolute() / "state" / "admin_audit.sqlite3")
    inode = database.stat()
    mode = "rw" if args.apply else "ro"
    entries = _manifest(args.manifest)
    with ExitStack() as stack:
        if args.apply:
            fd = os.open(database, os.O_RDWR | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            if (inode.st_dev, inode.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                raise ValueError("audit database identity changed")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn = stack.enter_context(closing(sqlite3.connect(database.as_uri() + "?mode=" + mode,
                                                           uri=True, timeout=0.5)))
        if args.apply:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        versioned = "metadata_version" in {row[1] for row in conn.execute("PRAGMA table_info(request_audit)")}
        legacy = f"metadata_version<>1 AND ({_FIELDS})" if versioned else _FIELDS
        rows = conn.execute(f"SELECT COUNT(*),COUNT(CASE WHEN {_BODY} THEN 1 END),"
                            f"COUNT(CASE WHEN {legacy} THEN 1 END) FROM request_audit").fetchone()
        # Bind approval to the exact selected row contents without emitting them.
        digest = hashlib.sha256()
        for row in conn.execute("SELECT request_id,user_id,query,answer,attachment_metadata_json,"
                                "tool_trace_json,retrieval_trace_json FROM request_audit "
                                f"WHERE ({_BODY}) OR ({legacy}) ORDER BY request_id"):
            digest.update(_digest([str(value) for value in row]).encode())
        root_fd, plan = None, []
        if args.archive_root is not None:
            root = args.archive_root.absolute()
            if root.resolve(strict=True) != root:
                raise ValueError("archive root must be a canonical existing directory")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, root_fd)
            plan = _archive_plan(conn, args.incident, root_fd, entries)
        summary = {"audit_rows": rows[0], "body_rows": rows[1], "legacy_metadata_rows": rows[2],
                   "archive_files": sum(identity is not None for _, _, _, identity in plan)}
        snapshot = _digest([inode.st_dev, inode.st_ino, rows, digest.hexdigest(),
                            [(key, identity) for _, _, key, identity in plan]])
        if args.apply:
            if snapshot != args.snapshot:
                raise ValueError("maintenance scope changed, inspect again")
            with conn:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {_RECEIPTS} (incident TEXT, phase TEXT, scope TEXT, "
                             "affected INTEGER, occurred_at REAL, PRIMARY KEY(incident,phase,scope))")
                conn.execute(f"UPDATE request_audit SET query='',answer='' WHERE {_BODY}")
                conn.execute("UPDATE request_audit SET attachment_metadata_json='[]',tool_trace_json='[]',"
                             f"retrieval_trace_json='{{}}' WHERE {legacy}")
                conn.execute(f"INSERT OR IGNORE INTO {_RECEIPTS} VALUES(?,?,?,?,?)",
                             (args.incident, "sql_scrubbed", snapshot, rows[1] + rows[2], time.time()))
            _truncated_wal(conn)
            conn.execute("VACUUM")
            _truncated_wal(conn)
            if (conn.execute(f"SELECT EXISTS(SELECT 1 FROM request_audit WHERE ({_BODY}) OR ({legacy}))").fetchone()[0]
                    or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
                raise RuntimeError("post-maintenance database verification failed")
            _archive_apply(conn, args.incident, root_fd, plan)
            _truncated_wal(conn)
            _receipt(conn, args.incident, "verified", snapshot, len(plan))
            _truncated_wal(conn)
        print(json.dumps({"mode": "applied" if args.apply else "inspect", "snapshot": snapshot, **summary},
                         sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--incident")
    parser.add_argument("--snapshot")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmed-writers-stopped", action="store_true")
    args = parser.parse_args()
    if bool(args.archive_root) != bool(args.manifest):
        parser.error("provide both --archive-root and --manifest to include archive files")
    if args.incident is not None and not _TICKET.fullmatch(args.incident):
        parser.error("invalid incident reference")
    if args.apply and (not args.confirmed_writers_stopped or not args.incident
                       or not _HASH.fullmatch(args.snapshot or "")):
        parser.error("apply requires exact --snapshot, authorized --incident, and --confirmed-writers-stopped")
    try:
        _run(args)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        # OS/SQLite exceptions can include protected paths or stored data.
        detail = str(exc) if type(exc) in {ValueError, RuntimeError} else type(exc).__name__
        parser.exit(2, f"maintenance stopped: {detail}\n")


if __name__ == "__main__":
    main()
