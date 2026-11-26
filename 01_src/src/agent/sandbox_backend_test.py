"""The native execute adapter: protocol shape, delegation and no host fallback."""
import ast
import base64
import hashlib
import json
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepagents.backends.protocol import (BackendProtocol, ExecuteResponse,
                                          SandboxBackendProtocol)

from src.agent import sandbox_backend
from src.agent.identity import ExecutionIdentity
from src.agent.workspace_backend import CitrusWorkspaceBackend
from src.code_execution import execution_bridge, presence
from src.core import account_leases, jobs, progress_bus, task_events, tool_results, tracing
from src.session import files, manager
from src.workspaces import TaskWorkspaceService

OWNER = 7
SESSION = "native-execute-session"
REQUEST = "request"


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """One admitted run with its own job/session/file databases."""
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(jobs, "_DB_PATH", db)
    monkeypatch.setattr(manager, "DB_PATH", db)
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tool_results, "_schema_ready", False)
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.db")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    # The account admission store is a separate subsystem; the Workspace
    # contract under test only needs the lease's epoch value.
    monkeypatch.setattr(account_leases, "account_lease",
                        lambda owner: nullcontext(SimpleNamespace(epoch=1)))
    previous = manager.SessionManager._instance
    manager.SessionManager._instance = None
    manager.SessionManager()._create_session_sync(SESSION, OWNER)
    claim = jobs.claim_job(SESSION, REQUEST, owner_user_id=OWNER,
                           client_request_id=REQUEST, payload={})
    identity = ExecutionIdentity(OWNER, SESSION, REQUEST, claim.job_id)
    task_events.init_budget(claim.job_id, time.time() + 3600, max_calls=10,
                            max_tokens=100_000)
    tracing_state = (tracing.get_user_id(), tracing.get_session_id(), tracing.get_job_id())
    tracing.set_user_id(OWNER)
    tracing.set_session_id(SESSION)
    tracing.set_job_id(identity.run_id)
    progress_bus.set_turn_scope(SESSION, REQUEST)
    try:
        yield identity
    finally:
        progress_bus.clear_turn_scope()
        tracing.set_user_id(tracing_state[0])
        tracing.set_session_id(tracing_state[1])
        tracing.set_job_id(tracing_state[2])
        manager.SessionManager._instance = previous


def backend_for(identity):
    return sandbox_backend.CitrusSandboxBackend(identity.owner_user_id,
                                                identity.session_id, identity.run_id)


def session_backend(identity):
    return CitrusWorkspaceBackend(identity.owner_user_id, identity.session_id, identity.run_id)


def script_source(file_id):
    with files._connect() as conn:
        row = conn.execute("SELECT storage_path FROM session_files WHERE file_id=?",
                           (file_id,)).fetchone()
    return Path(row["storage_path"]).read_text(encoding="utf-8")


class FakeWorker:
    """ExecutionService stand-in that keeps the real Workspace contract."""

    def __init__(self, identity, *, statuses=("succeeded",), files=(), exit_code=0,
                 timed_out=False, stdout=b"", stderr=b""):
        self.identity = identity
        self.repository = SimpleNamespace(path=Path("/nonexistent/code_executions.sqlite3"))
        self.workspace = TaskWorkspaceService(identity.owner_user_id, identity.session_id)
        self.statuses = list(statuses)
        self.payload = list(files)
        self.exit_code, self.timed_out = exit_code, timed_out
        self.stdout, self.stderr = stdout, stderr
        self.submissions, self.cancelled = [], []
        self.execution_id, self.fence = "a" * 32, 11
        self.receipt, self.logs = None, {}

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        snapshot = self.workspace.snapshot(kwargs["workspace_id"], task_id=kwargs["task_id"],
                                           expected_revision=kwargs["expected_revision"])
        bound = {item["path"] for item in snapshot["files"]}
        assert kwargs["script_path"] in bound
        assert all(path in bound for path in kwargs["inputs"])
        assert kwargs["script_path"] not in kwargs["inputs"]
        assert kwargs["outputs"] and kwargs["outputs"][0].startswith("/outputs/")
        wrapper = self._wrapper(kwargs["workspace_id"], kwargs["script_path"])
        manifest = self._accept(kwargs["workspace_id"], kwargs["outputs"][0], wrapper)
        marker = {"exit_code": self.exit_code, "timed_out": self.timed_out, "error": ""}
        self.logs = {"stdout": self.stdout,
                     "stderr": self.stderr + ("\n" + execution_bridge.MARKER + " "
                                              + wrapper["NONCE"] + " "
                                              + json.dumps(marker, separators=(",", ":"))
                                              + "\n").encode()}
        self.manifest_seen = manifest
        return {"execution_id": self.execution_id, "status": "queued"}

    def _wrapper(self, workspace_id, script_path):
        items = self.workspace.list_files(workspace_id, limit=100)["items"]
        match = [item for item in items if item["path"] == script_path]
        assert len(match) == 1, match
        source = script_source(match[0]["file_id"])
        values = {}
        for node in ast.parse(source).body:
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in {"COMMAND", "NONCE", "MANIFEST", "BASELINE",
                                               "SKIP", "TIMEOUT_SEC"}):
                values[node.targets[0].id] = ast.literal_eval(node.value)
        return values

    def _accept(self, workspace_id, manifest_path, wrapper):
        entries = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data), "data": base64.b64encode(data).decode()}
                   for path, data in self.payload]
        manifest = {"schema": execution_bridge.MANIFEST_SCHEMA, "nonce": wrapper["NONCE"],
                    "command_exit_code": self.exit_code, "timed_out": self.timed_out,
                    "error": "", "files": entries, "dropped": []}
        data = json.dumps(manifest).encode("utf-8")
        record = files.register_file(owner_user_id=self.identity.owner_user_id,
                                     session_id=self.identity.session_id,
                                     filename=manifest_path.rsplit("/", 1)[-1], content=data,
                                     mime="application/json", visibility="internal",
                                     producer_tool="fake_worker")
        revision = self.workspace.open(workspace_id)["revision"]
        self.workspace.bind(workspace_id, path=manifest_path, file_id=record["file_id"],
                            expected_revision=revision, expected_sha256=record["sha256"],
                            source={"tool": "native_execute"})
        self.receipt = {"execution_id": self.execution_id, "fence": self.fence,
                        "files": [{"path": manifest_path, "file_id": record["file_id"],
                                   "version": 1, "sha256": record["sha256"],
                                   "size_bytes": len(data)}]}
        return manifest

    def status(self, execution_id):
        state = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"execution_id": execution_id, "task_id": "code_task",
                "workspace_id": "", "status": state, "fence": self.fence,
                "created": 0.0, "updated": 0.0, "deadline": 0.0,
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


@pytest.fixture
def worker(scope, monkeypatch):
    """A worker stub with a present Worker and offline execution enabled."""
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(presence, "worker_available", lambda path: True)
    monkeypatch.setattr(execution_bridge, "POLL_INTERVAL_SEC", 0.0)
    return lambda **kwargs: FakeWorker(scope, **kwargs)


def no_host_process(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a host process was used")

    for name in ("Popen", "run", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, explode)
    for name in ("system", "popen", "spawnv", "execv", "fork"):
        monkeypatch.setattr(sandbox_backend.execution_bridge.os, name, explode)


# -- protocol shape ----------------------------------------------------------


def test_the_backend_is_a_sandbox_with_a_stable_scope_id(scope):
    backend = backend_for(scope)
    assert isinstance(backend, SandboxBackendProtocol)
    assert isinstance(backend, BackendProtocol)
    assert hasattr(backend, "execute") and hasattr(backend, "aexecute")
    for value in (str(OWNER), SESSION, scope.run_id):
        assert value in backend.id
    assert backend_for(scope).id == backend.id
    for owner, session, run in ((0, SESSION, "run"), (OWNER, "", "run"), (OWNER, SESSION, "")):
        with pytest.raises(ValueError):
            sandbox_backend.CitrusSandboxBackend(owner, session, run)


def test_file_tools_and_execute_share_one_workspace(scope, worker, monkeypatch):
    backend = backend_for(scope)
    assert backend.write("/workspace/notes.md", "alpha\n").error is None
    assert [item["path"] for item in backend.ls("/workspace").entries] == ["/workspace/notes.md"]
    assert backend.glob("*.md", "/workspace").matches[0]["path"] == "/workspace/notes.md"
    assert backend.grep("alpha", "/workspace").matches[0]["text"] == "alpha"

    # The very same version/hash is what the Worker receives as /workspace input.
    engine = worker(files=[("/outputs/derived.md", b"beta\n")])
    monkeypatch.setattr(execution_bridge, "identify",
                        lambda owner, session, run_id: scope)
    monkeypatch.setattr("src.code_execution.service.ExecutionService", lambda identity: engine)
    result = sandbox_backend.CitrusSandboxBackend(scope.owner_user_id, scope.session_id,
                                                  scope.run_id).execute("true")
    assert isinstance(result, ExecuteResponse)
    assert result.exit_code == 0
    assert "/workspace/notes.md" in engine.submissions[0]["inputs"]
    assert "/outputs/derived.md (v1, sha256:" in result.output
    text, ref = session_backend(scope).storage.text("/outputs/derived.md")
    assert text == "beta\n" and ref["version"] == 1


# -- delegation --------------------------------------------------------------


def test_file_operations_are_the_lead_backend_unchanged(scope):
    backend = backend_for(scope)
    assert backend.write("/outputs/x.md", "one").error is None
    assert backend.read("/outputs/x.md").file_data["content"] == "one"
    assert backend.edit("/outputs/x.md", "one", "two").error is None
    assert backend.read("/outputs/x.md").file_data["content"] == "two"
    uploaded = backend.upload_files([("/workspace/up.md", b"up")])
    assert uploaded[0].error is None
    downloaded = backend.download_files(["/workspace/up.md"])
    assert downloaded[0].content == b"up"
    assert backend.delete("/workspace/up.md").error is None
    # Read-only mounts stay refused through the sandbox wrapper too.
    assert backend.write("/input/data.txt", "nope").error == "permission_denied"
    assert backend.write("/skills/SKILL.md", "nope").error == "permission_denied"


# -- the unavailable paths ---------------------------------------------------


def test_execute_is_not_run_without_a_worker_and_never_uses_a_host_shell(scope, monkeypatch):
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(presence, "worker_available", lambda path: False)
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)
    no_host_process(monkeypatch)
    response = backend_for(scope).execute("python -c 'print(1)'")
    assert response.exit_code is None
    assert response.output.startswith("Error: execute not run (code_worker_unavailable)")
    assert response.truncated is False


def test_execute_is_not_run_when_offline_execution_is_disabled(scope, monkeypatch):
    monkeypatch.delenv("CITRUS_CODE_EXECUTION", raising=False)
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)
    no_host_process(monkeypatch)
    response = backend_for(scope).execute("python -c 'print(1)'")
    assert response.exit_code is None
    assert "code_execution_disabled" in response.output


def test_an_unknown_identity_is_not_run(scope, monkeypatch):
    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    no_host_process(monkeypatch)
    stranger = sandbox_backend.CitrusSandboxBackend(OWNER, SESSION, "other-run")
    response = stranger.execute("true")
    assert response.exit_code is None
    assert "execution_scope_unavailable" in response.output


# -- the successful end-to-end path -----------------------------------------


def test_execute_returns_output_exit_code_and_new_workspace_versions(scope, worker, monkeypatch):
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)
    assert session_backend(scope).write("/workspace/notes.md", "before\n").error is None
    engine = worker(files=[("/workspace/notes.md", b"after\n"),
                           ("/outputs/plot.csv", b"x,y\n1,2\n")], stdout=b"ran\n")
    monkeypatch.setattr("src.code_execution.service.ExecutionService", lambda identity: engine)

    response = backend_for(scope).execute("python plot.py")

    assert response.exit_code == 0
    assert response.truncated is False
    assert "ran" in response.output
    assert "/workspace/notes.md (v2, sha256:" in response.output
    assert "/outputs/plot.csv (v1, sha256:" in response.output
    reader = session_backend(scope)
    assert reader.storage.text("/workspace/notes.md")[0] == "after\n"
    assert reader.storage.text("/outputs/plot.csv")[0] == "x,y\n1,2\n"
    assert reader.read("/workspace/notes.md").file_data["content"] == "after\n"
    # The wrapper script is bound in the code workspace, not the session store.
    assert reader.read(engine.submissions[0]["script_path"]).error == "file_not_found"


def test_a_binary_or_oversized_output_is_reported_and_not_stored(scope, worker, monkeypatch):
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)
    engine = worker(files=[("/outputs/big.txt", b"\x00\x01" * 400),
                           ("/outputs/huge.txt", b"a" * 70000),
                           ("/outputs/plot.bin", b"\x00\x01")])
    monkeypatch.setattr("src.code_execution.service.ExecutionService", lambda identity: engine)
    response = backend_for(scope).execute("true")
    assert response.exit_code == 0
    # A NUL byte, a text file above the 64 KiB version cap and an unsupported
    # suffix are each reported by path + SHA-256 instead of being invented.
    assert "/outputs/big.txt (not written back: binary_not_stored_in_workspace" in response.output
    assert "/outputs/huge.txt (not written back: file_too_large_for_workspace" in response.output
    assert "/outputs/plot.bin (not written back: workspace_text_extension_required" in response.output
    reader = session_backend(scope)
    stored = reader.storage.index("/")[1]
    for path in ("/outputs/big.txt", "/outputs/huge.txt", "/outputs/plot.bin"):
        assert path not in stored

def test_a_worker_timeout_is_124_and_a_user_cancel_has_no_exit_code(scope, worker, monkeypatch):
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)
    timed = worker(statuses=("timed_out",))
    monkeypatch.setattr("src.code_execution.service.ExecutionService", lambda identity: timed)
    assert backend_for(scope).execute("sleep 999").exit_code == 124

    cancelled = worker(statuses=("cancelled",))
    monkeypatch.setattr("src.code_execution.service.ExecutionService", lambda identity: cancelled)
    response = backend_for(scope).execute("sleep 999")
    assert response.exit_code is None
    assert "cancelled" in response.output


def test_execute_rejects_a_command_that_cannot_be_bounded(scope, worker, monkeypatch):
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)
    monkeypatch.setattr("src.code_execution.service.ExecutionService",
                        lambda identity: worker(files=[]))
    oversized = "x" * (execution_bridge.MAX_COMMAND_BYTES + 1)
    response = backend_for(scope).execute(oversized)
    assert response.exit_code is None
    assert "command_too_long" in response.output


def test_a_backend_failure_is_never_a_silent_success(scope, monkeypatch):
    monkeypatch.setattr(execution_bridge, "identify", lambda owner, session, run_id: scope)

    def broken(identity):
        raise RuntimeError("/host/private/path")

    monkeypatch.setattr("src.code_execution.service.ExecutionService", broken)
    response = backend_for(scope).execute("true")
    assert response.exit_code is None
    assert "/host/private/path" not in response.output


def test_the_graph_registers_execute_only_with_the_sandbox_backend(scope):
    """FilesystemMiddleware must see SandboxBackendProtocol to offer execute."""
    from deepagents.backends import CompositeBackend
    from deepagents.middleware.filesystem import (FilesystemMiddleware, FilesystemPermission,
                                                  supports_execution)

    sandbox = backend_for(scope)
    assert supports_execution(sandbox) is True
    assert supports_execution(CitrusWorkspaceBackend(OWNER, SESSION, scope.run_id)) is False

    # The graph wires a CompositeBackend; execution follows its default.
    composite = CompositeBackend(default=sandbox, routes={}, artifacts_root="/workspace")
    assert supports_execution(composite) is True
    plain = CompositeBackend(default=session_backend(scope), routes={},
                             artifacts_root="/workspace")
    assert supports_execution(plain) is False

    # The current graph-wide permission set cannot coexist with execution:
    # every rule must be scoped to a route prefix instead.
    permissions = [
        FilesystemPermission(operations=["write", "edit"],
                             paths=["/workspace/**", "/outputs/**"], mode="allow"),
        FilesystemPermission(operations=["delete"],
                             paths=["/workspace/**", "/outputs/**"], mode="allow"),
        FilesystemPermission(operations=["write", "edit", "delete"], paths=["/**"],
                             mode="deny")]
    with pytest.raises(NotImplementedError):
        FilesystemMiddleware(backend=composite, permissions=permissions,
                            tools=["read_file", "ls", "execute"])
    middleware = FilesystemMiddleware(backend=composite, tools=["read_file", "ls", "execute"])
    assert "execute" in {tool.name for tool in middleware.tools}
