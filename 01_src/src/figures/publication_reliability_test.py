"""F04/F08 regressions on isolated real artifact, file and figure stores."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Barrier

import pytest

from src.figures import nature_plot_project, projects, store, svg_projects
from src.figures.svg_projects_test import _architecture_svg
from src.figures.workbench import artifacts as workbench
from src.figures.workbench.nature_vectors_test import R_SVG
from src.research import artifacts, tools
from src.session import files

OWNER = 73
OTHER = 74
SESSION = "synthetic-publication-session"
REQUEST = "synthetic-actual-request"
EXECUTION = "a" * 32


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from src import auth
    from src.core import account_leases
    from src.session.manager import session_manager

    @contextmanager
    def lease(owner):
        assert owner in {OWNER, OTHER}
        yield owner

    monkeypatch.setattr(account_leases, "account_lease", lease)
    monkeypatch.setattr(auth, "user_by_id", lambda uid: {"active": uid in {OWNER, OTHER}})
    monkeypatch.setattr(session_manager, "get_session_owner", lambda sid: OWNER if sid == SESSION else None)
    monkeypatch.setattr(session_manager, "session_belongs_to", lambda sid, uid: sid == SESSION and uid == OWNER)
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "state" / "session_files")
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "state" / "files.sqlite3")
    monkeypatch.setattr(store, "_STATE_DIR", tmp_path / "state")
    files.init_store()
    tools._db_path()
    return tmp_path


def _create_svg():
    return svg_projects.create_svg_project(owner=OWNER, session_id=SESSION,
                                           svg=_architecture_svg(), title="Synthetic SVG")


def _links(items):
    return [item["artifact_url"] for item in items]


def _verify_hidden(items):
    assert not {item["file_id"] for item in items}.intersection(row["file_id"] for row in files.list_files(SESSION, OWNER))
    for item in items:
        assert files.get_account_file(item["file_id"], OWNER) is None
        assert tools.resolve_artifact(SESSION, item["artifact_id"]) is None
    assert all(not data["valid"] for data in tools.validate_artifact_links(
        _links(items), session_id=SESSION, owner_user_id=OWNER, request_id=REQUEST))


def test_lost_project_cas_never_exposes_staged_files(runtime, monkeypatch):
    created = _create_svg()
    previous = store.get_project(created["figure_id"], OWNER)
    staged = []
    real = artifacts.stage_figure_artifact

    def capture(**kwargs):
        item = real(**kwargs)
        staged.append(item)
        return item

    def lose(*args, **kwargs):
        _verify_hidden(staged)
        assert store.get_project(created["figure_id"], OWNER) == previous
        return None

    monkeypatch.setattr(artifacts, "stage_figure_artifact", capture)
    monkeypatch.setattr(projects, "update_project_committed", lose)
    with pytest.raises(projects.ProjectError) as error:
        svg_projects.apply_svg_project_edits(figure_id=created["figure_id"], owner=OWNER,
            expected_revision=1, edits=[{"op": "set_label", "target": "label", "value": "Edited"}])
    assert error.value.code == "revision_conflict"
    assert len(staged) == 1
    _verify_hidden(staged)
    assert files.get_account_file(staged[0]["file_id"], OWNER, allow_internal=True) is None
    assert len(list((runtime / "state/artifacts" / SESSION).iterdir())) == 1


def test_commit_then_atomic_promotion_recovers_after_interruption(runtime, monkeypatch):
    svg = _architecture_svg().encode()
    png = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    from src.core import deliverable_check
    monkeypatch.setattr(deliverable_check, "validate_deliverable", lambda **kwargs: None)
    with projects.stage_rendered(OWNER, SESSION, "batch", {"svg": svg, "png": png},
                                 request_id=REQUEST) as (entries, publication):
        _verify_hidden(entries.values())
        project = store.create_project(owner_user_id=OWNER, session_id=SESSION, initial_exports=entries,
                                       publication=publication)
        with monkeypatch.context() as patch:
            sha256_file = files._sha256_file
            def interrupted(path):
                if path.name.endswith(".png"):
                    raise OSError("interrupt second promotion entry")
                return sha256_file(path)
            patch.setattr(files, "_sha256_file", interrupted)
            with pytest.raises(projects.ProjectError) as error:
                projects.complete_render_publication(project["figure_id"], OWNER)
            assert error.value.code == "publication_retryable"
            assert projects.list_for_owner(OWNER)[0]["exports"] == {}
        _verify_hidden(entries.values())
    assert projects.get_for_owner(project["figure_id"], OWNER)["exports_json"] == entries
    assert {row["file_id"] for row in files.list_files(SESSION, OWNER)} == {e["file_id"] for e in entries.values()}
    assert all(tools.resolve_artifact(SESSION, item["artifact_id"]) for item in entries.values())
    assert all(files.get_account_file(item["file_id"], OTHER) is None for item in entries.values())


def _publish_raw(blob, execution=EXECUTION):
    return artifacts.publish_artifact(owner=OWNER, session_id=SESSION, filename="plot.svg", content=blob,
        mime="image/svg+xml", producer_tool="run_nature_plot", request_id=REQUEST,
        logical_task_id=execution)


def _collect(raw, execution=EXECUTION):
    return nature_plot_project.open_nature_plot(owner=OWNER, session_id=SESSION, title="Synthetic Nature",
        backend="r", original_exports={"plot.svg": raw}, primary_svg="plot.svg",
        execution_id=execution, request_id=REQUEST)


def test_nature_concurrent_retries_reuse_exact_copy_and_receipt(runtime, monkeypatch):
    raw = _publish_raw(R_SVG)
    assert raw == _publish_raw(R_SVG)
    other_execution = "b" * 32
    other_raw = _publish_raw(R_SVG, other_execution)
    assert other_raw["file_id"] != raw["file_id"]
    with pytest.raises(projects.ProjectError) as error:
        _collect(other_raw)
    assert error.value.code == "plot_original_unverified"

    prepare = svg_projects.create_svg_project
    barrier = Barrier(2)
    def concurrent(**kwargs):
        barrier.wait(15)
        return prepare(**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(svg_projects, "create_svg_project", concurrent)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(_collect, [raw, raw]))
    assert results[0] == results[1] == _collect(raw)
    chosen = results[0]
    assert chosen["publication_state"] == "ready" and store.count_projects(OWNER) == 1
    url = chosen["exports"]["svg"]["artifact_url"]
    [receipt] = tools.validate_artifact_links([url], session_id=SESSION, owner_user_id=OWNER, request_id=REQUEST)
    assert receipt["valid"] and receipt["producer_tool"] == "figure_workspace"
    from src.figures.plot_vectors import plot_objects
    project = store.get_project(chosen["figure_id"], OWNER)
    target = next(target for target, _, _, values in plot_objects(project["source_json"]["svg"])
                  if "edgecolor" in values)
    edited = svg_projects.apply_svg_project_edits(figure_id=chosen["figure_id"], owner=OWNER,
        expected_revision=1, edits=[{"op": "set_attribute", "target": target,
                                     "attribute": "stroke", "value": "#0f3344"}])
    assert edited["render_version"] == 2 and edited["exports"] != chosen["exports"]
    assert _collect(raw) == chosen  # The bound receipt survives a later edit.
    assert files.is_artifact_referenced(chosen["exports"]["svg"]["artifact_id"], OWNER)
    assert files.get_account_file(raw["file_id"], OWNER)["path"].read_bytes() == R_SVG
    assert store.delete_project(chosen["figure_id"], OWNER)
    assert not files.is_artifact_referenced(chosen["exports"]["svg"]["artifact_id"], OWNER)
    assert _collect(raw)["reason_code"] == "plot_workspace_deleted"
    assert store.count_projects(OWNER) == 0


def test_nature_distinguishes_read_only_from_retryable_registration(runtime, monkeypatch):
    unsupported = R_SVG.replace(b"</g></g>", b"<foreignObject/></g></g>")
    raw = _publish_raw(unsupported, "c" * 32)
    result = _collect(raw, "c" * 32)
    assert result["publication_state"] == "read_only" and result["reason_code"] == "plot_svg_unsupported"
    supported = _publish_raw(R_SVG)
    with monkeypatch.context() as patch:
        def unavailable(**kwargs):
            raise OSError("registration offline")
        patch.setattr(artifacts, "stage_figure_artifact", unavailable)
        result = _collect(supported)
        assert result["publication_state"] == "retryable" and result["original_exports"]
        assert store.count_projects(OWNER) == 0
    assert _collect(supported)["publication_state"] == "ready"


@pytest.mark.parametrize("fmt,signature", [("png", b"\x89PNG\r\n\x1a\n"), ("pdf", b"%PDF-1.4\n")])
def test_missing_or_corrupt_current_export_replays_and_only_current_is_pinned(runtime, monkeypatch, fmt, signature):
    from src.core import deliverable_check
    monkeypatch.setattr(deliverable_check, "validate_deliverable", lambda **kwargs: None)
    project = store.create_project(owner_user_id=OWNER, session_id=SESSION, initial_exports={})
    fid, revision = project["figure_id"], project["render_version"]
    calls = []
    def replay(bound, owner, edits, **kwargs):
        calls.append(bound["render_version"])
        return [], [], {}, {fmt: signature + b"synthetic-render"}
    monkeypatch.setattr(projects, "_render_edits", replay)
    first = projects.generate_export(fid, OWNER, fmt, expected_revision=revision)
    original = first["export"]
    assert files.is_artifact_referenced(original["artifact_id"], OWNER)
    assert workbench.capabilities(project, OWNER, replayable=True)[fmt]["state"] == "ready"
    path = files.get_account_file(original["file_id"], OWNER)["path"]
    if fmt == "png":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    assert workbench.capabilities(project, OWNER, replayable=True)[fmt]["state"] == "generatable"
    got = workbench.download(fid, OWNER, revision, fmt, generate=True)
    restored = store.get_revision_export(fid, OWNER, revision, fmt)["export"]
    assert got.body == signature + b"synthetic-render" and calls == [1, 1]
    assert hashlib.sha256(got.body).hexdigest() == restored["sha256"]
    assert original["file_id"] != restored["file_id"]
    assert not files.is_artifact_referenced(original["artifact_id"], OWNER)
    assert files.is_artifact_referenced(restored["artifact_id"], OWNER)
    expired, _ = tools._expire_unreferenced_artifact(SESSION, restored["artifact_id"],
                                 str(files.get_account_file(restored["file_id"], OWNER)["path"]))
    assert not expired
    assert store.update_project(fid, OWNER, expected_revision=revision, render_version=revision + 1)
    assert not files.is_artifact_referenced(restored["artifact_id"], OWNER)
    assert workbench.download(fid, OWNER, revision, fmt).digest == got.digest
    files.get_account_file(restored["file_id"], OWNER)["path"].unlink()
    with pytest.raises(projects.ProjectError):
        workbench.download(fid, OWNER, revision, fmt, generate=True)
    assert calls == [1, 1]


def test_missing_staged_optional_export_does_not_block_project_recovery(runtime, monkeypatch):
    from src.core import deliverable_check
    monkeypatch.setattr(deliverable_check, "validate_deliverable", lambda **kwargs: None)
    project = store.create_project(owner_user_id=OWNER, session_id=SESSION, initial_exports={})
    fid = project["figure_id"]
    render = b"%PDF-1.4\nsynthetic"
    monkeypatch.setattr(projects, "_render_edits", lambda *args, **kwargs: ([], [], {}, {"pdf": render}))
    with monkeypatch.context() as patch:
        def interrupted(*args):
            raise OSError("interrupt optional batch")
        patch.setattr(files, "promote_figure_files", interrupted)
        with pytest.raises(projects.ProjectError) as failure:
            projects.generate_export(fid, OWNER, "pdf", expected_revision=1)
        assert failure.value.code == "publication_retryable"
    damaged = store.get_revision_export(fid, OWNER, 1, "pdf")["export"]
    files.get_account_file(damaged["file_id"], OWNER, allow_internal=True)["path"].unlink()
    assert projects.get_for_owner(fid, OWNER)["render_version"] == 1
    assert workbench.download(fid, OWNER, 1, "pdf", generate=True).body == render
    assert not files.is_artifact_referenced(damaged["artifact_id"], OWNER)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM figure_publications").fetchone()[0] == 0


def test_legacy_current_registered_export_is_pinned_before_ttl(runtime, monkeypatch):
    from src.core import deliverable_check
    monkeypatch.setattr(deliverable_check, "validate_deliverable", lambda **kwargs: None)
    fid = store.create_project(owner_user_id=OWNER, session_id=SESSION, initial_exports={})["figure_id"]
    with projects.stage_rendered(OWNER, SESSION, "legacy", {"pdf": b"%PDF-1.4\nlegacy"}) as (entries, publication):
        entry = entries["pdf"]
        files.promote_figure_files(OWNER, SESSION, "", [entry])
    import json
    with store._connect() as conn:
        payload = {"figure_id": fid, "render_version": 1, "format": "pdf", "export": entry}
        conn.execute("INSERT INTO figure_revision_exports (figure_id,owner_user_id,render_version,format,payload) "
                     "VALUES (?, ?, 1, 'pdf', ?)", (fid, OWNER, json.dumps(payload)))
    path = files.get_account_file(entry["file_id"], OWNER)["path"]
    assert not files.is_artifact_referenced(entry["artifact_id"], OWNER)
    assert tools._expire_unreferenced_artifact(SESSION, entry["artifact_id"], str(path)) == (False, 0)
    assert files.is_artifact_referenced(entry["artifact_id"], OWNER)
    assert store.update_project(fid, OWNER, expected_revision=1, render_version=2)
    assert tools._expire_unreferenced_artifact(SESSION, entry["artifact_id"], str(path)) == (True, 1)


def test_export_cas_after_staging_rejects_changed_revision_without_orphan(runtime, monkeypatch):
    from src.core import deliverable_check
    monkeypatch.setattr(deliverable_check, "validate_deliverable", lambda **kwargs: None)
    fid = store.create_project(owner_user_id=OWNER, session_id=SESSION, initial_exports={})["figure_id"]
    monkeypatch.setattr(projects, "_render_edits", lambda *a, **k: ([], [], {}, {"pdf": b"%PDF-1.4\nfixture"}))
    actual = projects.record_revision_export
    staged = []
    def stale(*args, **kwargs):
        staged.extend(kwargs["publication"][3])
        _verify_hidden(staged)
        assert store.update_project(fid, OWNER, expected_revision=1, render_version=2)
        return actual(*args, **kwargs)
    monkeypatch.setattr(projects, "record_revision_export", stale)
    with pytest.raises(projects.ProjectError) as error:
        projects.generate_export(fid, OWNER, "pdf", expected_revision=1)
    assert error.value.code == "revision_conflict"
    assert staged and store.get_revision_export(fid, OWNER, 1, "pdf") is None
    _verify_hidden(staged)
    assert files.get_account_file(staged[0]["file_id"], OWNER, allow_internal=True) is None


@pytest.fixture
def code_runtime(runtime, monkeypatch):
    from types import SimpleNamespace
    from src import config
    from src.code_execution import results, service
    from src.core import account_leases

    @contextmanager
    def lease(owner):
        assert owner in {OWNER, OTHER}
        yield SimpleNamespace(epoch=1)

    monkeypatch.setattr(config, "PROJECT_ROOT", runtime)
    monkeypatch.setattr(account_leases, "account_lease", lease)
    monkeypatch.setattr(service, "account_lease", lease)
    def acquire(owner, *, expected_epoch):
        assert owner == OWNER and expected_epoch == 1
        return SimpleNamespace(release=lambda: None)
    monkeypatch.setattr(account_leases, "acquire_account_lease", acquire)
    monkeypatch.setattr(results, "acquire_account_lease", acquire)
    return runtime


def _deliver_code_output(runtime, monkeypatch, payload, filename="candidate.svg"):
    from src.agent import code_tools
    from src.agent.identity import ExecutionIdentity
    from src.code_execution.figure_sources import delivery_key
    from src.code_execution.protocol import ExecutionRequest, FileSnapshot
    from src.code_execution.repository import ExecutionRepository
    from src.code_execution.results import OutputBlob, accept_outputs
    from src.code_execution.service import ExecutionService
    from src.workspaces import TaskWorkspaceService

    identity = ExecutionIdentity(OWNER, SESSION, REQUEST, "fixture-run")
    workspace = TaskWorkspaceService(OWNER, SESSION)
    task = "code_fixture"
    current = workspace.create(task_id=task, request_id=task)
    path = "/outputs/" + filename
    request = ExecutionRequest(identity, task, current["workspace_id"], current["revision"], task, task,
        FileSnapshot("/workspace/demo.py", "1" * 32, 1, "2" * 64, 1), outputs=(path,))
    repository = ExecutionRepository(runtime / "state" / "code_executions.sqlite3")
    queued = repository.submit(request)
    eid = queued["execution_id"]
    assert repository.claim("fixture")["execution_id"] == eid
    repository.transition(eid, "fixture", 1, "running")
    repository.transition(eid, "fixture", 1, "collecting")

    def accept():
        return {**accept_outputs(request, execution_id=eid, fence=1, blobs=(OutputBlob(path, payload),),
                                workspace_repository=workspace.repository),
                "request_fingerprint": request.fingerprint, "process_stopped": True}

    receipt = repository.complete_success(eid, "fixture", 1, process_stopped=True, accept_result=accept)
    ref = receipt["files"][0]
    with monkeypatch.context() as patch:
        patch.setattr(code_tools, "_identity", lambda: (identity, "fixture-deliver"))
        patch.setattr(code_tools, "_service", lambda identity: ExecutionService(identity,
            repository=repository, workspace_service=workspace))
        with code_tools.code_execution_context(identity, "fixture-deliver"):
            _, delivered = code_tools._dispatch("deliver", execution_id=eid, path=path)
    assert delivered["_tool_outcome"]["reason_code"] == "code_output_delivered", delivered
    assert delivered["filename"] == filename and delivered["source_sha256"] == ref["sha256"]
    assert delivered["file_id"] != ref["file_id"]
    public = artifacts.publish_artifact(owner=OWNER, session_id=SESSION, filename=filename, content=payload,
        mime=ref["mime_type"], producer_tool="code_deliver", request_id=REQUEST,
        source_file_id=ref["file_id"], source_sha256=ref["sha256"],
        logical_task_id=delivery_key(eid, ref["path"], ref["sha256"]))
    assert delivered["file_id"] == public["file_id"] and delivered["artifact_url"] == public["artifact_url"]
    return eid, ref, public


def test_verified_code_import_identity_and_idempotent_vector_copy(code_runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import figure_workbench_router as router
    from src.figures.workbench import service

    eid, ref, public = _deliver_code_output(code_runtime, monkeypatch, R_SVG)
    assert files.get_account_file(ref["file_id"], OWNER) is None
    app = FastAPI()
    app.include_router(router.router)
    # Only authenticate the synthetic owner. Keep the endpoint, registry, queue,
    # immutable files, derivatives and edit CAS real.
    monkeypatch.setattr(router, "_owner", lambda req: int(req.headers.get("fixture-owner", str(OWNER))))
    from contextlib import nullcontext
    monkeypatch.setattr(router, "_write_scope", nullcontext)
    async def thread(lease, fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(router, "run_account_thread", thread)
    client = TestClient(app)
    endpoint = "/api/v2/figure-workbench/import-svg"
    payload = {"file_id": public["file_id"], "expected_sha256": public["sha256"]}
    assert client.post(endpoint, json={"file_id": public["file_id"]}).status_code == 422
    assert client.post(endpoint, json={**payload, "expected_sha256": "0" * 64}).status_code == 409
    assert client.post(endpoint, json=payload, headers={"fixture-owner": str(OTHER)}).status_code == 404
    assert client.post(endpoint, json={**payload, "file_id": ref["file_id"]}).status_code == 404
    from src.code_execution.figure_sources import delivery_key
    forged = artifacts.publish_artifact(owner=OWNER, session_id=SESSION, filename="another.svg", content=R_SVG,
        mime="image/svg+xml", producer_tool="code_deliver", request_id=REQUEST,
        source_file_id=ref["file_id"], source_sha256=ref["sha256"],
        logical_task_id=delivery_key(eid, ref["path"], ref["sha256"]))
    assert client.post(endpoint, json={**payload, "file_id": forged["file_id"]}).status_code == 409
    assert store.count_projects(OWNER) == 0
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: client.post(endpoint, json=payload), range(2)))
    assert all(reply.status_code == 200 for reply in replies), [reply.text for reply in replies]
    opened = replies[0].json()
    assert opened == replies[1].json() == client.post(endpoint, json=payload).json()
    assert opened["editable"] and opened["origin"] == "code_output" and "backend" not in opened
    assert opened["original_exports"]["candidate.svg"]["file_id"] == public["file_id"]
    fid = opened["figure_id"]
    project = store.get_project(fid, OWNER)
    assert project["source_json"]["nature_plot"]["source_receipt"]["execution_id"] == eid
    assert store.count_projects(OWNER) == 1
    before = service.detail(fid, OWNER, 1)
    shape = next(group for group in before["groups"] if group["pick_ids"])
    service.edit(fid, OWNER, 1, [{"group_id": shape["id"], "property": "edgecolor", "value": "#3399cc"}], save=True)
    assert client.post(endpoint, json=payload).json() == opened
    assert service.detail(fid, OWNER, 2)["hit_manifest"] == before["hit_manifest"]
    source_file = files.get_account_file(public["file_id"], OWNER)["path"]
    assert source_file.read_bytes() == R_SVG
    source_file.write_bytes(R_SVG.replace(b"n = 6", b"n = 7"))
    assert client.post(endpoint, json=payload).status_code == 409
    assert store.count_projects(OWNER) == 1


@pytest.mark.parametrize("payload,filename", [
    (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="9" height="9"/></svg>', "unsupported.svg"),
    (b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aencAAAAASUVORK5CYII=', "raster.png"),
])
def test_noneditable_code_outputs_do_not_create_project(code_runtime, monkeypatch, payload, filename):
    if filename.endswith(".png"):
        from base64 import b64decode
        payload = b64decode(payload)
    _, _, entry = _deliver_code_output(code_runtime, monkeypatch, payload, filename)
    if filename.endswith(".svg"):
        result = nature_plot_project.import_library_svg(OWNER, entry["file_id"], expected_sha256=entry["sha256"])
        assert result["reason_code"] == "plot_svg_unsupported" and not result["editable"]
        assert "workbench_url" not in result
    else:
        with pytest.raises(projects.ProjectError) as error:
            nature_plot_project.import_library_svg(OWNER, entry["file_id"], expected_sha256=entry["sha256"])
        assert error.value.code == "plot_upload_not_svg"
    assert store.count_projects(OWNER) == 0


def test_ordinary_editable_svg_has_exact_editor_link_in_workbench(runtime):
    from src.figures.workbench import service

    original = _create_svg()
    fid = original["figure_id"]
    url = f"/figures/legacy?project={fid}"
    detail = service.detail(fid, OWNER, 1)
    assert original["links"]["workbench_url"] == detail["editor_url"] == url
    assert all(not item["properties"] and not item["pick_ids"] for item in detail["groups"])
    assert detail["hit_manifest"]["objects"] == []
    [editable] = service.list_assets(OWNER, limit=5, offset=0, kind="svg", access="editable")["projects"]
    assert editable["figure_id"] == fid and editable["editor_url"] == url
    assert service.list_assets(OWNER, limit=5, offset=0, kind="svg", access="read_only")["projects"] == []



@pytest.mark.parametrize("failure_first", [False, True])
def test_shared_publisher_receipts_bind_partial_writes_to_exact_files(runtime, monkeypatch, failure_first):
    from contextlib import closing
    import sqlite3

    from src.core import publication_reconcile, tracing
    from src.research.delivery_reconciliation import capture_delivery, delivery_outcome

    monkeypatch.setattr(tools, "get_user_id", lambda: OWNER)
    monkeypatch.setattr(tools, "get_session_id", lambda: SESSION)
    monkeypatch.setattr(tracing, "get_client_request_id", lambda: REQUEST)
    monkeypatch.setattr(tracing, "get_current_tool_name", lambda: "fixture_delivery")
    real = publication_reconcile.register_publication
    first, second = b"a successful report", b"a committed but unregistered report"

    def commit_without_registration():
        with monkeypatch.context() as patch:
            def interrupted(**kwargs):
                raise OSError("registration interrupted")
            patch.setattr(publication_reconcile, "register_publication", interrupted)
            with pytest.raises(ValueError, match="artifact_registration_failed"):
                tools._save_artifact("two.txt", second, "text/plain")

    with capture_delivery("fixture_delivery", "fixture-call"):
        if failure_first:
            commit_without_registration()
        url = tools._save_artifact("one.txt", first, "text/plain")
        if not failure_first:
            commit_without_registration()
        outcome = delivery_outcome("partial", "", [{"url": url}])
    assert outcome["status"] == "unknown"
    one, two = sorted(outcome["files"], key=lambda row: row["filename"])
    assert (one["file_index"], one["filename"], one["status"], one["url"], one["sha256"]) == (
        1 + failure_first, "one.txt", "confirmed", url, hashlib.sha256(first).hexdigest())
    assert (two["file_index"], two["filename"], two["status"], two["url"], two["sha256"]) == (
        2 - failure_first, "two.txt", "committed", "", hashlib.sha256(second).hexdigest())
    with closing(sqlite3.connect(tools._db_path())) as conn:
        pending, = conn.execute("SELECT artifact_id FROM publication_outbox WHERE state='pending'").fetchall()
    from src.core.op_receipt import make_receipt

    receipt = make_receipt("fixture_delivery", "fixture-call", "", {"_tool_outcome": outcome})
    durable_one, durable_two = sorted(receipt["coverage"]["publication"], key=lambda row: row["filename"])
    assert durable_one["artifact_id"] == url.rsplit("/", 1)[-1]
    assert durable_two["artifact_id"] == two["artifact_id"] == pending[0]
    ref = {"db_path": tools._db_path(), "artifact_id": durable_two["artifact_id"], "project_root": runtime}
    scope = (OWNER, SESSION, REQUEST, durable_two["sha256"])
    for wrong in ((OTHER, *scope[1:]), (OWNER, "another-session", *scope[2:]),
                  (*scope[:2], "another-request", scope[3]), (*scope[:3], "f" * 64)):
        with pytest.raises(publication_reconcile.PublicationRejected, match="publication_recovery_scope_mismatch"):
            real(**ref, expected_scope=wrong)
    assert files.list_files(SESSION, OWNER)[0]["artifact_id"] == durable_one["artifact_id"]
    with pytest.raises(publication_reconcile.PublicationRejected, match="publication_recovery_scope_mismatch"):
        real(**ref, expected_scope=scope, client_request_id="another-request")
    restored = real(**ref, expected_scope=scope)
    with monkeypatch.context() as patch:
        def cannot_reregister(**kwargs):
            pytest.fail("confirmed publication must be reused without registration")
        patch.setattr(files, "register_file", cannot_reregister)
        assert real(**ref, expected_scope=scope)["file_id"] == restored["file_id"]
    assert restored["sha256"] == two["sha256"]
    assert outcome["status"] == "unknown"  # historical operation stays unchanged
