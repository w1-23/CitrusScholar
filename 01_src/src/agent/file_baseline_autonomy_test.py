"""The real typed File tools run without a Skill and keep host recovery evidence."""
import asyncio
import hashlib
import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agent import workspace_tools
from src.agent.batch_journal import require_unclaimed_call
from src.agent.capabilities import CapabilityCatalog
from src.agent.checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
from src.agent.harness import HarnessRuntime
from src.agent.host_tools import HostTools
from src.agent.identity import ExecutionIdentity
from src.agent.runtime import AdmissionStopped, FinalCheck, RunLimits
from src.core import cancel, jobs, progress_bus, task_events, tool_results, tracing
from src.core.resource_refs import pin_inputs
from src.session import files, manager
from src.tools import registry


@pytest.fixture
def file_run(tmp_path, monkeypatch):
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(jobs, "_DB_PATH", db)
    monkeypatch.setattr(manager, "DB_PATH", db)
    monkeypatch.setattr(tool_results, "_schema_ready", False)
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "files.db")
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "files")
    monkeypatch.setattr(registry, "_TOOL_SPECS", {})
    monkeypatch.setattr(registry.settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(registry.settings, "PERMISSION_MODE", "deny")
    claim = jobs.claim_job("file-session", "request", owner_user_id=7, client_request_id="request", payload={})
    identity = ExecutionIdentity(7, "file-session", "request", claim.job_id)
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


def read_result(tool, **kwargs):
    return json.loads(tool.invoke(kwargs))


def test_input_mount_lists_and_reads_only_this_turns_pinned_text(file_run, tmp_path, monkeypatch):
    def upload(session, filename, owner=7, content=b"original\r\n"):
        return files.register_file(session_id=session, owner_user_id=owner, filename=filename, content=content)

    attached = upload("older-session", "原始数据.csv")
    named = upload("file-session", "(sample).txt")
    unselected = upload("file-session", "unselected.txt")
    foreign = upload("file-session", "foreign.txt", owner=8)
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    delivery_dir = tmp_path / "state" / "artifacts" / "older-session"
    delivery_dir.mkdir(parents=True)

    def deliver(filename, content):
        path = delivery_dir / filename
        path.write_bytes(content)
        return files.register_file(session_id="older-session", owner_user_id=7, filename=filename,
            storage_path=str(path), origin=files.ORIGIN_GENERATED)

    report = deliver("report.txt", b"reference text")
    workbook = deliver("published.xlsx", b"PK fixture")
    pin_inputs(7, "task_input", file_run.run_id, [item["file_id"] for item in (attached, named, report, workbook)])
    pin_inputs(8, "task_input", file_run.run_id, [foreign["file_id"]])
    pin_inputs(7, "task_input", "different-job", [unselected["file_id"]])

    attached_path = f"/input/{attached['file_id']}/原始数据.csv"
    named_path = f"/input/{named['file_id']}/attachment.txt"
    roots = read_result(workspace_tools.workspace_list)["entries"]
    input_root = next(row["path"] for row in roots if row["path"] == "/input/")
    dirs = read_result(workspace_tools.workspace_list, path=input_root)["entries"]
    assert {row["path"].rstrip("/").split("/")[-1] for row in dirs} == {
        item["file_id"] for item in (attached, named, report)}
    attached_dir = next(row["path"] for row in dirs if attached["file_id"] in row["path"])
    discovered = read_result(workspace_tools.workspace_list, path=attached_dir)["entries"]
    assert [row["path"] for row in discovered] == [attached_path]
    report_path = f"/input/{report['file_id']}/report.txt"
    assert read_result(workspace_tools.workspace_read, path=report_path)["text"] == "reference text"
    result = read_result(workspace_tools.workspace_read, path=attached_path, expected_sha256=attached["sha256"])
    assert result["text"] == "original\r\n"
    assert result["ref"] == {"kind": "workspace_input", "path": attached_path, "filename": "原始数据.csv",
                             "file_id": attached["file_id"], "sha256": attached["sha256"],
                             "version": 1, "read_only": True}
    alias = read_result(workspace_tools.workspace_search, path="/data", pattern="original")
    assert {m["ref"]["path"] for m in alias["matches"]} == {
        attached_path.replace("/input/", "/data/"), named_path.replace("/input/", "/data/")}
    assert read_result(workspace_tools.workspace_read, path=named_path)["ref"]["filename"] == "(sample).txt"

    for item in (unselected, foreign, workbook):
        result = read_result(workspace_tools.workspace_read, path=f"/input/{item['file_id']}/{item['filename']}")
        assert result["_tool_outcome"]["status"] == "failed"
    blocked = read_result(workspace_tools.workspace_write, path=attached_path, content="not allowed",
                          expected_version=0, expected_sha256="")
    assert blocked["_tool_outcome"]["status"] == "failed"
    assert read_result(workspace_tools.workspace_read, path=attached_path)["text"] == "original\r\n"
    # Changing the current catalog hash cannot make the old authorization read new bytes.
    with files._connect() as conn:
        conn.execute("UPDATE session_files SET sha256=? WHERE file_id=?", ("a" * 64, attached["file_id"]))
    assert read_result(workspace_tools.workspace_read, path=attached_path)["error"] == "workspace_file_unavailable"


def test_private_file_versions_remain_scoped_and_cancelled_stage_rolls_back(file_run, monkeypatch):
    path = "/outputs/summary.md"
    storage = workspace_tools._storage()
    first = storage.save(path, content="one\r\n", expected_version=0, expected_sha256="")["ref"]
    second = storage.save(path, old_string="one", new_string="two", expected_version=1,
                          expected_sha256=first["sha256"])["ref"]
    assert second["version"] == 2 and second["file_id"] != first["file_id"]
    assert storage.read(path, version=1)["text"] == "one\r\n"
    assert storage.read(path)["text"] == "two\r\n"
    assert files.get_account_file(second["file_id"], 7) is None
    for owner, session_id in ((7, "older-session"), (8, "file-session")):
        with pytest.raises(LookupError, match="workspace_file_unavailable"):
            type(storage)(owner, session_id, file_run.run_id).read(path)
    with pytest.raises(ValueError, match="workspace_version_conflict"):
        storage.save(path, content="stale", expected_version=1, expected_sha256=first["sha256"])

    token = cancel.CancelToken()
    add_pin = files._replace_resource_references_locked

    def cancel_before_commit(*args):
        result = add_pin(*args)
        token.cancel("test_cancelled")
        return result

    monkeypatch.setattr(files, "_replace_resource_references_locked", cancel_before_commit)
    with cancel.bound_token(token), pytest.raises(cancel.ToolCancelled):
        storage.save(path, content="cancelled", expected_version=2, expected_sha256=second["sha256"])
    assert storage.read(path)["ref"] == second
    with files._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_files").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_versions").fetchone()[0] == 2


def test_unsafe_file_write_is_rejected_before_dispatch(file_run):
    async def run():
        catalog = CapabilityCatalog()
        workspace_tools.register_workspace_tools(catalog)
        allowed = workspace_tools.WORKSPACE_TOOL_NAMES
        host = HostTools(file_run, {"query": "Prepare a private note"}, allowed_names=allowed)
        tool = catalog.resolve("workspace_write", allowed)
        for index, path in enumerate(("/etc/secret.txt", "/outputs/published.xlsx", "/workspace/../output.txt")):
            call = {"name": tool.name, "id": f"deny-{index}", "args": {"path": path,
                    "content": "test", "expected_version": 0, "expected_sha256": ""}}
            execution = await host.invoke(tool, call, file_run)
            assert execution.message.artifact["_tool_outcome"]["reason_code"] == "invalid_tool_arguments"
            assert execution.message.additional_kwargs["execution_dispatched"] is False
            assert execution.uncertain is False
        assert not host.dispatched
    asyncio.run(run())


@pytest.mark.parametrize("interrupted", [False, True])
def test_framework_no_skill_file_dispatch_and_replay(tmp_path, file_run, interrupted):
    async def run():
        catalog = CapabilityCatalog()
        workspace_tools.register_workspace_tools(catalog)
        allowed = workspace_tools.WORKSPACE_TOOL_NAMES
        query = "Prepare a private progress note as an intermediate step."
        call = {"name": "workspace_write", "id": "write-note", "args": {
                "path": "/workspace/note.md", "content": "Evidence ready.\n",
                "expected_version": 0, "expected_sha256": ""}}
        schemas = catalog.schemas(allowed)
        assert {row["function"]["name"] for row in schemas} == allowed
        model_calls, dispatches = [], []
        model_refs, restored_refs = [], []
        provider = ("https://example.test/v1", "fixture")
        config = framework_checkpoint_scope(file_run, provider)
        host = HostTools(file_run, {"query": query}, allowed_names=allowed)

        async def model(history, _, *, native_schemas):
            model_calls.append(1)
            assert not {"write_file", "edit_file"} & {row["function"]["name"] for row in native_schemas}
            observed = [m for m in history if isinstance(m, ToolMessage)]
            if not observed:
                return AIMessage("", tool_calls=[call])
            assert [m.tool_call_id for m in observed] == [call["id"]]
            model_refs.append(observed[0].artifact["ref"])
            return AIMessage("Private note staged.")

        async def execute(call, identity):
            require_unclaimed_call(identity, call)
            dispatches.append(call["id"])
            tool = catalog.resolve(call["name"], allowed)
            execution = await host.invoke(tool, call, identity)
            assert execution.operation_id and not execution.uncertain
            message = await host.publish(call, execution, identity)
            if interrupted:
                raise RuntimeError("fixture_crash_after_publication")
            return message, {}

        def restore_observation(message, original_call):
            assert original_call["id"] == call["id"]
            host.restore_observation(message)
            restored_refs.append(message.artifact["ref"])

        async def accept(*args):
            return FinalCheck()

        runtime = HarnessRuntime(model, execute, lambda *_: None, accept, RunLimits(4), schemas,
            export_host=lambda: {"fixture": True}, restore_host=lambda state: None,
            restore_tool_observation=restore_observation, tool_mode=catalog.execution_mode)
        async with open_framework_checkpoint(tmp_path, file_run, provider) as saver:
            pending = runtime.run(file_run, [HumanMessage(query)], saver=saver, checkpoint_config=config)
            if interrupted:
                with pytest.raises(RuntimeError, match="fixture_crash_after_publication"):
                    await pending
            else:
                assert (await pending).status == "complete"
        host = HostTools(file_run, {"query": query}, allowed_names=allowed)
        async with open_framework_checkpoint(tmp_path, file_run, provider) as saver:
            resumed = runtime.run(file_run, [], saver=saver, checkpoint_config=config, resume=True)
            if interrupted:
                with pytest.raises(AdmissionStopped, match="recovery_requires_reconciliation"):
                    await resumed
            else:
                assert (await resumed).status == "complete"
        assert dispatches == [call["id"]]
        assert len(model_calls) == (1 if interrupted else 2)
        result = workspace_tools._storage().read(call["args"]["path"])
        assert result["text"] == call["args"]["content"]
        assert result["ref"]["version"] == 1
        assert result["ref"]["sha256"] == hashlib.sha256(result["text"].encode()).hexdigest()
        assert restored_refs == model_refs == ([] if interrupted else [result["ref"]])
        with task_events.transaction() as conn:
            rows = conn.execute("SELECT status, host_observation, recovery_mode FROM task_operations "
                                "WHERE job_id=? AND tool_call_id=?", (file_run.run_id, call["id"])).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "complete" and rows[0]["host_observation"]
        assert rows[0]["recovery_mode"] == "never"
    asyncio.run(run())



def test_projected_skill_uses_host_pinned_release(tmp_path, file_run, monkeypatch):
    from src.agent.installed_skills import task_release
    from src.agent.workspace_projection import bind_skill_release

    entry = "skills/fixture-method/SKILL.md"
    def install(release, version, headline):
        path = release / entry
        path.parent.mkdir(parents=True)
        payload = "---\nname: fixture-method\ndescription: Test release fixture\n---\n" + headline
        path.write_text(payload)
        (release / "release.json").write_text(json.dumps({"fixture-method": version}))
        (release / "inventory.json").write_text(json.dumps({entry: hashlib.sha256(payload.encode()).hexdigest()}))

    old, new = tmp_path / "old-release", tmp_path / "new-release"
    install(old, "a" * 40, "Old release.")
    install(new, "b" * 40, "New release.")
    state = {}
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(old))
    assert task_release(state, resume=False) == old
    monkeypatch.setenv("CITRUS_SKILL_RELEASE", str(new))
    assert task_release(state, resume=True) == old

    resource = "/" + entry
    def projected_read():
        result = read_result(workspace_tools.workspace_read, path=resource)
        return result["ref"]["skill_version"], result["text"].splitlines()[-1]

    async def from_host(skill_release):
        catalog = CapabilityCatalog()
        workspace_tools.register_workspace_tools(catalog)
        allowed = workspace_tools.WORKSPACE_TOOL_NAMES
        host = HostTools(file_run, {"query": "Read guidance"}, allowed_names=allowed, skill_release=skill_release)
        call = {"id": "guidance", "name": "workspace_read", "args": {"path": resource}}
        execution = await host.invoke(catalog.resolve(call["name"], allowed), call, file_run)
        assert execution.operation_id and not execution.uncertain
        return execution.message.artifact

    result = asyncio.run(from_host(task_release(state, resume=True)))
    assert result["ref"]["skill_version"] == "a" * 40
    assert result["text"].endswith("Old release.")
    assert projected_read() == ("b" * 40, "New release.")
    with bind_skill_release(None):
        assert read_result(workspace_tools.workspace_read, path=resource)["error"] == "workspace_file_unavailable"
