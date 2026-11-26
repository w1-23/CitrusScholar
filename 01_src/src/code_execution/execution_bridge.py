"""Native ``execute`` over the existing offline Code Worker; never a host shell.

The Deep Agents ``execute`` tool needs a ``SandboxBackendProtocol``, but this
product already owns a bounded offline execution path: ``ExecutionService``
turns one snapshot of already-bound workspace files into a queued Worker run and
stores bounded stdout/stderr. This module bridges the two without adding a
second execution path, a second store or any host process:

- the command travels as JSON data inside a generated Python wrapper, so no
  string concatenation reaches a shell;
- the wrapper runs ``/bin/sh -c`` *inside* the Worker sandbox and reports the
  real command status through one deterministic marker line, because the Worker
  only accepts a zero script exit (a non-zero exit discards the outputs);
- new or changed files under ``/workspace`` and ``/outputs`` travel back in one
  declared JSON manifest, which is verified byte-for-byte and then written into
  the existing versioned session Workspace through a caller-supplied view;
- when the Worker is absent, or offline execution is disabled, the call returns
  a not-run error. There is no host subprocess/Python fallback in this module.

The module imports no Deep Agents symbol and no session-storage class, so it is
testable without the SDK graph and cannot become a second persistence layer.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
import re
import time
import uuid

from .native_execute_wrapper import (
    MANIFEST_SCHEMA, MARKER, MAX_RETURNED_FILES, MAX_TRANSFER_BYTES,
    MAX_TRANSFER_FILE_BYTES, build_wrapper, parse_marker, strip_marker,
)
from .protocol import ExecutionLimits, TERMINAL, logical_path

__all__ = ["ExecuteResult", "MAX_COMMAND_BYTES", "build_wrapper", "effective_timeout",
           "enabled", "execute", "identify", "parse_marker", "sandbox_ready"]

MAX_COMMAND_BYTES = 8 * 1024
DEFAULT_TIMEOUT_SEC = 120
MIN_TIMEOUT_SEC = 5
MAX_TIMEOUT_SEC = 900
# The Worker needs room for collection/acceptance inside its own row deadline,
# so the sandbox budget stays apart from the caller's command timeout.
ACCEPTANCE_HEADROOM_SEC = 30
CANCEL_GRACE_SEC = 15
POLL_INTERVAL_SEC = 0.5
MAX_STREAM_BYTES = 64 * 1024
MAX_PAGE_BYTES = 16384
MAX_TAIL_BYTES = 4096
MAX_MANIFEST_BYTES = 2 * MAX_TRANSFER_BYTES + 65536
MAX_BOUND_FILES = 48
MAX_BOUND_INPUTS = 24
MAX_SCRIPT_BYTES = 190000
MANIFEST_ROOT = "/outputs"
RUN_ROOT = "/workspace/code_runs"
_REASON = re.compile(r"[A-Za-z0-9_.: -]{1,72}")

# The view is the small, duck-typed seam to the session Workspace storage:
#   view.refs() -> {logical path: {"kind", "file_id", "version", "sha256"}}
#   view.write_back(path, data, *, expected_version, expected_sha256)
#                  -> {"version": int, "sha256": str} or raises ValueError(reason)
# It keeps this module free of storage imports and lets tests use a fake.


@dataclass(frozen=True)
class ExecuteResult:
    """One native execute outcome, independent of the SDK response type."""

    output: str
    exit_code: int | None
    truncated: bool = False


def _reason(exc):
    """Bounded, model-safe reason text: never a host path or a raw traceback."""
    if isinstance(exc, ValueError) and _REASON.fullmatch(str(exc)):
        return str(exc)
    return type(exc).__name__


def _text(reason, detail=""):
    return (f"Error: execute not run ({reason}). "
            "Commands run only inside the isolated offline Code Worker; there is no host "
            "shell fallback." + (f" {detail}" if detail else ""))


def enabled():
    """A pinned production Worker or explicit offline developer configuration."""
    release = os.getenv("CITRUS_WORKER_RELEASE", "")
    # The sealed launcher supplies the release digest. Presence in this mode
    # must match it, the same readiness fence used by the Web service.
    return (re.fullmatch(r"[a-f0-9]{64}", release) is not None
            or os.getenv("CITRUS_CODE_EXECUTION") == "offline-development")


def sandbox_ready():
    """True only when offline execution is enabled and a Worker is present.

    A hint for graph construction and diagnostics, never an execution lease:
    ``execute`` re-checks availability before it submits anything.
    """
    if not enabled():
        return False
    from src.config import PROJECT_ROOT

    from .presence import worker_available

    return worker_available(PROJECT_ROOT / "state" / "code_executions.sqlite3")


def identify(owner, session, run_id):
    """Rebuild the exact execution identity from the authenticated run record.

    A message, tool name or model argument never supplies identity. The turn id
    is read back from the durable job row so the Worker, its budget and the
    workspace retention barrier all share one authorization boundary.
    """
    from src.core import jobs

    job = jobs.get_job(run_id) if isinstance(run_id, str) and run_id else None
    if (not isinstance(job, dict) or job.get("owner_user_id") != owner
            or job.get("session_id") != session or not job.get("request_id")):
        raise LookupError("execution_scope_unavailable")
    if job.get("status") not in {"running", "detached"}:
        raise LookupError("execution_scope_unavailable")
    from src.agent.identity import ExecutionIdentity

    return ExecutionIdentity(owner, session, str(job["request_id"]), run_id)


def run_deadline(identity):
    """Observe the run's stored wall deadline; never create or extend it."""
    from src.core import task_events

    with task_events.transaction() as conn:
        row = conn.execute("SELECT deadline FROM task_budgets WHERE job_id=?",
                           (identity.run_id,)).fetchone()
    if row is None or type(row[0]) not in (int, float):
        return None
    deadline = float(row[0])
    if deadline <= 0:
        return None
    return deadline


def effective_timeout(requested, remaining=None):
    """Bound one command by the caller, the product cap and the run deadline."""
    if requested is None:
        value = DEFAULT_TIMEOUT_SEC
    elif type(requested) is bool or not isinstance(requested, int) or requested < 0:
        raise ValueError("invalid_execute_timeout")
    else:
        value = requested or DEFAULT_TIMEOUT_SEC
    value = max(MIN_TIMEOUT_SEC, min(int(value), MAX_TIMEOUT_SEC))
    if remaining is not None:
        usable = int(remaining) - ACCEPTANCE_HEADROOM_SEC
        if usable < MIN_TIMEOUT_SEC:
            raise ValueError("execute_run_deadline_exhausted")
        value = min(value, usable)
    return max(MIN_TIMEOUT_SEC, value)


class _Logs:
    """Bounded paging over the Worker's captured stdout/stderr bytes."""

    def __init__(self, service, execution_id, fence):
        self._service = service
        self._execution_id = execution_id
        self._fence = fence

    def _page(self, stream, offset, limit):
        return self._service.read_log(self._execution_id, fence=self._fence,
                                      stream=stream, offset=offset, limit=limit)

    def read(self, stream, *, cap=MAX_STREAM_BYTES):
        chunks, offset, size = [], 0, 0
        while True:
            page = self._page(stream, offset, MAX_PAGE_BYTES)
            size = int(page.get("size_bytes") or size or 0)
            try:
                chunk = base64.b64decode(page.get("data_base64") or "", validate=True)
            except (ValueError, TypeError):
                chunk = (page.get("text_preview") or "").encode("utf-8", "replace")
            chunks.append(chunk)
            offset = page.get("next_offset")
            if page.get("eof") is True or not isinstance(offset, int) or offset <= 0:
                break
            if sum(len(item) for item in chunks) >= cap:
                break
        collected = b"".join(chunks)
        return collected[:cap], max(size, len(collected))

    def tail(self, stream, size, *, limit=MAX_TAIL_BYTES):
        offset = max(0, int(size) - limit)
        page = self._page(stream, offset, min(limit, 65536))
        try:
            return base64.b64decode(page.get("data_base64") or "", validate=True)
        except (ValueError, TypeError):
            return (page.get("text_preview") or "").encode("utf-8", "replace")


def _decode(data, size):
    text = data.decode("utf-8", errors="replace")
    if size > len(data):
        text += f"\n[... {size - len(data)} more bytes not shown]"
    return text


# -- binding the session view into the run's code workspace ------------------


def _bind_all(workspace, workspace_id, revision, paths, refs, *, mode):
    """Bind only changed session files; one CAS revision per new binding."""
    bound, skipped = [], []
    for path in paths:
        ref = refs[path]
        if len(bound) >= MAX_BOUND_FILES:
            skipped.append(path)
            continue
        done = False
        for attempt in (0, 1):
            try:
                result = workspace.bind(workspace_id, path=path, file_id=ref["file_id"],
                                        expected_revision=revision,
                                        expected_sha256=ref["sha256"],
                                        source={"tool": "native_execute", "mode": mode})
            except Exception:
                if attempt:
                    break
                # Another caller of this run may have bumped the revision; the
                # immutable file reference is unchanged, so re-observe and retry.
                try:
                    revision = workspace.open(workspace_id)["revision"]
                except Exception:
                    break
                continue
            revision = result["revision"]
            bound.append(path)
            done = True
            break
        if not done:
            skipped.append(path)
    return revision, bound, skipped


def _current_bindings(workspace, workspace_id):
    """Map the code workspace's current logical files to their pinned identity."""
    current, offset = {}, 0
    while True:
        page = workspace.list_files(workspace_id, offset=offset, limit=100)
        for item in page["items"]:
            current[item["path"]] = (item["file_id"], item["sha256"])
        offset = page["next_offset"]
        if offset is None:
            return current


def _register_script(identity, path, source):
    from src.session import files

    return files.register_file(owner_user_id=identity.owner_user_id,
                               session_id=identity.session_id,
                               filename=path.rsplit("/", 1)[-1],
                               content=source.encode("utf-8"), mime="text/plain",
                               visibility="internal", producer_tool="native_execute")


def _prepare(identity, service, view, command, timeout, nonce, run_token):
    """Bind the run's session files, add the wrapper and return the snapshot."""
    workspace = service.workspace
    key = "code_" + hashlib.sha256(identity.run_id.encode("utf-8")).hexdigest()
    try:
        manifest = workspace.create(task_id=key, request_id=key)
    except ValueError:
        manifest = workspace.open()
        if manifest["task_id"] != key:
            raise ValueError("execute_workspace_unavailable")
    workspace_id, task_id = manifest["workspace_id"], manifest["task_id"]
    revision = manifest["revision"]

    refs = view.refs()
    writable = sorted(path for path in refs
                      if path.startswith(("/workspace/", "/outputs/"))
                      and refs[path].get("kind") == "workspace_file")
    inputs = sorted(path for path in refs if path.startswith("/input/"))[:MAX_BOUND_INPUTS]
    current = _current_bindings(workspace, workspace_id)

    pending = [path for path in writable
               if current.get(path) != (refs[path]["file_id"], refs[path]["sha256"])]
    revision, bound, skipped = _bind_all(workspace, workspace_id, revision,
                                         pending[:MAX_BOUND_FILES], refs, mode="workspace")
    inputs = [path for path in inputs
              if current.get(path) != (refs[path]["file_id"], refs[path]["sha256"])]
    revision, bound_inputs, skipped_inputs = _bind_all(
        workspace, workspace_id, revision, inputs, refs, mode="input")

    baseline = {path: refs[path]["sha256"] for path in bound + bound_inputs}
    script_path = f"{RUN_ROOT}/exec_{run_token}.py"
    manifest_path = f"{MANIFEST_ROOT}/code_run_{run_token}.json"
    source = build_wrapper(command, timeout=timeout, nonce=nonce, manifest=manifest_path,
                           baseline=baseline, skip=[script_path, manifest_path])
    if len(source.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ValueError("execute_command_too_large")
    record = _register_script(identity, script_path, source)
    result = workspace.bind(workspace_id, path=script_path, file_id=record["file_id"],
                            expected_revision=revision, expected_sha256=record["sha256"],
                            source={"tool": "native_execute", "mode": "wrapper"})
    return {
        "workspace_id": workspace_id,
        "task_id": task_id,
        "revision": result["revision"],
        "script_path": script_path,
        "manifest_path": manifest_path,
        "inputs": tuple(path for path in bound + bound_inputs if path != script_path),
        "refs": refs,
        "skipped": skipped + skipped_inputs,
    }


def _accepted_manifest(service, status, prepared):
    """Read the accepted manifest bytes through the existing workspace contract."""
    receipt = status.get("receipt")
    if (not isinstance(receipt, dict) or receipt.get("execution_id") != status["execution_id"]
            or receipt.get("fence") != status.get("fence")
            or type(receipt.get("files")) is not list):
        raise LookupError("execute_receipt_unavailable")
    matches = [item for item in receipt["files"]
               if isinstance(item, dict) and item.get("path") == prepared["manifest_path"]]
    if len(matches) != 1:
        raise LookupError("execute_output_missing")
    ref = matches[0]
    data = service.workspace.read_bytes(prepared["workspace_id"], path=prepared["manifest_path"],
                                        version=ref["version"], expected_sha256=ref["sha256"])
    if len(data) != ref["size_bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise ValueError("execute_output_integrity")
    return data


def _manifest_files(raw, *, nonce, manifest_path, script_path):
    """Validate the wrapper's manifest; hostile or malformed entries are dropped."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("execute_manifest_too_large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("execute_manifest_invalid")
    if data.get("nonce") != nonce:
        raise ValueError("execute_manifest_scope")
    entries = data.get("files")
    if type(entries) is not list or len(entries) > MAX_RETURNED_FILES:
        raise ValueError("execute_manifest_invalid")
    files, rejected, used = [], [], 0
    for entry in entries:
        if type(entry) is not dict:
            rejected.append("malformed_entry")
            continue
        path, digest = entry.get("path"), entry.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            rejected.append("malformed_entry")
            continue
        try:
            logical_path(path, writable=True)
        except ValueError:
            rejected.append(f"{path[:120]} (path)")
            continue
        if path in {manifest_path, script_path} or not path.startswith(("/workspace/", "/outputs/")):
            rejected.append(f"{path[:120]} (scope)")
            continue
        try:
            blob = base64.b64decode(entry.get("data") or "", validate=True)
        except (ValueError, TypeError):
            rejected.append(f"{path[:120]} (encoding)")
            continue
        if (len(blob) > MAX_TRANSFER_FILE_BYTES or used + len(blob) > MAX_TRANSFER_BYTES
                or hashlib.sha256(blob).hexdigest() != digest
                or entry.get("size_bytes") != len(blob)):
            rejected.append(f"{path[:120]} (integrity)")
            continue
        used += len(blob)
        files.append((path, digest, blob))
    dropped = [item for item in (data.get("dropped") or []) if isinstance(item, dict)]
    return files, rejected, dropped, data


def _deliver(view, prepared, files):
    """Write verified bytes back as new versions of the same session paths."""
    written, refused = [], []
    for path, digest, blob in files:
        ref = prepared["refs"].get(path)
        try:
            saved = view.write_back(path, blob,
                                    expected_version=ref["version"] if ref else 0,
                                    expected_sha256=ref["sha256"] if ref else "")
        except Exception as exc:
            refused.append((path, digest, _reason(exc)))
            continue
        written.append((path, saved.get("version"), saved.get("sha256") or digest))
    return written, refused


def _file_report(written, refused, dropped, rejected, skipped):
    lines = []
    for path, version, digest in written:
        lines.append(f"{path} (v{version}, sha256:{digest})")
    for path, digest, reason in refused:
        lines.append(f"{path} (not written back: {reason}, sha256:{digest})")
    for item in dropped:
        path, digest = item.get("path"), item.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            lines.append(f"{path[:200]} (staged in the sandbox only: "
                         f"{str(item.get('reason'))[:32]}, sha256:{digest})")
    for entry in rejected:
        lines.append(f"{entry} (not accepted)")
    if skipped:
        lines.append(f"[{len(skipped)} session file(s) were not bound into this run]")
    if not lines:
        return ""
    return "\n[files]\n" + "\n".join(lines) + "\n"


# -- the one public entry point ---------------------------------------------


def _wait(service, execution_id, budget):
    """Poll to a terminal state; a stuck run is cancelled, never waited on.

    Returns ``(status, expired)``. ``expired`` means this side's budget ran out
    and the Worker was asked to stop, which is reported as a command timeout.
    """
    deadline = time.monotonic() + budget + CANCEL_GRACE_SEC
    while True:
        status = service.status(execution_id)
        if status.get("status") in TERMINAL:
            return status, False
        if time.monotonic() >= deadline:
            try:
                service.cancel(execution_id)
            except Exception:
                pass
            stop = time.monotonic() + CANCEL_GRACE_SEC
            while time.monotonic() < stop:
                status = service.status(execution_id)
                if status.get("status") in TERMINAL:
                    return status, True
                time.sleep(POLL_INTERVAL_SEC)
            return status, True
        time.sleep(POLL_INTERVAL_SEC)


def execute(owner, session, run_id, command, *, timeout=None, service=None, view=None):
    """Run one command in the offline Worker and return its bounded result."""
    if not enabled():
        return ExecuteResult(_text("code_execution_disabled"), None)
    if not isinstance(command, str) or not command.strip():
        return ExecuteResult(_text("empty_command"), None)
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        return ExecuteResult(_text("command_too_long",
                                   f"The limit is {MAX_COMMAND_BYTES} UTF-8 bytes."), None)
    try:
        identity = identify(owner, session, run_id)
    except Exception:
        return ExecuteResult(_text("execution_scope_unavailable"), None)
    if service is None:
        from .service import ExecutionService

        service = ExecutionService(identity)
    from .presence import worker_available

    try:
        ready = worker_available(service.repository.path)
    except Exception:
        ready = False
    if not ready:
        return ExecuteResult(_text("code_worker_unavailable"), None)
    if view is None:
        return ExecuteResult(_text("execution_session_unavailable"), None)
    try:
        deadline = run_deadline(identity)
        budget = effective_timeout(timeout, None if deadline is None else deadline - time.time())
    except ValueError as exc:
        return ExecuteResult(_text(_reason(exc)), None)

    run_token, nonce = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        prepared = _prepare(identity, service, view, command, budget, nonce, run_token)
    except Exception as exc:
        return ExecuteResult(_text("workspace_preparation_failed", _reason(exc)), None)

    # The Worker needs its own room to stop, collect and accept inside the row
    # deadline, so the sandbox budget always exceeds the command timeout.
    sandbox_budget = min(MAX_TIMEOUT_SEC + ACCEPTANCE_HEADROOM_SEC,
                         budget + ACCEPTANCE_HEADROOM_SEC)
    try:
        # Workspace preparation can take time or overlap a stop request.
        # Do not enter the Worker queue after the run has lost admission.
        identify(owner, session, run_id)
        if deadline is not None and deadline - time.time() < MIN_TIMEOUT_SEC + ACCEPTANCE_HEADROOM_SEC:
            return ExecuteResult(_text("execute_run_deadline_exhausted"), None)
        row = service.submit(task_id=prepared["task_id"],
                             workspace_id=prepared["workspace_id"],
                             expected_revision=prepared["revision"],
                             script_path=prepared["script_path"],
                             inputs=prepared["inputs"],
                             outputs=(prepared["manifest_path"],),
                             operation_id="exec_" + run_token,
                             idempotency_key="exec_" + run_token,
                             limits=ExecutionLimits(timeout_sec=sandbox_budget),
                             network_policy="offline", absolute_deadline=deadline)
    except Exception as exc:
        return ExecuteResult(_text("submission_rejected", _reason(exc)), None)

    execution_id = row["execution_id"]
    try:
        status, expired = _wait(service, execution_id, sandbox_budget)
    except Exception as exc:
        return ExecuteResult(_text("status_unavailable", _reason(exc)), None)

    fence = status.get("fence")
    stdout, stdout_size, stderr, stderr_size = b"", 0, b"", 0
    if isinstance(fence, int):
        logs = _Logs(service, execution_id, fence)
        try:
            stdout, stdout_size = logs.read("stdout")
            stderr, stderr_size = logs.read("stderr")
        except Exception:
            stdout, stdout_size, stderr, stderr_size = b"", 0, b"", 0
    stderr_text = _decode(stderr, stderr_size)
    marker = parse_marker(stderr_text, nonce)
    if marker is None and isinstance(fence, int) and stderr_size > len(stderr):
        try:
            marker = parse_marker(_decode(logs.tail("stderr", stderr_size), stderr_size), nonce)
        except Exception:
            marker = None

    state = status.get("status")
    files, rejected, dropped, manifest = [], [], [], {}
    if state == "succeeded":
        try:
            raw = _accepted_manifest(service, status, prepared)
            files, rejected, dropped, manifest = _manifest_files(
                raw, nonce=nonce, manifest_path=prepared["manifest_path"],
                script_path=prepared["script_path"])
        except Exception as exc:
            files, rejected, dropped, manifest = [], [], [], {}
            rejected.append(f"manifest ({_reason(exc)})")
    written, refused = _deliver(view, prepared, files) if files else ([], [])

    truncated = (stdout_size > len(stdout) or stderr_size > len(stderr)
                 or bool(dropped))
    body = _decode(stdout, stdout_size)
    stderr_text = strip_marker(stderr_text, nonce)
    if stderr_text:
        body = body + ("" if not body or body.endswith("\n") else "\n") + stderr_text
    tail = _file_report(written, refused, dropped, rejected, prepared["skipped"])
    tail += f"\n[execution {execution_id} fence {fence}]"
    if marker and marker.get("error"):
        tail += f"\n[{marker['error']}]"

    if marker is not None and marker.get("timed_out"):
        return ExecuteResult(body + f"\n[timed out after {budget}s; the sandbox stopped the "
                                    f"command]" + tail, 124, True)
    if expired:
        return ExecuteResult(body + f"\n[timed out: the {budget}s budget ran out and the "
                                    "Worker was asked to stop]" + tail, 124, True)
    if state == "cancelled":
        return ExecuteResult(body + "\n[execution cancelled before it completed; no exit code "
                                    "is known]" + tail, None, truncated)
    if state == "timed_out":
        return ExecuteResult(body + f"\n[the Worker timed out after {sandbox_budget}s]" + tail,
                             124, True)
    if state != "succeeded":
        reason = status.get("reason") or "worker_failure"
        return ExecuteResult(body + f"\n[execution ended as {state} "
                                    f"({str(reason)[:48]}); no exit code is known]" + tail,
                             None, truncated)
    code = marker["exit_code"] if marker else manifest.get("command_exit_code")
    if type(code) is not int:
        return ExecuteResult(body + "\n[the command ran but its status was not captured]" + tail,
                             None, truncated)
    return ExecuteResult(body + tail, code, truncated)
