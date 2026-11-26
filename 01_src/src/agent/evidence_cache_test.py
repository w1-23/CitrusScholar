"""Prove reuse through persisted tool and drawing boundaries, not fake hits."""
from contextlib import nullcontext
from io import BytesIO
import json
from pathlib import Path

import pytest

from src.core import jobs, tool_results, tracing
from src.research import result_tools, tools


OWNER = 61
SESSION = "evidence-cache-session-fixture"


@pytest.fixture
def identity(tmp_path, monkeypatch):
    from src import config
    from src.session import files, manager

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "DB_PATH", state / "files.sqlite3")
    monkeypatch.setattr(files, "FILES_ROOT", state / "files")
    monkeypatch.setattr(manager, "DB_PATH", state / "history.sqlite3")
    monkeypatch.setattr(tool_results, "_schema_ready", False)
    monkeypatch.setattr(jobs, "_DB_PATH", state / "jobs.sqlite3")
    monkeypatch.setattr(manager.session_manager, "get_session_owner", lambda sid: OWNER if sid in
                        (SESSION, "evidence-cache-other-session") else None)
    monkeypatch.setattr("src.core.account_leases.account_lease", lambda _owner: nullcontext())
    prior = tracing.get_user_id(), tracing.get_session_id(), tracing.get_job_id()
    tracing.set_user_id(OWNER)
    tracing.set_session_id(SESSION)
    claim = jobs.claim_job(SESSION, "request", owner_user_id=OWNER, client_request_id="request", payload={})
    tracing.set_job_id(claim.job_id)
    try:
        yield files
    finally:
        tracing.set_user_id(prior[0])
        tracing.set_session_id(prior[1])
        tracing.set_job_id(prior[2])


def test_same_page_uses_verified_receipt_before_selecting_twice(identity, monkeypatch):
    def persist(cid, content, name="source", status="complete"):
        assert tool_results.record(tool_call_id=cid, tool_name=name, session_id=SESSION,
                                   request_id="request", status=status, result_text=content)

    persist("original", json.dumps({"value": "abcdef" * 20, "group": "A"}))
    selected, select = [], result_tools._select

    def observe(data, path):
        selected.append(path)
        return select(data, path)

    monkeypatch.setattr(result_tools, "_select", observe)
    args = {"tool_call_id": "original", "fields": ["/value"], "max_chars": 25}
    page = result_tools.read_tool_result.invoke(args)
    persist("first-page", page, "read_tool_result")
    original = json.loads(page)
    replay = json.loads(result_tools.read_tool_result.invoke(args))
    assert replay["status"] == "reused"
    assert selected == ["/value"]
    assert "result_text" not in replay and replay["page"] == original["page"]
    record = replay["evidence_cache"]
    assert record["result_ref"]["tool_call_id"] == "first-page"
    assert record["returned_fields"] == []  # A fragment is not a complete field.
    assert record["content_sha256"] and record["created_at"]

    next_args = {**args, "start_char": original["page"]["next_start_char"],
                 "cursor": original["page"]["next_cursor"]}
    next_page = json.loads(result_tools.read_tool_result.invoke(next_args))
    other_selector = json.loads(result_tools.read_tool_result.invoke({**args, "fields": ["/group"]}))
    assert next_page["status"] == other_selector["status"] == "ok"
    assert next_page["tool_result_ref"] == other_selector["tool_result_ref"] == original["tool_result_ref"]
    assert len({page["readback_page_key"] for page in (next_page, other_selector, original)}) == 3
    persist("wrong-status", json.dumps({"value": [1, 2]}), status="failed")
    failure_page = result_tools.read_tool_result.invoke({**args, "tool_call_id": "wrong-status"})
    assert "readback_page_key" not in json.loads(failure_page)
    missing_page = result_tools.read_tool_result.invoke({**args, "fields": ["/absent"]})
    assert "readback_page_key" not in json.loads(missing_page)

    tracing.set_session_id("evidence-cache-other-session")
    assert json.loads(result_tools.read_tool_result.invoke(args))["reason"] == "result_unavailable"
    tracing.set_session_id(SESSION)
    tracing.set_user_id(OWNER + 1)
    assert json.loads(result_tools.read_tool_result.invoke(args))["reason"] == "result_access_denied"


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_inspect_audit_and_draw_only_selected_frozen_fields(identity, monkeypatch, kind):
    import citrus_audit
    import openpyxl
    from src.figures.dataset_codec import decode_frame
    from src.figures.projects import ProjectError, script_replay_inputs, snapshot_script_inputs
    from src.research import audit_review_adapter
    from src.agent import evidence_cache

    files = identity
    if kind == "csv":
        source = b"group,value,exclude\nA,12,unused\nB,24,unused\n"
    else:
        workbook = openpyxl.Workbook()
        for row in [("group", "value", "exclude"), ("A", 12, "unused"), ("B", 24, "unused")]:
            workbook.active.append(row)
        stream = BytesIO()
        workbook.save(stream)
        source = stream.getvalue()
    entry = files.register_file(session_id=SESSION, owner_user_id=OWNER, filename="sample." + kind,
                                content=source, mime="application/octet-stream", origin="uploaded")
    file_id = entry["file_id"]
    review_calls = []
    review_sheet = citrus_audit.review_sheet

    def run_one_audit(*args, **kwargs):
        review_calls.append(1)
        return review_sheet(*args, **kwargs)

    freeze_exceptions = []
    freeze = evidence_cache.publish_audit_dataset

    def observe_projection(*args, **kwargs):
        try:
            return freeze(*args, **kwargs)
        except Exception as exc:
            freeze_exceptions.append(repr(exc))
            raise

    monkeypatch.setattr(evidence_cache, "publish_audit_dataset", observe_projection)
    monkeypatch.setattr(citrus_audit, "review_sheet", run_one_audit)
    monkeypatch.setattr(audit_review_adapter, "text_only", lambda _: True)
    inspection = tools.inspect_uploaded_table.invoke({"file_ids_json": [file_id]})
    assert json.loads(inspection)
    content = tools.audit_uploaded_data.invoke({"file_ids_json": [file_id], "checks_json": {
        "requested_checks": ["duplicate_row"], "plot_fields": ["value"]}})
    result = json.loads(content)
    assert result["_tool_outcome"]["status"] == "complete", result
    assert "plot_datasets" in result, (result, freeze_exceptions)
    record, = result["plot_datasets"]
    assert record["source_file_id"] == file_id and record["source_sha256"] == entry["sha256"]
    assert record["returned_fields"] == ["value"] and "values" not in record
    assert record["dataset_ref"]["kind"] == "dataset_snapshot"

    original = Path(files.get_account_file(file_id, OWNER, touch=False)["path"])
    original.unlink()  # Subsequent steps cannot possibly read the original CSV/XLSX.
    with pytest.raises(ProjectError, match="会话"):
        snapshot_script_inputs(record["inputs_binding"], OWNER, "evidence-cache-other-session")
    frozen, refs = snapshot_script_inputs(record["inputs_binding"], OWNER, SESSION)
    _, worker_paths, _ = script_replay_inputs({"input_snapshots": frozen}, OWNER)
    frame = decode_frame(json.loads(Path(worker_paths["data"]).read_text()))
    assert list(frame.columns) == ["value"] and list(frame.index) == [2, 3]
    assert [int(value) for value in frame["value"]] == [12, 24]
    assert record["dataset_ref"] in refs and review_calls == [1]



def test_fusion_workbook_frozen_plot(identity, monkeypatch):
    import openpyxl
    from src.agent import fusion_tools, fusion_worker
    from src.agent.identity import ExecutionIdentity
    from src.figures.dataset_codec import decode_frame
    from src.figures.projects import script_replay_inputs, snapshot_script_inputs

    files = identity
    who = ExecutionIdentity(OWNER, SESSION, "request", tracing.get_job_id())
    monkeypatch.setattr("src.core.account_leases.account_lease", lambda _owner, **kw: nullcontext())
    book = openpyxl.Workbook()
    book.active.title = "Trials"
    for row in [("group", "value", "extra"), ("A", 12, "not selected"), ("B", 24, "not selected")]:
        book.active.append(row)
    stream = BytesIO()
    book.save(stream)
    book.close()
    data = stream.getvalue()
    entry = files.register_file(session_id=SESSION, owner_user_id=OWNER, filename="sample.xlsx",
                                content=data, mime="application/octet-stream", origin="uploaded")
    ref = {key: entry[key] for key in ("file_id", "sha256")}
    region = {"sheet": "Trials", "range": "A1:C3", "id": "main", "header_row": 1}
    eid = "e" * 32
    requests = []
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (who, "fixture", None))
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda *_: (data, "sample.xlsx"))
    monkeypatch.setattr(fusion_worker, "schedule_fusion", lambda _, **kw:
        (requests.append(kw), {"execution_id": eid, "status": "queued"})[1])
    with fusion_tools.fusion_delivery_scope(fusion_tools.FusionDelivery(), query="确认 Trials!A1:C3，表头第1行"):
        _, queued = fusion_tools.run_external_skill.func("data-audit", {"workbook": ref},
            {"regions": [region], "plot_fields": ["value"]})
    assert queued["input_contract"]["plot_projection"]["status"] == "pending_audit"
    request, = requests
    assert request["options"] == {"regions": [region]}
    assert b"not selected" not in request["plot_projection"]
    result = {"status": "succeeded", "inputs": {"workbook": ref}, "options": request["options"],
              "report": {"status": "partial", "coverage": [{"check": "eda_profile", "status": "checked",
                         "region_id": "main"}]},
              "receipt": {"execution_id": eid, "process_stopped": True, "network_policy": "offline"},
              "_plot_projection": request["plot_projection"], "_account_epoch": 1}
    monkeypatch.setattr(fusion_worker, "consume_fusion", lambda *a, **kw: result)
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda *_: pytest.fail("unexpected source reread"))
    assert "plot_datasets" not in fusion_tools.collect_external_skill.func(eid, "data-audit")[1]
    result["report"]["status"] = "complete"
    projection = result.pop("_plot_projection")
    result["_plot_projection_unavailable"] = True
    _, degraded = fusion_tools.collect_external_skill.func(eid, "data-audit")
    assert degraded["_tool_outcome"]["status"] == "complete"
    assert degraded["plot_projection"]["status"] == "unavailable" and "plot_datasets" not in degraded
    result.pop("_plot_projection_unavailable")
    result["_plot_projection"] = projection
    body, public = fusion_tools.collect_external_skill.func(eid, "data-audit")
    assert "_plot_projection" not in body and "not selected" not in body
    record, = public["plot_datasets"]
    assert record["source_sha256"] == ref["sha256"] and record["returned_fields"] == ["value"]
    Path(files.get_account_file(ref["file_id"], OWNER, touch=False)["path"]).unlink()
    frozen, _ = snapshot_script_inputs(record["inputs_binding"], OWNER, SESSION)
    _, paths, _ = script_replay_inputs({"input_snapshots": frozen}, OWNER)
    frame = decode_frame(json.loads(Path(paths["data"]).read_text()))
    assert list(frame.columns) == ["value"] and list(frame["value"]) == [12, 24]
