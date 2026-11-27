"""Internal Worker entry point. No model-controlled commands/paths or host fallback."""
import hashlib
import json
import os
from pathlib import Path
import stat

from .benford import BENFORD_ID, adjust, assess
from .contract import MAX_FILE, MAX_REQUEST, REPORT_SCHEMA, decode_request, verified_source
from .evidence import normalize
from .scanner import PAPERCONAN_COMMIT, PINS, scan

GENG_COMMIT = "9610daeddf796a2e80b6feca40cc45987ba108c1"
INPUT_DIR = Path("/input/numeric-integrity")
RESULT = Path("/outputs/numeric-integrity/report.json")


def _read(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        entry = os.fstat(stream.fileno())
        if not stat.S_ISREG(entry.st_mode) or not 0 < entry.st_size <= limit:
            raise ValueError("invalid_snapshot_file")
        data = stream.read(limit + 1)
        if len(data) != entry.st_size:
            raise ValueError("snapshot_size_changed")
        return data


def run(inputs):
    request = _read(inputs / "request.json", MAX_REQUEST)
    sources, enable_benford = decode_request(request)
    bound = [(source, verified_source(_read(inputs / source.staged_name, MAX_FILE), source)) for source in sources]
    state, findings, coverage = normalize(scan(bound), sources)
    plugins = {BENFORD_ID: {"status": "not_requested", "commit": GENG_COMMIT, "columns": []}}
    if enable_benford:
        items = [item for source, data in bound if source.benford_columns for item in assess(source, data)]
        findings += adjust(items)
        count = sum(item["state"] == "executed" for item in items)
        plugin_status = "executed" if count and count == len(items) else "partial" if count else "not_applicable"
        plugins[BENFORD_ID].update(status=plugin_status, columns=items, method_version="geng-benford-adapt/1",
                                  adjustment="bonferroni", reason="" if items else "no_confirmed_csv_columns")
        if state == "complete" and plugin_status in {"partial", "not_applicable"}:
            state = "partial"
    return {"schema": REPORT_SCHEMA, "status": state, "request_sha256": hashlib.sha256(request).hexdigest(),
            "image_review": "not_in_scope", "sources": [source.reference for source in sources],
            "paperconan": {"commit": PAPERCONAN_COMMIT, "version": PINS["paperconan"], "coverage": coverage},
            "optional_detectors": plugins, "findings": findings, "signal_not_verdict": True}


def main():
    try:
        report = run(INPUT_DIR)
    except Exception:
        # Full diagnostics are not a public artifact. The host correlates the
        # execution receipt. A failed scanner never looks like a zero-hit scan.
        report = {"schema": REPORT_SCHEMA, "status": "failed", "image_review": "not_in_scope",
                  "limitations": ["numeric_worker_request_or_execution_failed"], "findings": [],
                  "signal_not_verdict": True}
    data = (json.dumps(report, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    RESULT.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(RESULT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


if __name__ == "__main__":
    main()
