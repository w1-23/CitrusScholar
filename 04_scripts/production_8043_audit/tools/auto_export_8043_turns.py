#!/usr/bin/env python3
"""Export each paused-for-reconciliation or terminal 8043 turn to one TXT."""
import argparse
from contextlib import closing
from datetime import datetime
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import sys
from tempfile import NamedTemporaryFile
import time

from export_8043_turns import HEADER, JOB_ID, SHANGHAI, runtime_root, save_export


OUTPUT = Path.home() / "citrusscholar" / "log-exports" / "auto"
# Let the final request and diagnostics log entries flush before exporting.
LOG_GRACE_SECONDS = 5
STOPPED = ("reconcile_required", "terminal")


def write_cursor(path, position):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                            prefix=".cursor-", delete=False) as handle:
        pending = Path(handle.name)
        try:
            json.dump({"updated_cursor": position}, handle)
            handle.flush()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def load_cursor(path):
    if not path.exists():
        return time.time(), ""
    saved = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(saved, list):
        # The old cursor only covered finished_at and missed paused turns.
        start = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        return min(saved[0], start.timestamp()), ""
    timestamp, job_id = saved["updated_cursor"]
    if not (isinstance(timestamp, (int, float)) and math.isfinite(timestamp)
            and isinstance(job_id, str) and (not job_id or JOB_ID.fullmatch(job_id))):
        raise ValueError("invalid export cursor")
    return timestamp, job_id


def up_to_date(target, revision):
    try:
        with target.open(encoding="utf-8") as report:
            return report.readline().rstrip("\n") == HEADER and report.readline().rstrip("\n") == f"记录版本 {revision}"
    except FileNotFoundError:
        return False


def export_pending(root, directory):
    database = root / "state" / "sessions.db"
    uri = database.as_uri() + "?mode=ro"
    cursor_path = directory / ".cursor.json"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        accounts = root / "state" / "accounts.sqlite3"
        conn.execute("ATTACH DATABASE ? AS auth_source", (accounts.as_uri() + "?mode=ro",))
        previous = initial = load_cursor(cursor_path)
        pending = conn.execute(
            "SELECT updated_at,root_job_id FROM chat_turn_queue WHERE state IN (?,?) "
            "AND root_job_id!='' AND (updated_at,root_job_id)>(?,?) AND updated_at<=? "
            "ORDER BY updated_at,root_job_id",
            (*STOPPED, *initial, time.time() - LOG_GRACE_SECONDS),
        ).fetchall()

        failed = False
        for updated, job in pending:
            try:
                if not JOB_ID.fullmatch(job):
                    raise ValueError("invalid root job ID")
                target = directory / f"{job}.txt"
                conn.execute("BEGIN")
                try:
                    turn = conn.execute(
                        "SELECT q.* FROM chat_turn_queue q "
                        "JOIN auth_source.users u ON u.user_id=q.owner_user_id "
                        "JOIN auth_source.account_admission a ON a.owner=u.user_id "
                        "WHERE a.deleting=0 AND q.state IN (?,?) AND q.root_job_id=? AND q.updated_at=?",
                        (*STOPPED, job, updated),
                    ).fetchone()
                    if turn is not None:
                        revision = f"{turn['generation']}:{turn['state']}:{updated!r}"
                        if not up_to_date(target, revision):
                            save_export(conn, root, [turn], directory, f".{job}-",
                                        filename=target.name, revision=revision)
                finally:
                    conn.rollback()
            except Exception as error:
                print(f"TXT export failed: task={job!r}, error={type(error).__name__}", file=sys.stderr)
                failed = True
            if not failed:
                previous = (updated, job)
        if previous != initial or not cursor_path.exists():
            write_cursor(cursor_path, previous)
        return not failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, help="覆盖从 Web systemd 单元识别的运行目录")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    root = (args.runtime or runtime_root()).resolve(strict=True)
    args.out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.out_dir / ".lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return 0 if export_pending(root, args.out_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
