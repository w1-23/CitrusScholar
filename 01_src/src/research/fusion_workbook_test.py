from contextlib import closing, nullcontext
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sqlite3
from zipfile import ZipFile

import openpyxl
from openpyxl.comments import Comment
import pytest

from src.research.fusion_workbook import publish_fusion_workbook


PRIMARY = "Pilot's! data"
SESSION = "fusion-workbook-test"
OWNER = 117


def _source():
    book = openpyxl.Workbook()
    first = book.active
    first.title = PRIMARY
    first.merge_cells("A1:D1")
    first["A1"] = "Original experiment"
    first["B4"], first["C4"], first["D4"] = "Treatment", "Brix", "Calculated"
    first["B5"], first["C5"], first["D5"] = "CK", 12.5, "=1+2"
    first["C5"].number_format = "0.00"
    first["B6"], first["D6"] = "T1", "=#REF!"
    first["D6"].comment = Comment("Original\nformula note", "Reviewer")
    first["B5"].comment = Comment("Keep this note verbatim", "Original analyst")
    first.row_dimensions[6].hidden = True
    first.column_dimensions["C"].hidden = True
    first["F1"], first["G1"] = "Units", "Other"
    first.merge_cells("F2:G2")
    first["F2"], first["F3"], first["G3"] = "merged", 8, 9
    book.create_sheet("Secondary").sheet_state = "hidden"
    second = book["Secondary"]
    second["E1"], second["F1"], second["E2"], second["F2"] = "Group", "Reading", "A", 50
    original_summary = book.create_sheet("审查结果")
    original_summary.sheet_state = "veryHidden"
    original_summary["A1"] = "User's original sheet"
    intermediate, output = BytesIO(), BytesIO()
    book.save(intermediate)
    with ZipFile(intermediate) as package, ZipFile(output, "w") as source:
        for member in package.infolist():
            data = package.read(member)
            if member.filename == "xl/worksheets/sheet1.xml":
                assert b"<f>1+2</f><v></v>" in data
                data = data.replace(b"<f>1+2</f><v></v>", b"<f>1+2</f><v>3</v>")
            source.writestr(member, data)
    return output.getvalue()


def _report(digest):
    def loc(cells=None, region="brix", sheet=PRIMARY, **other):
        return dict(source_file_hash=digest, sheet_name=sheet, region_id=region, cell_range=cells, **other)

    def found(rule, *locations, severity="review", **other):
        return dict(finding_id=sha256(rule.encode()).hexdigest()[:24], rule_id=rule,
                    locations=list(locations), severity=severity, **other)

    return {
        "status": "partial",
        "execution": {"skill": "data-audit", "inputs": [{"role": "workbook", "path": "book.xlsx", "sha256": digest}],
            "options": {"regions": [
                {"id": "brix", "sheet": PRIMARY, "range": "B4:D6"},
                {"id": "merge", "sheet": PRIMARY, "range": "F1:G3"},
                {"id": "other", "sheet": "Secondary", "range": "E1:F2"}]}},
        "findings": [
            found("required", loc("$c$6", row_index=6, column_index=3), severity="error", observed_value="None"),
            found("BROKEN_REFERENCE", loc("D6"), evidence={"detector": "spreadsheet-auditor", "formula": "=#REF!"}),
            found("formula_cache_unavailable", loc("D5"), severity="coverage"),
            found("biological_unit_count", loc(scope="group", group="CK"), observed_value=1, expected_value=3),
            found("sheet_only", loc("B5", scope="worksheet")),
            found("locationless"),
            found("outside", loc("F3")),
            found("conflicting", loc("B5", row_index=6, column_index=2)),
            found("merged_placeholder", loc("G2", region="merge")),
            found("cross_sheet", loc("F3:G3", region="merge"), loc("F2", region="other", sheet="Secondary")),
        ],
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from src.core import publication_reconcile, tracing
    from src.research import tools
    from src.session import files
    from src.session.manager import session_manager

    request = ["fusion-first-turn"]
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "DB_PATH", state / "session_files.sqlite3")
    monkeypatch.setattr(files, "FILES_ROOT", state / "session_files")
    monkeypatch.setattr(tools, "get_user_id", lambda: OWNER)
    monkeypatch.setattr(tools, "get_session_id", lambda: SESSION)
    monkeypatch.setattr(tracing, "get_user_id", lambda: OWNER)
    monkeypatch.setattr(tracing, "get_client_request_id", lambda: request[0])
    monkeypatch.setattr(tracing, "get_current_tool_name", lambda: "run_fusion_skill")
    monkeypatch.setattr("src.core.account_leases.account_lease", lambda _owner: nullcontext())
    monkeypatch.setattr(publication_reconcile, "_owner_valid", lambda sid, owner: (sid, owner) == (SESSION, OWNER))
    monkeypatch.setattr(session_manager, "get_session_owner", lambda sid: OWNER if sid == SESSION else None)
    return request, state


def test_real_xlsx_and_registration_dedupe(tmp_path, registry):
    request, state = registry
    original = tmp_path / "book.xlsx"
    source = _source()
    original.write_bytes(source)
    digest = sha256(source).hexdigest()
    report = _report(digest)
    source_fields = dict(source_file_id="a" * 32, source_sha256=digest, source_name=original.name, source_bytes=source)
    first = publish_fusion_workbook(report, **source_fields)
    request[0] = "fusion-retry-turn"
    repeated = deepcopy(report)
    repeated["findings"].reverse()
    repeated["findings"][0]["locations"].reverse()
    retry = publish_fusion_workbook(repeated, **source_fields)
    assert first == retry and first["cells"] == 5
    assert first["filename"] == "book_审查标注.xlsx"
    assert first["source_file_id"] == "a" * 32 and first["source_sha256"] == digest
    artifact_id = first["url"].rsplit("/", 1)[-1]
    with closing(sqlite3.connect(state / "research_artifacts.sqlite3")) as db:
        ((path, key, version),) = db.execute("SELECT path,logical_task_id,version FROM artifacts")
        assert key.startswith("audit_workbook:") and version == 1
        assert db.execute("SELECT state FROM publication_outbox").fetchall() == [("committed",)]
    with closing(sqlite3.connect(state / "session_files.sqlite3")) as db:
        assert db.execute("SELECT artifact_id,sha256 FROM session_files").fetchall() == [(artifact_id, first["sha256"])]
        assert db.execute("SELECT client_request_id FROM session_file_turn_refs ORDER BY client_request_id").fetchall() == [
            ("fusion-first-turn",), ("fusion-retry-turn",)]
    content = Path(path).read_bytes()
    assert sha256(content).hexdigest() == first["sha256"] and len(content) == first["size_bytes"]
    assert original.read_bytes() == source
    with BytesIO(content) as stream:
        workbook = openpyxl.load_workbook(stream)
        assert workbook.sheetnames == [PRIMARY, "Secondary", "审查结果", "审查结果_2"]
        assert workbook["Secondary"].sheet_state == "hidden"
        assert workbook["审查结果"].sheet_state == "veryHidden"
        assert workbook["审查结果"]["A1"].value == "User's original sheet"
        sheet = workbook[PRIMARY]
        assert sheet.row_dimensions[6].hidden and sheet.column_dimensions["C"].hidden
        assert {str(area) for area in sheet.merged_cells.ranges} == {"A1:D1", "F2:G2"}
        assert sheet["C5"].value == 12.5 and sheet["C5"].number_format == "0.00"
        assert sheet["D5"].value == "=1+2" and sheet["D6"].value == "=#REF!" and sheet["C6"].value is None
        assert sheet["B5"].comment.text == "Keep this note verbatim" and sheet["B5"].comment.author == "Original analyst"
        assert sheet["D6"].comment.text.startswith("Original\nformula note\n\nCitrus Audit\n")
        assert sheet["D6"].comment.author == "Reviewer"
        for current, annotated in ((sheet, ["C6", "D6", "F3", "G3"]), (workbook["Secondary"], ["F2"])):
            for address in annotated:
                assert current[address].comment.text and current[address].fill.fgColor.rgb.endswith("FDE2E1")
        for address in ("B5", "D5", "B6", "F2", "G2"):
            assert sheet[address].fill.patternType is None
        for address in ("D5", "B6", "F2", "G2"):
            assert sheet[address].comment is None
        summary = workbook["审查结果_2"]
        assert "B4:D6" in summary["A1"].value and "partial" in summary["A1"].value
        assert list(summary.values)[1] == ("位置", "类型", "结论")
        locations = {row[1].value: row[0] for row in list(summary.rows)[2:]}
        assert locations["必填值缺失"].hyperlink.target == "#'Pilot''s! data'!C6"
        assert "Secondary!F2" in locations["cross_sheet"].value and "F3:G3" in locations["cross_sheet"].value
        assert "CK" in locations["生物学重复数不符"].value and locations["生物学重复数不符"].hyperlink is None
        assert "整张工作表" in locations["sheet_only"].value and locations["sheet_only"].hyperlink is None
        assert "未提供精确位置" in locations["locationless"].value
        assert "未能绑定已审范围" in locations["outside"].value and locations["outside"].hyperlink is None
        assert "公式缓存不可用" in locations
        workbook.close()
    cached = openpyxl.load_workbook(BytesIO(content), data_only=True)
    assert cached[PRIMARY]["D5"].value == 3
    cached.close()


@pytest.mark.parametrize("fault", ["bad bytes", "bad input receipt", "bad finding hash", "failed report"])
def test_unmatched_or_failed_report_is_never_published(fault, monkeypatch):
    from src.research import tools

    def unexpected(*_args, **_kwargs):
        pytest.fail("A mismatched or failed report reached publication")

    monkeypatch.setattr(tools, "_save_artifact", unexpected)
    data = _source()
    digest = sha256(data).hexdigest()
    report = _report(digest)
    if fault == "bad bytes":
        data += b"changed"
    elif fault == "bad input receipt":
        report["execution"]["inputs"][0]["sha256"] = "0" * 64
    elif fault == "bad finding hash":
        report["findings"][0]["locations"][0]["source_file_hash"] = "0" * 64
    else:
        report["status"] = "failed"
    with pytest.raises(ValueError):
        publish_fusion_workbook(report, source_file_id="a" * 32, source_sha256=digest,
                                source_name="book.xlsx", source_bytes=data)


def test_csv_derived_marks_only_verified_record_positions_and_keeps_literal_text(registry):
    source = b'column1,column2\r\n"multi\nline",42\r\n=1+2,42\r\nline,42\r\nline,42\r\nunrelated,5\r\n'
    digest = sha256(source).hexdigest()
    def location(scope, **positions):
        return {"scope": scope, "source_file_hash": digest, **positions}
    report = {"status": "complete", "execution": {"skill": "research-integrity",
              "inputs": [{"role": "table", "sha256": digest}], "options": {"checks": ["numeric"]}},
              "findings": [
                  {"finding_id": "exact", "rule_id": "duplicate_numeric_block", "severity": "review",
                   "evidence": {"interpretation": "Review repetition"}, "locations": [
                      location("csv_range", row_index=2, end_row_index=5, column_index=2),
                      location("csv_range", row_index=5, end_row_index=100, column_index=1)]},
                  {"finding_id": "column", "rule_id": "terminal_digits", "severity": "review", "locations": [
                      location("column", column_index=1)]},
              ]}
    receipt = publish_fusion_workbook(report, source_file_id="0" * 32, source_sha256=digest,
                                      source_name="data.csv", source_bytes=source)
    assert receipt["source_format"] == "csv" and "CSV派生" in receipt["filename"] and receipt["cells"] == 4
    with closing(sqlite3.connect(registry[1] / "research_artifacts.sqlite3")) as db:
        ((path,),) = db.execute("SELECT path FROM artifacts")
    workbook = openpyxl.load_workbook(path)
    sheet, summary = workbook["CSV 原始记录"], workbook["审查结果"]
    assert sheet["A2"].value == "multi\nline"
    assert sheet["A3"].value == "=1+2" and sheet["A3"].data_type == "s"
    for cell in ("B2", "B3", "B4", "B5"):
        assert sheet[cell].fill.fgColor.rgb.endswith("FDE2E1") and "Review repetition" in sheet[cell].comment.text
    for cell in ("A2", "A4", "A5", "B6"):
        assert sheet[cell].fill.patternType is None and sheet[cell].comment is None
    assert any("CSV 派生副本" == row[1] for row in list(summary.values)[2:])


@pytest.mark.parametrize(("fmt", "query"), [
    ("csv", "把异常清单另存为 CSV"),
    ("json", "把审查结果另存为 JSON"),
    ("html", "导出可下载的审查报告文件"),
    ("docx", "把审查结果另存为 Word"),
    ("pdf", "把审查结果另存为 PDF"),
])
def test_report_publication_registry_accepts_the_actual_requested_file(fmt, query, registry, monkeypatch):
    from src.core import tracing
    from src.core.turn_outcome import artifact_goal_evidence
    from src.graph.task_contract import contract_for_turn
    from src.research import fusion_delivery, tools

    monkeypatch.setattr(tracing, "get_current_tool_name", tracing._current_tool_name.get)
    report = {"status": "complete", "findings": [{"finding_id": "one", "rule_id": "required",
              "severity": "error", "observed_value": None, "expected_value": "有数值", "evidence": {"reason": "缺失"},
              "locations": [{"scope": "cell", "cell_range": "A2"}]}]}
    receipt = {"result": {"file_id": "a" * 32, "sha256": "b" * 64}}
    result = fusion_delivery.publish_report(report, execution_id="c" * 32, skill_id="research-integrity",
                                             fmt=fmt, receipt=receipt)
    validation = tools.validate_artifact_links([result["url"]], session_id=SESSION, owner_user_id=OWNER, request_id=registry[0][0])
    assert validation[0]["valid"] and validation[0]["producer_tool"].startswith("fusion_"), validation
    goals, gaps = artifact_goal_evidence(contract_for_turn(query, has_tables=True).deliverables,
        [{"name": "resolve_external_report", "tool_call_id": "test", "status": "complete", "artifact_validations": validation}],
        {result["url"]})
    assert goals and not gaps


def test_literature_saved_workbook_retains_all_rows_and_unqueried_status(registry, monkeypatch):
    from src.core import tracing
    from src.core.turn_outcome import artifact_goal_evidence
    from src.graph.task_contract import contract_for_turn
    from src.research import fusion_delivery, tools

    monkeypatch.setattr(tracing, "get_current_tool_name", tracing._current_tool_name.get)
    records = [{"record_index": index, "status": "not_found", "submitted": {"doi": f"10.1234/{index}"}}
               for index in range(1, 101)]
    result = fusion_delivery.publish_report({"status": "partial", "records": records}, execution_id="c" * 32,
        skill_id="literature-verification", fmt="xlsx", receipt={"result": {"file_id": "a" * 32, "sha256": "b" * 64}},
        host_coverage={index: "queried" if index < 90 else "not_queried" for index in range(1, 101)})
    validation = tools.validate_artifact_links([result["url"]], session_id=SESSION, owner_user_id=OWNER, request_id=registry[0][0])
    goals, gaps = artifact_goal_evidence(contract_for_turn("核验参考文献并导出 XLSX", has_tables=False).deliverables,
        [{"name": "collect_external_skill", "tool_call_id": "test", "status": "partial", "artifact_validations": validation}],
        {result["url"]})
    assert goals and not gaps and validation[0]["producer_tool"] == "fusion_reference_workbook"
    with closing(sqlite3.connect(registry[1] / "research_artifacts.sqlite3")) as db:
        ((path,),) = db.execute("SELECT path FROM artifacts")
    workbook = openpyxl.load_workbook(path, read_only=True)
    values = iter(workbook["文献逐条核验"].values)
    labels = next(values)
    rows = [dict(zip(labels, row)) for row in values]
    assert len(rows) == result["rows"] == 100
    assert rows[0]["status"] == "not_found" and rows[0]["host_coverage"] == "queried"
    assert rows[-1]["status"] == "not_checked" and rows[-1]["host_coverage"] == "not_queried"
    workbook.close()
