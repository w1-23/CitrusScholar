from hashlib import sha256
from io import BytesIO
import sqlite3

import openpyxl

from src.research import audit_review_adapter, delivery_tools, integrity_review, tools
from src.research.delivery_reconciliation import capture_delivery, publication_started


def test_one_multiregion_scan_and_one_workbook_reused_for_export(tmp_path, monkeypatch):
    import citrus_audit
    from src import config
    from src.core import tracing
    from src.session import files, manager

    owner = 101
    session = "remote-acceptance-fixture"
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(manager.session_manager, "get_session_owner", lambda _: owner)
    monkeypatch.setattr(tracing, "get_user_id", lambda: owner)
    monkeypatch.setattr(tracing, "get_session_id", lambda: session)
    monkeypatch.setattr(tracing, "get_client_request_id", lambda: "fixture-multi-region")
    monkeypatch.setattr("src.core.account_leases.account_lease", lambda _: __import__("contextlib").nullcontext())
    monkeypatch.setattr(files, "replace_resource_references", lambda *args: None)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    regions = []
    for index, (row, column) in enumerate(((1, 1), (1, 8), (15, 1), (15, 8), (34, 1), (34, 8))):
        sheet.cell(row, column, f"value{index}")
        sheet.cell(row, column + 1, f"other{index}")
        for offset, values in enumerate(((11, 22), (11, 22), (31, 53)), 1):
            for step, value in enumerate(values):
                sheet.cell(row + offset, column + step, value)
        begin, end = (sheet.cell(row, column).coordinate, sheet.cell(row + 3, column + 1).coordinate)
        regions.append({"file_index": 0, "sheet": "Sheet1", "region_id": f"r{index}",
                        "bounds": begin + ":" + end, "header_rows": [row]})
    sheet["Z5"] = "outside"
    sheet["Z6"] = "=1+2"
    workbook.create_sheet("Unreviewed")["B2"] = "unchanged"
    stream = BytesIO()
    workbook.save(stream)
    source = stream.getvalue()
    digest = sha256(source).hexdigest()
    original = tmp_path / "data.xlsx"
    original.write_bytes(source)
    file_id = "b" * 32
    monkeypatch.setattr(files, "get_account_file", lambda *_args, **_kwargs:
        {"path": str(original), "size_bytes": len(source), "sha256": digest})
    calls, outputs = [], {}
    review_sheet = citrus_audit.review_sheet

    def observe_review(*args, **kwargs):
        calls.append(kwargs)
        return review_sheet(*args, **kwargs)

    def register(name, data, _mime, *, logical_task_id, **_source):
        assert logical_task_id.startswith("audit_workbook:")
        url = "/api/v2/session/" + session + "/artifacts/" + logical_task_id[-32:]
        assert outputs.setdefault(url, (name, data)) == (name, data)
        return url

    monkeypatch.setattr(citrus_audit, "review_sheet", observe_review)
    monkeypatch.setattr(tools, "_save_artifact", register)
    inputs = [{"name": original.name, "data": source, "file_id": file_id}]
    validated = {"checks": {"requested_checks": ["duplicates"]}, "regions": regions, "header_rows": [1]}
    arguments = dict(sheet_name="Sheet1", query="审查 Sheet1 的重复")
    _, first = audit_review_adapter.run_review(inputs, [0], validated, **arguments)
    _, second = audit_review_adapter.run_review(inputs, [0], validated, **arguments)
    assert calls[0]["requested_checks"] == ["duplicate_row"]
    assert first["_tool_outcome"]["status"] == "complete"
    assert len(calls) == 1 and len(first["coverage"]) == 6
    assert first["integrity_review"] == second["integrity_review"]
    receipt, = first["publication_receipts"]
    assert delivery_tools._publish_data_audit(first["integrity_review"]["review_id"], "xlsx").endswith(receipt["url"])
    assert len(outputs) == 1 and original.read_bytes() == source
    name, data = outputs[receipt["url"]]
    assert name == "data_审查标注.xlsx" and sha256(data).hexdigest() == receipt["sha256"]
    exported = openpyxl.load_workbook(BytesIO(data))
    assert exported.sheetnames == ["Sheet1", "Unreviewed", "审查结果"]
    summary = exported["审查结果"]
    rows = list(summary.values)
    header_row = rows.index(("位置", "类型", "结论")) + 1
    finding_row = next(index for index, (_, kind, _) in enumerate(rows[header_row:], header_row + 1)
                       if "重复" in str(kind))
    assert summary.cell(finding_row, 1).hyperlink.target.startswith("#'Sheet1'!")
    assert [exported["Sheet1"][cell].value for cell in ("Z5", "Z6")] == ["outside", "=1+2"]
    with sqlite3.connect(tmp_path / "state" / "integrity_reviews.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM integrity_reviews").fetchone()[0] == 1

    from citrus_audit import api

    def fails_before_write(*_args, **_kwargs):
        raise ValueError("workbook render failed before publication")

    with monkeypatch.context() as fixture:
        fixture.setattr(api, "export_workbook", fails_before_write)
        with capture_delivery("audit_uploaded_data", "render-rejected") as evidence:
            _, rejected = audit_review_adapter.run_review(inputs, [0], validated, **arguments)
        assert evidence.preflight and rejected["_tool_outcome"]["status"] == "not_run"

    def loses_confirmation(*_args, **_kwargs):
        publication_started()
        raise OSError("lost publication confirmation")

    monkeypatch.setattr(tools, "_save_artifact", loses_confirmation)
    with capture_delivery("audit_uploaded_data", "lost-confirmation"):
        _, unknown = audit_review_adapter.run_review(inputs, [0], validated, **arguments)
    assert unknown["_tool_outcome"]["status"] == "unknown" and len(calls) == 1
