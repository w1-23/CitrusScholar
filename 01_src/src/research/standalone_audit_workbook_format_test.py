"""Do not publish a macro-capable package as a normal spreadsheet."""
from contextlib import closing
from io import BytesIO
from zipfile import ZipFile

import pytest
from openpyxl import Workbook, load_workbook

from citrus_audit import api
from src.research import delivery_tools, tools


def _workbook():
    buf = BytesIO()
    book = Workbook()
    book.active.append(["sample", "value"])
    book.active.append(["first", 5])
    book.active.append(["second", 6])
    book.save(buf)
    return buf.getvalue()


def _macro_package(normal):
    buf = BytesIO()
    with ZipFile(BytesIO(normal)) as source, ZipFile(buf, "w") as target:
        for entry in source.namelist():
            content = source.read(entry)
            if entry == "[Content_Types].xml":
                content = content.replace(
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    b"application/vnd.ms-excel.sheet.macroEnabled.main+xml")
            target.writestr(entry, content)
        target.writestr("xl/vbaProject.bin", b"synthetic VBA payload")
    return buf.getvalue()


def _report(snapshot):
    return {"schema": "standalone-audit-review/1", "snapshots": [{
        "file_index": 0, "source_name": snapshot.source_name, "source_file_id": "a" * 32,
        "source_sha256": snapshot.source_sha256, "report": snapshot.report,
        "report_sha256": snapshot.report_sha256}]}


def test_xlsm_snapshot_remains_reviewable_but_is_not_published(monkeypatch):
    macro = _macro_package(_workbook())
    snapshot = api.review_bytes(macro, "source.XLSM")
    assert snapshot.report["tables"]
    monkeypatch.setattr(tools, "_save_artifact", lambda *a, **kw: pytest.fail("published mislabeled XLSM"))
    receipts, errors = delivery_tools.publish_audit_workbooks(_report(snapshot), load_source=lambda _: macro)
    assert not receipts and len(errors) == 1 and "XLSM" in errors[0]


def test_standalone_audit_verifies_real_normal_xlsx_before_publish(monkeypatch):
    normal = _workbook()
    snapshot = api.review_bytes(normal, "source.xlsx")
    published = []

    def publish(filename, content, mime, **kwargs):
        published.append((filename, content, mime))
        return "/artifacts/test-download"

    monkeypatch.setattr(tools, "_save_artifact", publish)
    receipts, errors = delivery_tools.publish_audit_workbooks(_report(snapshot), load_source=lambda _: normal)
    assert len(receipts) == 1 and not errors
    filename, content, mime = published.pop()
    assert filename.endswith(".xlsx")
    assert mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    with closing(load_workbook(BytesIO(content))) as book:
        assert book.active["B2"].value == 5

    monkeypatch.setattr(api, "export_workbook", lambda *a, **kw: (_macro_package(normal), {}))
    receipts, errors = delivery_tools.publish_audit_workbooks(_report(snapshot), load_source=lambda _: normal)
    assert not receipts and len(errors) == 1 and not published
