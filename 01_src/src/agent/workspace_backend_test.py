"""One native Deep Agents file view over the existing Workspace storage."""
import time

import pytest

from src.agent import workspace_backend
from src.agent.identity import ExecutionIdentity
from src.core import jobs, progress_bus, task_events, tool_results, tracing
from src.session import files, manager


@pytest.fixture
def scope(tmp_path, monkeypatch):
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(jobs, "_DB_PATH", db)
    monkeypatch.setattr(manager, "DB_PATH", db)
    monkeypatch.setattr(tool_results, "_schema_ready", False)
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.db")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    claim = jobs.claim_job("native-file-session", "request", owner_user_id=7,
                           client_request_id="request", payload={})
    identity = ExecutionIdentity(7, "native-file-session", "request", claim.job_id)
    task_events.init_budget(claim.job_id, time.time() + 60, max_calls=10, max_tokens=100_000)
    previous = (tracing.get_user_id(), tracing.get_session_id(), tracing.get_job_id())
    tracing.set_user_id(identity.owner_user_id)
    tracing.set_session_id(identity.session_id)
    tracing.set_job_id(identity.run_id)
    progress_bus.set_turn_scope(identity.session_id, identity.user_turn_id)
    try:
        yield identity
    finally:
        progress_bus.clear_turn_scope()
        tracing.set_user_id(previous[0])
        tracing.set_session_id(previous[1])
        tracing.set_job_id(previous[2])


def backend_for(identity):
    return workspace_backend.CitrusWorkspaceBackend(
        identity.owner_user_id, identity.session_id, identity.run_id)


def read_text(backend, path, offset=0, limit=2000):
    result = backend.read(path, offset, limit)
    assert result.error is None, result.error
    return result.file_data["content"]


def test_write_lists_reads_and_searches_the_same_file(scope):
    backend = backend_for(scope)
    written = backend.write("/workspace/notes.md", "alpha\nbeta\n")
    assert written.error is None, written.error

    listing = backend.ls("/workspace")
    assert listing.error is None, listing.error
    assert [entry["path"] for entry in listing.entries] == ["/workspace/notes.md"]
    assert backend.glob("*.md", "/workspace").matches[0]["path"] == "/workspace/notes.md"
    assert read_text(backend, "/workspace/notes.md") == "alpha\nbeta\n"
    found = backend.grep("beta", "/workspace")
    assert [(row["path"], row["text"]) for row in found.matches] == [("/workspace/notes.md", "beta")]

    # The same committed version backs every native tool, not a second store.
    storage = workspace_backend.CitrusWorkspaceBackend(
        scope.owner_user_id, scope.session_id, scope.run_id).storage
    text, ref = storage.text("/workspace/notes.md")
    assert text == "alpha\nbeta\n"
    assert ref["version"] == 1 and len(ref["sha256"]) == 64


def test_write_refuses_a_version_observed_by_another_run(scope):
    first = backend_for(scope)
    assert first.write("/workspace/report.md", "one").error is None

    other = workspace_backend.CitrusWorkspaceBackend(scope.owner_user_id, scope.session_id, "other-run")
    assert read_text(other, "/workspace/report.md") == "one"
    assert other.write("/workspace/report.md", "two").error is None

    stale = first.write("/workspace/report.md", "three")
    assert stale.error.startswith("stale_revision")
    assert read_text(first, "/workspace/report.md") == "two"
    assert first.write("/workspace/report.md", "three").error is None


def test_edit_requires_a_read_of_the_current_version(scope):
    backend = backend_for(scope)
    assert backend.write("/workspace/report.md", "hello world").error is None

    # A directory listing or search match does not authorize an exact edit.
    assert backend.ls("/workspace").error is None
    assert backend.edit("/workspace/report.md", "world", "there").error.startswith("stale_revision")
    assert read_text(backend, "/workspace/report.md") == "hello world"

    assert backend.edit("/workspace/report.md", "world", "there").error is None
    assert read_text(backend, "/workspace/report.md") == "hello there"
    assert backend.edit("/workspace/missing.md", "a", "b").error == "file_not_found"


def test_observations_are_scoped_to_the_run_and_survive_a_new_backend(scope):
    writer = backend_for(scope)
    assert writer.write("/workspace/keep.txt", "v1").error is None
    assert read_text(writer, "/workspace/keep.txt") == "v1"

    # A new backend instance in the same run still holds the observed version,
    # but another run of the same session must not inherit it.
    assert backend_for(scope).edit("/workspace/keep.txt", "v1", "v2").error is None
    other = workspace_backend.CitrusWorkspaceBackend(scope.owner_user_id, scope.session_id, "other-run")
    assert other.edit("/workspace/keep.txt", "v2", "v3").error.startswith("stale_revision")


def test_delete_is_a_versioned_tombstone(scope):
    backend = backend_for(scope)
    assert backend.write("/workspace/scratch.txt", "temporary").error is None
    assert read_text(backend, "/workspace/scratch.txt") == "temporary"

    assert backend.delete("/workspace/scratch.txt").error is None
    assert backend.read("/workspace/scratch.txt").error == "file_not_found"
    assert backend.ls("/workspace").entries == []
    assert backend.delete("/workspace/scratch.txt").error == "file_not_found"

    # Session bytes are retained; only the logical version is deleted.
    with files._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_files WHERE filename='scratch.txt'").fetchone()[0] == 1
        tombstone = conn.execute("SELECT version FROM workspace_file_tombstones WHERE path='/workspace/scratch.txt'").fetchone()
    assert tombstone["version"] == 2

    # Re-creating after the delete starts a new version above the tombstone.
    assert backend.write("/workspace/scratch.txt", "again").error is None
    assert read_text(backend, "/workspace/scratch.txt") == "again"


def test_readonly_mounts_and_unobserved_delete_are_refused(scope):
    backend = backend_for(scope)
    for path in ("/input/data.txt", "/data/data.csv", "/skills/nature-figure/SKILL.md", "/01_src/src/app.py"):
        result = backend.write(path, "nope")
        assert result.error == "permission_denied", (path, result.error)
        assert backend.delete(path).error == "permission_denied", path
    assert backend.write("/workspace", "x").error is not None
    assert backend.write("/elsewhere/file.txt", "x").error is not None

    assert backend.write("/outputs/staged.md", "staged").error is None
    assert read_text(backend, "/outputs/staged.md") == "staged"
    # Staged output equals unpublished work, and a delete from another run that
    # never observed this version is refused.
    stranger = workspace_backend.CitrusWorkspaceBackend(
        scope.owner_user_id, scope.session_id, "other-run")
    assert stranger.delete("/outputs/staged.md").error.startswith("stale_revision")
    assert backend.delete("/outputs/staged.md").error is None


def test_offloaded_tool_result_paths_are_text_and_readable(scope):
    """SDK tool-result offload names have no extension but stay text-only."""
    backend = backend_for(scope)
    path = "/workspace/large_tool_results/call_abc123"
    assert backend.write(path, "long tool result").error is None
    assert read_text(backend, path) == "long tool result"
    assert backend.write("/workspace/large_tool_results/../escape.txt", "x").error is not None
    assert backend.write("/workspace/large_tool_results", "x").error is not None


def test_offload_paths_allow_a_bounded_larger_result(scope):
    """Offload files get a larger cap; ordinary workspace text keeps 64 KiB."""
    backend = backend_for(scope)
    big = "x" * 200_000
    assert backend.write("/workspace/large_tool_results/call_big", big).error is None
    assert read_text(backend, "/workspace/large_tool_results/call_big") == big
    assert backend.write("/workspace/regular.txt", "y" * 70_000).error is not None


def test_invalid_scope_is_rejected_at_construction():
    for owner, session, run in ((0, "session-name", "run"), (7, "", "run"), (7, "session-name", "")):
        with pytest.raises(ValueError):
            workspace_backend.CitrusWorkspaceBackend(owner, session, run)


def test_graph_exposes_one_native_file_surface(scope):
    """The assembled graph offers the native file tools and no closed ones."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import ToolNode

    from src.agent.deep_agent_runtime import create_framework_agent
    from src.agent.harness import HarnessState
    from src.agent.framework_test_support import ScriptedModel

    async def script(*args, **kwargs):
        raise AssertionError("model must not be called")

    graph = create_framework_agent(
        ScriptedModel(script=script), [], [], InMemorySaver(), HarnessState,
        workspace_scope=(scope.owner_user_id, scope.session_id, scope.run_id))
    names = sorted(name for node in graph.get_graph().nodes.values()
                   if isinstance(node.data, ToolNode) for name in node.data.tools_by_name)
    assert {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"} <= set(names)
    # Without an authenticated identity there is no delegation surface.
    assert "task" not in names
