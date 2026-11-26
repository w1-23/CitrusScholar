"""F-03: data-audit speaks two input protocols and never disguises one as the other."""
from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
import json
import sqlite3

import pytest

from src.agent import fusion_tools, fusion_worker, fusion_worker_contract as contract


CSV = (b"sample_id,treatment,value\n"
       b"s1,CK,12.5\n"
       b"s2,CK,12.5\n"
       b"s3,T1,13.0\n")
CSV_DIGEST = sha256(CSV).hexdigest()
FILE_ID = "a" * 32
OWNER = 501
SESSION = "fusion-csv-fixture"


def _ref():
    return {"file_id": FILE_ID, "sha256": CSV_DIGEST}


@pytest.fixture
def tool(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (object(), "fixture-call", tmp_path))
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda identity, ref: (CSV, "trials.csv"))
    monkeypatch.setattr(fusion_worker, "schedule_fusion",
                        lambda _identity, **kwargs: (calls.append(kwargs),
                                                     {"execution_id": "b" * 32, "status": "queued"})[1])
    return calls


@pytest.fixture
def generic(tmp_path, monkeypatch):
    """The real generic audit route: citrus_audit review, saved review, publisher."""
    from src import config
    from src.core import tracing
    from src.research import tools
    from src.session import files
    from src.session.manager import session_manager

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(session_manager, "get_session_owner", lambda _sid: OWNER)
    monkeypatch.setattr(tracing, "get_user_id", lambda: OWNER)
    monkeypatch.setattr(tracing, "get_session_id", lambda: SESSION)
    monkeypatch.setattr(tracing, "get_client_request_id", lambda: "fixture-csv-turn")
    monkeypatch.setattr("src.core.account_leases.account_lease", lambda _owner: nullcontext())
    monkeypatch.setattr(files, "replace_resource_references", lambda *args: None)
    monkeypatch.setattr(files, "release_resource_references", lambda *args: None)
    # The review pins its authorized source; the fixture file must resolve once.
    monkeypatch.setattr(files, "get_account_file",
                        lambda *args, **kwargs: {"path": str(tmp_path / "trials.csv"),
                                                 "size_bytes": len(CSV), "sha256": CSV_DIGEST})
    published = {}
    review_sheet_calls = []
    import citrus_audit

    original_review_sheet = citrus_audit.review_sheet

    def counted_review_sheet(*args, **kwargs):
        review_sheet_calls.append(kwargs)
        return original_review_sheet(*args, **kwargs)

    monkeypatch.setattr(citrus_audit, "review_sheet", counted_review_sheet)

    def register(name, content, mime, *, logical_task_id, **_source):
        url = "/api/v2/session/" + SESSION + "/artifacts/" + sha256(logical_task_id.encode()).hexdigest()[:32]
        assert published.setdefault(url, (name, content, mime)) == (name, content, mime)
        return url

    monkeypatch.setattr(tools, "_save_artifact", register)
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (object(), "fixture-call", tmp_path))
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda identity, ref: (CSV, "trials.csv"))
    # The pinned data-audit 0.1.2 does not declare the csv_table role.
    monkeypatch.setattr(contract, "analyzer_version", lambda release, skill: "0.1.2")
    return {"cv": (state, review_sheet_calls, published),
            "run_review": None}


def test_csv_is_described_as_one_table_and_never_as_a_sheet():
    table = contract.csv_table_contract(CSV, "trials.csv")
    assert table["kind"] == "csv_table" and table["table"] == "single"
    assert table["sheet_name"] is None and "sheets" not in table
    assert table["column_names"] == ["sample_id", "treatment", "value"]
    assert table["header_rows"] == [1]
    assert (table["record_count"], table["column_count"]) == (3, 3)
    assert table["data_range"] == {"first_row": 2, "last_row": 4, "first_field": 1, "last_field": 3}
    assert table["source_sha256"] == CSV_DIGEST
    assert table["positions"] == "logical records and fields"
    assert contract.data_audit_input_kind("trials.csv") == "csv_table"
    assert contract.data_audit_input_kind("trials.XLSM") == "excel_workbook"
    assert contract.data_audit_input_kind("trials.xlsb") is None


def test_workbook_bytes_and_header_defects_are_reported_not_renamed():
    with pytest.raises(ValueError, match="fusion_csv_table_is_a_workbook"):
        contract.csv_table_contract(b"PK\x03\x04binary", "fake.csv")
    ragged = contract.csv_table_contract(b"a,,a\n1,2,3\n4,5\n", "ragged.csv")
    assert ragged["header_defects"] == {"blank_fields": [2], "duplicate_names": ["a"]}
    assert ragged["ragged_records"] == 1
    assert ragged["column_names"] == ["a", "", "a"]  # neither renamed nor invented


def test_csv_is_scheduled_through_the_table_protocol_when_the_analyzer_supports_it(tool, monkeypatch):
    monkeypatch.setattr(contract, "analyzer_version", lambda release, skill: "0.2.0")
    monkeypatch.setattr(contract, "csv_table_supported", lambda skill, version: True)
    body, artifact = fusion_tools.run_external_skill.func("data-audit", {"csv_table": _ref()})
    scheduled, = tool
    assert scheduled["skill_id"] == "data-audit" and scheduled["inputs"] == {"csv_table": _ref()}
    options = scheduled["options"]
    assert "regions" not in options
    assert options["csv_table"]["column_names"] == ["sample_id", "treatment", "value"]
    assert options["csv_table"]["source_sha256"] == CSV_DIGEST
    result = json.loads(body)
    assert result["status"] == "queued" and result["execution_id"] == "b" * 32
    assert result["collect_required"] is True and result["completion_scope"] == "submission_only"
    assert result["input_contract"]["kind"] == "csv_table"
    assert result["input_contract"]["sheet_name"] is None
    assert artifact["_tool_outcome"]["status"] == "complete"


def test_csv_review_runs_through_the_real_generic_audit_route(generic):
    state, review_sheet_calls, published = generic["cv"]
    from src.graph.task_contract import FusionDelivery

    with fusion_tools.fusion_delivery_scope(FusionDelivery(), query="审查这份 CSV 的数据"):
        body, artifact = fusion_tools.run_external_skill.func("data-audit", {"csv_table": _ref()})
    result = json.loads(body)
    # A real citrus_audit review ran on the authorized table bytes.
    assert len(review_sheet_calls) == 1
    assert published == {}  # the host contract asked for text only
    assert result["route"] == "generic_data_audit" and result["review_id"]
    assert result["collect_required"] is False and result["completion_scope"] == "final_report"
    assert "execution_id" not in result and "_code_execution_id" not in artifact
    assert result["review_url"].endswith(result["review_id"])
    assert result["input_contract"]["kind"] == "csv_table" and result["input_contract"]["sheet_name"] is None
    assert result["status"] in {"complete", "partial"}
    assert isinstance(result["findings"], list) and isinstance(result["coverage"], list)
    assert all("sheet_name" not in location
               for finding in result["findings"] for location in finding["locations"])
    assert result["result_refs"] == [{"kind": "integrity_review", "review_id": result["review_id"]}]
    with sqlite3.connect(state / "integrity_reviews.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM integrity_reviews").fetchone()[0] == 1
    assert artifact["_tool_outcome"]["status"] == result["status"]


def test_repeating_the_csv_review_reuses_the_saved_report(generic):
    state, review_sheet_calls, _ = generic["cv"]
    from src.graph.task_contract import FusionDelivery

    with fusion_tools.fusion_delivery_scope(FusionDelivery(), query="审查这份 CSV 的数据"):
        first, _ = fusion_tools.run_external_skill.func("data-audit", {"csv_table": _ref()})
        second, _ = fusion_tools.run_external_skill.func("data-audit", {"csv_table": _ref()})
    # One scan, one saved review: the second call reads the stored snapshot.
    assert len(review_sheet_calls) == 1
    assert json.loads(first)["review_id"] == json.loads(second)["review_id"]
    assert json.loads(second)["findings_total"] == json.loads(first)["findings_total"]
    with sqlite3.connect(state / "integrity_reviews.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM integrity_reviews").fetchone()[0] == 1


def test_requested_annotated_workbook_is_published_from_the_same_review(generic):
    state, review_sheet_calls, published = generic["cv"]
    from src.graph.task_contract import fusion_delivery_for_turn

    query = "审查这份 CSV 并生成标注工作簿"
    with fusion_tools.fusion_delivery_scope(fusion_delivery_for_turn(query, has_tables=True), query=query):
        body, artifact = fusion_tools.run_external_skill.func("data-audit", {"csv_table": _ref()})
    result = json.loads(body)
    assert len(review_sheet_calls) == 1 and len(published) == 1
    receipt, = result["publication_receipts"]
    url, (name, content, mime) = next(iter(published.items()))
    assert receipt["url"] == url and receipt["source_sha256"] == CSV_DIGEST
    assert name.endswith("_审查标注.xlsx") and "spreadsheetml" in mime
    assert content[:2] == b"PK"
    assert artifact["_tool_outcome"]["artifacts"] == [url]


def test_saved_review_projects_into_the_shared_finding_schema():
    table = {"sheet": "", "header_row": 1, "column_numbers": [1, 2, 3],
             "columns": ["sample_id", "treatment", "value"], "row_numbers": [2, 3],
             "source_sha256": CSV_DIGEST, "source_name": "trials.csv", "region_id": ""}
    signal = {"check": "duplicate_row", "severity": "review", "detail": "第 2、3 行完全相同",
              "evidence": {"detector": "duplicate_row", "rows": [2, 3]},
              "row": 2, "column_index": 1, "field": "sample_id"}
    report = {"schema": "standalone-audit-review/1",
              "snapshots": [{"file_index": 0, "source_file_id": FILE_ID, "source_name": "trials.csv",
                             "source_sha256": CSV_DIGEST,
                             "report": {"tables": [{"table": table,
                                                    "result": {"data": {"findings": [signal]}}}]}}]}
    findings, projection = fusion_tools._table_findings(report, CSV_DIGEST, "trials.csv")
    assert projection["schema_version"] == "data-audit-findings/1"
    projected, = findings
    assert projected["rule_id"] == "duplicate_row" and projected["severity"] == "review"
    assert projected["evidence"]["message"] == "第 2、3 行完全相同"
    location, = projected["locations"]
    assert location == {"source_file_hash": CSV_DIGEST, "filename": "trials.csv",
                        "row_index": 2, "end_row_index": 2, "column_index": 1,
                        "end_column_index": 1, "scope": "csv_range"}
    assert "sheet_name" not in location


def test_adapter_frozen_dataset_refs_pass_through_with_plot_fields(monkeypatch, tmp_path):
    from src.graph.task_contract import FusionDelivery
    from src.research import audit_review_adapter, integrity_review

    seen = {}
    dataset = {"dataset_ref": {"dataset_id": "d" * 32}, "fields": ["value"], "sheet": ""}

    def run_review(tables, indexes, validated, *, sheet_name, query):
        seen.update(tables=tables, indexes=indexes, validated=validated,
                    sheet_name=sheet_name, query=query)
        return "{}", {"integrity_review": {"review_id": "e" * 32},
                      "plot_datasets": [dataset],
                      "result_refs": [{"kind": "dataset", "dataset_ref": dataset["dataset_ref"]}],
                      "publication_receipts": [], "coverage": [], "coverage_gaps": [],
                      "_tool_outcome": {"status": "complete", "reason_code": "audit_review_completed",
                                        "artifacts": []}}

    monkeypatch.setattr(audit_review_adapter, "run_review", run_review)
    monkeypatch.setattr(integrity_review, "get_review",
                        lambda owner, review_id: {"report": {"schema": "standalone-audit-review/1",
                                                             "snapshots": []}})
    monkeypatch.setattr(fusion_tools, "_bound", lambda skill: (object(), "fixture-call", tmp_path))
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda identity, ref: (CSV, "trials.csv"))
    monkeypatch.setattr(contract, "analyzer_version", lambda release, skill: "0.1.2")
    with fusion_tools.fusion_delivery_scope(FusionDelivery(), query="审查这份 CSV 的数据"):
        body, _ = fusion_tools.run_external_skill.func(
            "data-audit", {"csv_table": _ref()}, {"plot_fields": ["value", "value"]})
    result = json.loads(body)
    # Frozen by the adapter and passed through unchanged: no second dataset.
    assert result["plot_datasets"] == [dataset]
    assert result["result_refs"] == [
        {"kind": "integrity_review", "review_id": "e" * 32},
        {"kind": "dataset", "dataset_ref": {"dataset_id": "d" * 32}}]
    # An explicit model choice reaches the adapter once, deduplicated.
    assert seen["validated"]["checks"] == {"plot_fields": ["value"]}
    assert seen["sheet_name"] == "" and seen["indexes"] == [0]
    assert seen["tables"] == [{"name": "trials.csv", "data": CSV, "file_id": FILE_ID}]
    assert result["input_contract"]["sheet_name"] is None


def test_role_must_match_the_real_file_and_regions_are_refused_for_a_table(tool, monkeypatch):
    _, mismatch = fusion_tools.run_external_skill.func("data-audit", {"workbook": _ref()})
    assert mismatch["_tool_outcome"]["reason_code"] == "external_data_audit_input_role_mismatch"
    monkeypatch.setattr(contract, "analyzer_version", lambda release, skill: "0.2.0")
    monkeypatch.setattr(contract, "csv_table_supported", lambda skill, version: True)
    _, regions = fusion_tools.run_external_skill.func(
        "data-audit", {"csv_table": _ref()},
        {"regions": [{"id": "one", "sheet": "Sheet1", "range": "A1:D20"}]})
    assert regions["_tool_outcome"]["reason_code"] == "external_csv_table_regions_not_allowed"
    assert tool == []


def test_request_builder_rejects_an_invented_sheet_or_an_unbound_hash():
    refs = {"csv_table": {"path": contract.INPUT_ROOT + "csv_table.csv", **_ref(), "size_bytes": len(CSV)}}
    table = {"header_rows": [1], "column_names": ["sample_id"], "source_sha256": CSV_DIGEST}
    raw, _ = contract.make_request("data-audit", refs, {"csv_table": table}, None)
    payload = contract.decode(raw)
    assert payload["inputs"] == [{"role": "csv_table", "path": "csv_table.csv", "sha256": CSV_DIGEST}]
    assert payload["options"]["csv_table"]["source_sha256"] == CSV_DIGEST
    assert contract.input_path("data-audit", "csv_table", "trials.csv") == contract.INPUT_ROOT + "csv_table.csv"
    with pytest.raises(ValueError, match="fusion_csv_table_regions_not_allowed"):
        contract.make_request("data-audit", refs, {"csv_table": table, "regions": []}, None)
    with pytest.raises(ValueError, match="fusion_csv_table_source_mismatch"):
        contract.make_request("data-audit", refs, {"csv_table": {**table, "source_sha256": "0" * 64}}, None)
    with pytest.raises(ValueError, match="fusion_csv_table_header_invalid"):
        contract.make_request("data-audit", refs, {"csv_table": {**table, "header_rows": [3]}}, None)
    with pytest.raises(ValueError, match="fusion_workbook_input_required"):
        contract.make_request("data-audit", {**refs, "workbook": refs["csv_table"]}, {}, None)


def test_multi_sheet_workbook_names_the_unreviewed_sheets_and_its_formula_policy():
    regions = [{"sheet": "Trials", "bounds": "B3:D20", "header_rows": [4], "region_id": "one"}]
    titled = contract.excel_workbook_contract(["Trials", "Raw", "Archive"], regions)
    assert titled["reviewed_sheets"] == ["Trials"]
    assert titled["unreviewed_sheets"] == ["Raw", "Archive"]
    assert titled["regions"] == [{"sheet": "Trials", "range": "B3:D20", "header_rows": [4]}]
    assert titled["formula_policy"] == {"formula_values": "cached", "recalculate": False,
                                        "missing_cache": "coverage_finding",
                                        "macro_safety": "reject_signed_activex_embedded"}
    assert "未审查" in titled["unreviewed_scope"]


def test_xlsm_macro_safety_is_checked_before_any_worker_schedule(tool, monkeypatch):
    from src.research.fusion_workbook_test import _macro_package, _source

    active = _macro_package(_source(), extra="xl/activeX/activeX1.bin")
    monkeypatch.setattr(fusion_tools, "_workbook_source", lambda identity, ref: (active, "sample.xlsm"))
    _, artifact = fusion_tools.run_external_skill.func("data-audit", {"workbook": _ref()})
    assert artifact["_tool_outcome"]["reason_code"] == "external_workbook_format_unsupported"
    assert tool == []


def test_repeated_export_reuses_the_saved_report_without_rerunning_the_review(tool, monkeypatch, tmp_path):
    from src.graph.task_contract import fusion_delivery_for_turn
    from src.research import tools

    report = {"status": "complete",
              "findings": [{"finding_id": "f1", "rule_id": "duplicate_numeric_block", "severity": "review",
                            "observed_value": "12.5", "expected_value": None, "evidence": {"length": 2},
                            "locations": [{"scope": "csv_range", "row_index": 2, "end_row_index": 3,
                                           "column_index": 3, "source_file_hash": CSV_DIGEST}]}]}
    value = {"status": "succeeded", "report": report,
             "receipt": {"result": {"file_id": FILE_ID, "sha256": CSV_DIGEST}}}
    monkeypatch.setattr(fusion_worker, "consume_fusion", lambda *args, **kwargs: value)
    monkeypatch.setattr(fusion_worker, "schedule_fusion",
                        lambda *_a, **_k: pytest.fail("an export reran the review"))
    published = {}

    def save(name, content, mime, **options):
        # One logical target keeps one artifact; identical bytes reuse it.
        return published.setdefault(options["logical_task_id"],
                                    "/api/v2/session/fixture/artifacts/" + sha256(content).hexdigest()[:32])

    monkeypatch.setattr(tools, "_save_artifact", save)
    query = "把异常清单另存为 CSV"
    urls = []
    with fusion_tools.fusion_delivery_scope(fusion_delivery_for_turn(query, has_tables=True), query=query):
        for _ in range(2):
            body, artifact = fusion_tools.collect_external_skill.func("c" * 32, "research-integrity")
            urls.append(json.loads(body)["saved_report"]["url"])
            assert artifact["_tool_outcome"]["artifacts"] == [urls[-1]]
    assert urls[0] == urls[1] and len(published) == 1
    assert json.loads(body)["saved_report"]["filename"].endswith(".csv")


def test_csv_rejects_sheet_regions_on_generic_route(generic):
    _, calls, _ = generic["cv"]
    _, result = fusion_tools.run_external_skill.func(
        "data-audit", {"csv_table": _ref()}, {"regions": [{"sheet": "Sheet1", "range": "A1:C4"}]})
    assert result["_tool_outcome"]["reason_code"] == "external_csv_table_regions_not_allowed"
    assert not calls
