"""Native execute semantics: Worker-only, bounded, and never a host shell."""
import ast
import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.identity import ExecutionIdentity
from src.code_execution import execution_bridge, presence

IDENTITY = ExecutionIdentity(7, "bridge-session", "turn", "run-1")
MANIFEST_SCHEMA = execution_bridge.MANIFEST_SCHEMA
SOURCES = {}


class FakeView:
    """The session write-back seam, without any storage behind it."""

    def __init__(self, refs=None):
        self.paths = {path: dict(ref) for path, ref in (refs or {}).items()}
        self.writes = []
        self.refuse = {}

    def refs(self):
        return {path: dict(ref) for path, ref in self.paths.items()}

    def write_back(self, path, data, *, expected_version, expected_sha256):
        if path in self.refuse:
            raise ValueError(self.refuse[path])
        self.writes.append({"path": path, "data": data, "expected_version": expected_version,
                            "expected_sha256": expected_sha256})
        digest = hashlib.sha256(data).hexdigest()
        version = expected_version + 1
        self.paths[path] = {"kind": "workspace_file", "file_id": "b" * 32,
                            "version": version, "sha256": digest}
        return {"version": version, "sha256": digest}


class FakeWorkspace:
    """Records the binding contract a controller would re-validate."""

    def __init__(self):
        self.workspace_id = "c" * 32
        self.task_id = None
        self.revision = 1
        self.files = {}
        self.binds = []
        self.payloads = {}

    def read_bytes(self, workspace_id, *, path, version=None, expected_sha256=None):
        return self.payloads[path]

    def create(self, *, task_id, request_id):
        self.task_id = task_id
        return self.open()

    def open(self, workspace_id=None):
        return {"workspace_id": self.workspace_id, "task_id": self.task_id,
                "revision": self.revision}

    def list_files(self, workspace_id, *, offset=0, limit=50, history=False):
        items = [{"path": path, "file_id": item["file_id"], "sha256": item["sha256"],
                  "version": item["version"], "size_bytes": item["size_bytes"]}
                 for path, item in sorted(self.files.items())]
        end = offset + limit
        return {"items": items[offset:end], "next_offset": end if len(items) > end else None}

    def bind(self, workspace_id, *, path, file_id, expected_revision, expected_sha256,
             source=None, parents=None):
        assert expected_revision == self.revision, (expected_revision, self.revision)
        self.binds.append({"path": path, "file_id": file_id, "source": source})
        previous = self.files.get(path, {}).get("version", 0)
        self.revision += 1
        self.files[path] = {"file_id": file_id, "sha256": expected_sha256,
                            "version": previous + 1, "size_bytes": 1}
        return {"revision": self.revision, "ref": {"path": path, "version": previous + 1}}


class FakeService:
    """Stand-in for ``ExecutionService`` covering only the polling contract."""

    def __init__(self, *, statuses=("succeeded",), files=(), dropped=(), exit_code=0,
                 timed_out=False, stderr=b"", stdout=b"", manifest=True, error=""):
        self.workspace = FakeWorkspace()
        self.repository = SimpleNamespace(path=Path("/nonexistent/code_executions.sqlite3"))
        self.statuses = list(statuses)
        self.payload = list(files)
        self.dropped = list(dropped)
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.error = error
        self.stdout, self.stderr = stdout, stderr
        self.want_manifest = manifest
        self.submissions, self.cancelled = [], []
        self.execution_id = "a" * 32
        self.fence = 11
        self.receipt = None
        self.logs = {}

    # -- submission ----------------------------------------------------
    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        if self.want_manifest:
            manifest_path = kwargs["outputs"][0]
            entries = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data), "data": base64.b64encode(data).decode()}
                       for path, data in self.payload]
            manifest = {"schema": MANIFEST_SCHEMA, "nonce": self._nonce(kwargs["script_path"]),
                        "command_exit_code": self.exit_code, "timed_out": self.timed_out,
                        "error": self.error, "files": entries, "dropped": self.dropped}
            data = json.dumps(manifest).encode()
            self.workspace.bind(kwargs["workspace_id"], path=manifest_path,
                                file_id="e" * 32, expected_revision=kwargs["expected_revision"],
                                expected_sha256=hashlib.sha256(data).hexdigest())
            self.workspace.payloads[manifest_path] = data
            self.receipt = {"execution_id": self.execution_id, "fence": self.fence,
                            "files": [{"path": manifest_path, "file_id": "e" * 32, "version": 1,
                                       "sha256": hashlib.sha256(data).hexdigest(),
                                       "size_bytes": len(data)}]}
        marker = {"exit_code": self.exit_code, "timed_out": self.timed_out, "error": self.error}
        self.logs = {"stdout": self.stdout,
                     "stderr": self.stderr + self._marker_line(kwargs["script_path"], marker)}
        return {"execution_id": self.execution_id, "status": "queued"}

    def _nonce(self, path):
        source = self.script_source(path)
        for node in ast.parse(source).body:
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "NONCE"):
                return ast.literal_eval(node.value)
        raise AssertionError("wrapper has no NONCE")

    def _marker_line(self, path, payload):
        # Proves the marker is last on stderr and is the wrapper's own record.
        return ("\n" + execution_bridge.MARKER + " " + self._nonce(path) + " "
                + json.dumps(payload, separators=(",", ":")) + "\n").encode()

    def script_source(self, path):
        return SOURCES[path]

    # -- polling -------------------------------------------------------
    def status(self, execution_id):
        state = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"execution_id": execution_id, "task_id": "code_task",
                "workspace_id": self.workspace.workspace_id, "status": state,
                "fence": self.fence, "created": 0.0, "updated": 0.0, "deadline": 0.0,
                "reason": "" if state == "succeeded" else "script_exit_nonzero",
                "receipt": self.receipt if state == "succeeded" else None}

    def read_log(self, execution_id, *, fence, stream, offset=0, limit=16384):
        data = self.logs.get(stream, b"")
        chunk = data[offset:offset + limit]
        end = offset + len(chunk)
        return {"execution_id": execution_id, "fence": fence, "stream": stream,
                "size_bytes": len(data), "offset": offset,
                "next_offset": end if end < len(data) else None, "eof": end == len(data),
                "returned_bytes": len(chunk),
                "data_base64": base64.b64encode(chunk).decode("ascii"),
                "text_preview": chunk.decode("utf-8", "replace")}

    def cancel(self, execution_id):
        self.cancelled.append(execution_id)
        return {"execution_id": execution_id, "status": "cancel_requested"}

    def read_bytes(self, workspace_id, *, path, version=None, expected_sha256=None):
        return self._manifest_bytes


@pytest.fixture
def worker(monkeypatch):
    """A commit-free worker stub plus the host-side seams it needs."""
    SOURCES.clear()
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: IDENTITY)
    monkeypatch.setattr(execution_bridge, "run_deadline", lambda identity: execution_bridge.time.time() + 3600.0)
    monkeypatch.setattr(presence, "worker_available", lambda path: True)
    monkeypatch.setattr(execution_bridge, "POLL_INTERVAL_SEC", 0.0)

    def register(identity, path, source):
        SOURCES[path] = source
        return {"file_id": "d" * 32, "sha256": hashlib.sha256(source.encode()).hexdigest()}

    monkeypatch.setattr(execution_bridge, "_register_script", register)
    return FakeService


@pytest.fixture
def view():
    return FakeView({"/workspace/report.md": {"kind": "workspace_file", "file_id": "1" * 32,
                                              "version": 2, "sha256": "2" * 64},
                     "/input/data.csv": {"kind": "workspace_input", "file_id": "3" * 32,
                                         "version": 1, "sha256": "4" * 64}})


def run(service, view, command="echo hello", **kwargs):
    return execution_bridge.execute(IDENTITY.owner_user_id, IDENTITY.session_id,
                                    IDENTITY.run_id, command, service=service, view=view,
                                    **kwargs)


def no_host_process(monkeypatch):
    """Any host shell/interpreter use fails the test loudly."""

    def explode(*args, **kwargs):
        raise AssertionError("a host process was used")

    for name in ("Popen", "run", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, explode)
    for name in ("system", "popen", "spawnv", "execv", "fork"):
        monkeypatch.setattr(execution_bridge.os, name, explode)


# -- the unavailable paths ---------------------------------------------------


def test_missing_worker_is_not_run_and_never_falls_back(monkeypatch, view):
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(execution_bridge, "identify", lambda *scope: IDENTITY)
    monkeypatch.setattr(presence, "worker_available", lambda path: False)
    no_host_process(monkeypatch)
    result = run(FakeService(), view)
    assert result.exit_code is None
    assert result.output.startswith("Error: execute not run (code_worker_unavailable)")
    assert "no host shell fallback" in result.output


def test_disabled_execution_is_refused_before_the_worker_is_consulted(monkeypatch, view):
    monkeypatch.delenv("CITRUS_CODE_EXECUTION", raising=False)

    def never(path):
        raise AssertionError("availability must not be read when execution is disabled")

    monkeypatch.setattr(presence, "worker_available", never)
    no_host_process(monkeypatch)
    result = run(FakeService(), view)
    assert result.exit_code is None
    assert "code_execution_disabled" in result.output


def test_an_unknown_run_scope_is_not_run(monkeypatch, view):
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(presence, "worker_available", lambda path: True)
    no_host_process(monkeypatch)
    result = run(FakeService(), view)
    assert result.exit_code is None
    assert "execution_scope_unavailable" in result.output


def test_sandbox_ready_needs_both_the_profile_and_a_worker(monkeypatch):
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(presence, "worker_available", lambda path: True)
    assert execution_bridge.sandbox_ready() is True
    monkeypatch.setattr(presence, "worker_available", lambda path: False)
    assert execution_bridge.sandbox_ready() is False
    monkeypatch.delenv("CITRUS_CODE_EXECUTION", raising=False)

    def never(path):
        raise AssertionError("availability must not be read when execution is disabled")

    monkeypatch.setattr(presence, "worker_available", never)
    assert execution_bridge.sandbox_ready() is False

def test_command_size_and_empty_text_are_refused(worker, view):
    assert "empty_command" in run(worker(), view, "   ").output
    oversized = "x" * (execution_bridge.MAX_COMMAND_BYTES + 1)
    assert "command_too_long" in run(worker(), view, oversized).output


# -- the successful path -----------------------------------------------------


def test_success_reports_exit_code_output_and_written_files(worker, view):
    service = worker(files=[("/outputs/report.csv", b"a,b\n1,2\n")], stdout=b"hello\n")
    result = run(service, view, "python run.py")

    assert result.exit_code == 0
    assert result.truncated is False
    assert "hello" in result.output
    assert "/outputs/report.csv (v1, sha256:" in result.output
    # The new output is written into the session store, /input is never written.
    assert [item["path"] for item in view.writes] == ["/outputs/report.csv"]
    assert view.writes[0]["expected_version"] == 0
    # The command text is data in the script, never a shell string on the host.
    submission = service.submissions[0]
    assert submission["outputs"] and submission["outputs"][0].startswith("/outputs/")
    assert submission["network_policy"] == "offline"
    # The Worker keeps collection headroom beyond the command timeout.
    assert (submission["limits"].timeout_sec
            == execution_bridge.DEFAULT_TIMEOUT_SEC + execution_bridge.ACCEPTANCE_HEADROOM_SEC)
    assert submission["script_path"].endswith(".py")
    assert all(path.startswith(("/workspace/", "/outputs/", "/input/"))
               for path in submission["inputs"])
    assert "/workspace/report.md" in submission["inputs"]
    assert "/input/data.csv" in submission["inputs"]


def test_a_changed_session_file_is_written_back_on_its_observed_version(worker, view):
    service = worker(files=[("/workspace/report.md", b"new body")])
    result = run(service, view)
    assert result.exit_code == 0
    write = view.writes[0]
    assert write["path"] == "/workspace/report.md"
    assert write["expected_version"] == 2 and write["expected_sha256"] == "2" * 64
    assert "/workspace/report.md (v3, sha256:" in result.output


def test_an_unchanged_session_file_is_not_rewritten(worker, view):
    service = worker(files=[])
    result = run(service, view)
    assert result.exit_code == 0
    assert view.writes == []
    assert "[files]" not in result.output


def test_the_marker_is_stripped_from_visible_output(worker, view):
    service = worker(stdout=b"visible\n", stderr=b"stderr line\n")
    result = run(service, view)
    assert "visible" in result.output and "stderr line" in result.output
    assert execution_bridge.MARKER not in result.output


def test_the_manifest_status_is_used_when_the_marker_is_missing(worker, view):
    service = worker(files=[], exit_code=3)
    service._marker_line = lambda path, payload: b""
    result = run(service, view)
    assert result.exit_code == 3


def test_a_nonzero_command_status_still_returns_its_files(worker, view):
    service = worker(files=[("/workspace/out.md", b"partial")], exit_code=2,
                     stderr=b"boom\n")
    result = run(service, view, "false")
    assert result.exit_code == 2
    assert "boom" in result.output
    assert "/workspace/out.md (v1, sha256:" in result.output


# -- timeouts and cancellation -----------------------------------------------


def test_the_sandbox_timeout_is_reported_as_124_with_partial_output(worker, view):
    service = worker(timed_out=True, stderr=b"before the stop\n")
    result = run(service, view, timeout=30)
    assert result.exit_code == 124
    assert result.truncated is True
    assert "timed out after 30s" in result.output


def test_a_worker_side_timeout_has_no_confirmed_command_exit(worker, view):
    service = worker(statuses=("timed_out",), manifest=False)
    result = run(service, view)
    assert result.exit_code is None
    assert result.not_run_reason == ""
    assert "Worker timed out" in result.output


def test_a_cancelled_run_has_no_exit_code(worker, view):
    service = worker(statuses=("cancelled",), manifest=False)
    result = run(service, view)
    assert result.exit_code is None
    assert "cancelled before it completed" in result.output


def test_an_expired_budget_has_no_confirmed_command_exit(worker, view, monkeypatch):
    service = worker(statuses=("running",), manifest=False)
    monkeypatch.setattr(execution_bridge, "_wait", lambda *args: (service.status("a" * 32), True))
    result = run(service, view, timeout=20)
    assert result.exit_code is None
    assert result.not_run_reason == ""
    assert "budget ran out" in result.output


def test_wait_cancels_a_stuck_worker(worker, monkeypatch):
    service = worker(statuses=("running",), manifest=False)
    monkeypatch.setattr(execution_bridge, "CANCEL_GRACE_SEC", 0)
    status, expired = execution_bridge._wait(service, "a" * 32, 0)
    assert expired is True and status["status"] == "running"
    assert service.cancelled == ["a" * 32]


def test_a_run_deadline_caps_the_command_timeout():
    assert execution_bridge.effective_timeout(None, None) == 120
    assert execution_bridge.effective_timeout(None, float("inf")) == 120
    for invalid in (float("nan"), -float("inf")):
        with pytest.raises(ValueError, match="execute_run_deadline_exhausted"):
            execution_bridge.effective_timeout(None, invalid)
    assert execution_bridge.effective_timeout(30, None) == 30
    assert execution_bridge.effective_timeout(10_000, None) == execution_bridge.MAX_TIMEOUT_SEC
    assert execution_bridge.effective_timeout(None, 90) == 60
    with pytest.raises(ValueError):
        execution_bridge.effective_timeout(None, 10)
    with pytest.raises(ValueError):
        execution_bridge.effective_timeout(None, 34)
    with pytest.raises(ValueError):
        execution_bridge.effective_timeout(True)


# -- manifest validation and injection ---------------------------------------


@pytest.mark.parametrize("path", ["/input/data.csv", "/skills/SKILL.md",
                                  "/workspace/../etc/passwd", "/etc/passwd",
                                  "/01_src/src/app.py"])
def test_only_writable_workspace_paths_are_accepted(worker, view, path):
    service = worker(files=[(path, b"hostile")])
    result = run(service, view)
    assert result.exit_code == 0
    assert view.writes == []
    assert "not accepted" in result.output


def test_a_refused_write_back_is_reported_not_hidden(worker, view):
    view.refuse["/outputs/big.bin"] = "file_too_large_for_workspace"
    service = worker(files=[("/outputs/big.bin", b"x" * 16)])
    result = run(service, view)
    assert result.exit_code == 0
    assert "/outputs/big.bin (not written back: file_too_large_for_workspace" in result.output


def test_dropped_sandbox_files_are_listed_and_mark_the_result_truncated(worker, view):
    dropped = [{"path": "/outputs/huge.bin", "sha256": "9" * 64, "size_bytes": 999,
                "reason": "transfer_limit"}]
    service = worker(dropped=dropped)
    result = run(service, view)
    assert result.truncated is True
    assert "/outputs/huge.bin (staged in the sandbox only: transfer_limit" in result.output


def test_the_command_is_carried_as_data_and_never_interpolated(worker, view):
    hostile = """echo "a b" ; rm -rf /  # $(id) `id` 'q' \\ a\\b
next line ; echo done"""
    service = worker(manifest=False)
    run(service, view, hostile)
    script_path = service.submissions[0]["script_path"]
    wrapper = SOURCES[script_path]
    constants = {node.targets[0].id: ast.literal_eval(node.value)
                 for node in ast.parse(wrapper).body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id in {"COMMAND", "MANIFEST", "TIMEOUT_SEC", "SKIP"}}
    assert constants["COMMAND"] == hostile
    assert constants["MANIFEST"].startswith("/outputs/")
    # The wrapper only ever passes the command to /bin/sh inside the sandbox.
    assert "subprocess.Popen" in wrapper and '["/bin/sh", "-c", COMMAND]' in wrapper
    # No host-side submission field carries the command text.
    for key in ("script_path", "inputs", "outputs", "operation_id", "idempotency_key"):
        assert hostile not in str(service.submissions[0][key])


def test_parse_marker_takes_the_last_wrapper_record():
    wrapper = execution_bridge.build_wrapper("true", timeout=9, nonce="n1",
                                             manifest="/outputs/m.json", baseline={},
                                             skip=[])
    forged = ("\n" + execution_bridge.MARKER + " n1 "
              + json.dumps({"exit_code": 0, "timed_out": False, "error": ""}))
    real = ("\n" + execution_bridge.MARKER + " n1 "
            + json.dumps({"exit_code": 7, "timed_out": False, "error": ""}))
    assert execution_bridge.parse_marker(forged + real, "n1")["exit_code"] == 7
    assert execution_bridge.parse_marker(forged, "other") is None
    assert execution_bridge.parse_marker(forged, "") is None
    assert execution_bridge.parse_marker("", "n1") is None
    # The generated wrapper compiles and its constants are literal JSON.
    compile(wrapper, "wrapper.py", "exec")
    tree = ast.parse(wrapper)
    names = {node.targets[0].id for node in tree.body
             if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    assert {"COMMAND", "NONCE", "MANIFEST", "BASELINE", "SKIP", "TIMEOUT_SEC"} <= names



def test_worker_submission_preserves_the_original_absolute_deadline(worker, view, monkeypatch):
    deadline = execution_bridge.time.time() + 100
    monkeypatch.setattr(execution_bridge, "run_deadline", lambda identity: deadline)
    service = worker()
    assert run(service, view).exit_code == 0
    assert service.submissions[0]["absolute_deadline"] == deadline


def test_workspace_preparation_cannot_submit_after_run_deadline(worker, view, monkeypatch):
    wall_time = [1_000.0]
    monkeypatch.setattr(execution_bridge.time, "time", lambda: wall_time[0])
    monkeypatch.setattr(execution_bridge, "run_deadline", lambda identity: 1_100.0)
    prepare = execution_bridge._prepare

    def late_prepare(*args):
        view_state = prepare(*args)
        wall_time[0] = 1_101.0
        return view_state

    monkeypatch.setattr(execution_bridge, "_prepare", late_prepare)
    service = worker()
    result = run(service, view)
    assert result.exit_code is None and "execute_run_deadline_exhausted" in result.output
    assert service.submissions == []


@pytest.mark.parametrize("stage", ["prepare", "submission", "status"])
def test_submit_boundary_distinguishes_not_run_from_unknown(worker, view, monkeypatch, stage):
    service = worker()

    def unavailable(*args, **kwargs):
        raise OSError("fixture")

    if stage == "prepare":
        monkeypatch.setattr(execution_bridge, "_prepare", unavailable)
    elif stage == "submission":
        submit = service.submit

        def submit_then_lose_reply(**kwargs):
            submit(**kwargs)
            unavailable()

        monkeypatch.setattr(service, "submit", submit_then_lose_reply)
    else:
        monkeypatch.setattr(service, "status", unavailable)

    result = run(service, view)
    assert result.exit_code is None
    if stage == "prepare":
        assert not service.submissions
        assert result.not_run_reason == "workspace_preparation_failed"
    else:
        assert len(service.submissions) == 1
        assert not result.not_run_reason
        assert "could not be confirmed" in result.output
        assert "not run" not in result.output
