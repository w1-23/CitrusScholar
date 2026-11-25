#!/usr/bin/env python3
"""One systemd-user timer tick for an explicitly configured 8043-family Web unit.

Run with --unit and --port from the timer service. Only /live is a restart
signal. A failed /ready is a capability problem, not a Web restart condition.
"""
from __future__ import annotations

import argparse
import fcntl
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import NamedTemporaryFile
import time

GRACE_SEC = 120
PROBE_TIMEOUT_SEC = 5
FAILURE_LIMIT = 3
BACKOFF_INITIAL_SEC = 120
BACKOFF_MAX_SEC = 900
UNIT_NAME = re.compile(r"citrusscholar-[A-Za-z0-9_.@-]+-web\.service\Z")


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], check=True, text=True,
                          stdout=subprocess.PIPE, timeout=10).stdout


def active_since(unit):
    result = systemctl("show", "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic", unit)
    values = dict(line.split("=", 1) for line in result.splitlines() if "=" in line)
    if values.get("ActiveState") != "active":
        return None
    timestamp = int(values.get("ActiveEnterTimestampMonotonic", "0"))
    return timestamp / 1_000_000 if timestamp > 0 else None


def live(port):
    connection = HTTPConnection("127.0.0.1", port, timeout=PROBE_TIMEOUT_SEC)
    try:
        connection.request("GET", "/live", headers={"Connection": "close"})
        return connection.getresponse().status == 200
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def read_state(path):
    try:
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if (not isinstance(record, dict) or any(type(record.get(key)) not in (int, float)
                or not (0 <= record[key] < 10 ** 12) for key in ("activation", "retry_after"))
                or any(type(record.get(key)) is not int or not (0 <= record[key] <= BACKOFF_MAX_SEC)
                       for key in ("failures", "backoff"))):
            raise ValueError("invalid watchdog state")
        return record
    except (FileNotFoundError, ValueError):
        return {"activation": 0, "retry_after": 0, "failures": 0, "backoff": 0}


def write_state(path, record):
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream)
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def tick(unit, port, path):
    activation = active_since(unit)
    now = time.monotonic()
    record = read_state(path)
    if (activation or 0) != record["activation"]:
        record.update(activation=activation or 0, failures=0)
        write_state(path, record)
    if activation is None or now - activation < GRACE_SEC:
        return
    if live(port):
        if record["failures"] or record["backoff"]:
            record.update(failures=0, retry_after=0, backoff=0)
            write_state(path, record)
        return
    if now < record["retry_after"]:
        return
    record["failures"] += 1
    if record["failures"] < FAILURE_LIMIT:
        write_state(path, record)
        return
    # Do not act on failures from an invocation replaced while the probe ran.
    if active_since(unit) != activation:
        return
    delay = min(BACKOFF_MAX_SEC, max(BACKOFF_INITIAL_SEC, record["backoff"] * 2))
    record.update(failures=0, backoff=delay, retry_after=now + delay)
    # Persist even when systemd refuses the request; that must not cause a loop.
    write_state(path, record)
    print(f"{unit}: {FAILURE_LIMIT} /live failures; requesting restart (backoff {delay}s)", flush=True)
    systemctl("try-restart", "--no-block", unit)


def private_state_dir():
    root = Path(os.environ["XDG_RUNTIME_DIR"])
    if not root.is_absolute():
        raise ValueError("XDG_RUNTIME_DIR must be absolute")
    for directory in (root, root / "citrusscholar-watchdog"):
        if directory != root:
            directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("watchdog state must be in a private user runtime directory")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if not UNIT_NAME.fullmatch(args.unit) or not 1024 <= args.port <= 65535:
        parser.error("require a CitrusScholar Web user unit and its loopback HTTP port")
    state_dir = private_state_dir()
    state_path = state_dir / f"{args.unit}-{args.port}.json"
    lock_path = state_path.with_suffix(".lock")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600), "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        tick(args.unit, args.port, state_path)


if __name__ == "__main__":
    main()
