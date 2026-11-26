"""Data-only projections from accepted external Skill reports.

The private report is the source of truth. Content here makes no new finding,
reads no additional source, and never exposes private paths or provider bodies.
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
from hashlib import sha256
from html import escape
import io
import json
import re
from urllib.parse import urlsplit


LOCATIONS = ("source_file_id", "source_file_hash", "sheet_name", "region_id", "cell_range", "row_index", "column_index",
             "end_row_index", "end_column_index", "scope", "group", "field", "filename", "pixel_box",
             "hidden_row", "hidden_column", "merged_placeholder")
EVIDENCE = ("detector", "reason", "message", "notes", "formula", "statistics", "interpretation", "byte_match",
            "group", "rows", "distinct_biological_units", "rows_without_unit", "technical_replicate_column",
            "batch_column", "provider", "status", "record_id", "evidence_id", "title", "doi", "url",
            "queried_at", "source_file_hash", "source_id", "source", "sources", "method", "grade", "length", "where", "method_version", "prerequisite")
COVERAGE = ("check", "status", "scope", "record_index", "region_id", "reason", "column", "n", "n_rows",
            "n_images", "method", "detector", "limits", "recalculation", "missing_cache_count", "findings_scope", "applied", "group",
            "rows", "distinct_biological_units", "rows_without_unit", "source_file_hashes", "method_version", "minimum")
RECORD = ("record_index", "status", "evidence_ids", "evidence_grade", "problems", "reason", "flags", "fields_checked",
          "identifier_checks", "retraction_status", "registered", "differences", "candidates", "method", "details_omitted")
ATTEMPT = ("provider", "status", "queried_at", "url", "reason", "http_status", "record_id")
_PRIVATE_KEYS = re.compile(r"(?:^|_)(?:path|paths|body|raw|logs?|headers|secret|token|credentials?|options|password)(?:$|_)", re.I)
_SAFE_FIELD = re.compile(r"[\w .-]{1,64}", re.UNICODE)
_INTERNAL_PATH = re.compile(r"(?<!\w)(?:(?:/workspace|/input|/output|/home|/tmp|/proc|/run|/etc|/var)(?:/[^\s,;\])]+)|[A-Z]:\\[^\s,;\])]+)", re.I)


def _bounded(value, *, depth=0, text=500, items=24):
    if isinstance(value, str):
        value = _INTERNAL_PATH.sub("[private location]", value)
        return value if len(value) <= text else value[:text - 1] + "…"
    if value is None or type(value) in (bool, int, float):
        return value
    if depth >= 3:
        return "[bounded]"
    if isinstance(value, list):
        bounded = [_bounded(item, depth=depth + 1, text=text, items=items) for item in value[:items]]
        return bounded + ([{"omitted_items": len(value) - items}] if len(value) > items else [])
    if isinstance(value, dict):
        safe = (k for k in value if isinstance(k, str) and _SAFE_FIELD.fullmatch(k) and not _PRIVATE_KEYS.search(k))
        return {key: _bounded(value[key], depth=depth + 1, text=text, items=items) for key in list(safe)[:items]}
    return None


def _project(row, fields):
    if not isinstance(row, dict):
        return {}
    result = {key: _bounded(row[key]) for key in fields if key in row}
    if "url" in result:
        url = result.get("url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
                result.pop("url")
        except ValueError:
            result.pop("url")
    return result


def project_finding(finding, *, location_limit=None):
    row = _project(finding, ("finding_id", "rule_id", "severity", "observed_value", "expected_value"))
    row["evidence"] = _project(finding.get("evidence"), EVIDENCE)
    locations = finding.get("locations") or []
    row["locations"] = [_project(location, LOCATIONS) for location in locations[:location_limit]]
    total = finding.get("locations_total")
    if type(total) is not int or total < len(locations):
        total = len(locations)
    if total > len(row["locations"]):
        row["locations_total"] = total
    return row


def _project_record(record, *, attempt_limit=12):
    result = _project(record, RECORD)
    result["submitted"] = _project(record.get("submitted"), ("doi", "arxiv_id", "pmid", "title", "year", "author", "authors", "journal", "venue", "raw_reference"))
    result["attempts"] = [_project(a, ATTEMPT) for a in (record.get("attempts") or [])[:attempt_limit]]
    if record.get("method") == "citrus_refcheck/reference-verification/1":
        fields = ("status", "reason", "registered", "differences", "candidates")
        result["identifier_checks"] = {key: _project(check, fields) for key, check in record.get("identifier_checks", {}).items()
                                       if key in {"doi", "arxiv", "pmid"}}
    return result


def summary(report, *, findings_offset=0, findings_limit=36):
    result = {"status": report["status"], "limitations": _bounded(report.get("limitations") or [], text=300),
              "execution": _project(report.get("execution"), ("skill", "version", "python"))}
    if report.get("reference_batch"):
        result["reference_batch"] = _project(report["reference_batch"], ("schema", "original_report_sha256", "stop_reason",
            "coverage", "distinct_queries", "provider_requests", "elapsed_ms", "status_counts"))
    if type(report.get("findings_omitted")) is int and report["findings_omitted"] > 0:
        result["findings_omitted"] = report["findings_omitted"]
    for key, build in (("findings", lambda f: project_finding(f, location_limit=16)),
                       ("coverage", lambda row: _project(row, COVERAGE)),
                       ("evidence", lambda row: _project(row, EVIDENCE)), ("records", _project_record)):
        rows = report.get(key)
        if not isinstance(rows, list):
            continue
        size = len(rows)
        start = min(findings_offset, size) if key == "findings" else 0
        limit = findings_limit if key == "findings" else 36
        projected = []
        bytes_left = 96 * 1024 if key == "findings" else 20 * 1024
        for row in rows[start:start + limit]:
            projected_row = build(row)
            cost = len(json.dumps(projected_row, ensure_ascii=False, allow_nan=False).encode())
            if cost > bytes_left and projected:
                break
            projected.append(projected_row)
            bytes_left -= cost
        result[key], result[key + "_total"] = projected, size
        if key == "findings":
            stop = start + len(projected)
            result["findings_page"] = {"offset": start, "returned": len(projected),
                                       "next_offset": stop if stop < size else None}
    return result


def annotate_record_coverage(record, by_index):
    status = by_index.get(record["record_index"], "not_queried")
    record["host_coverage"] = status
    if (not record.get("method") and status not in {"queried", "previously_observed"}
            and record.get("status") == "not_found"):
        record["worker_status"] = "not_found"
        record["status"] = "unavailable" if status == "provider_unavailable" else "not_checked"


def _csv_value(value):
    # Export findings as inert cell data, even when opened in a spreadsheet.
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _write_csv(rows, columns, fmt):
    stream = io.StringIO(newline="")
    output = csv.DictWriter(stream, fieldnames=columns, delimiter="\t" if fmt == "tsv" else ",")
    output.writeheader()
    for row in rows:
        output.writerow({column: _csv_value(row.get(column)) if row.get(column) is not None else "" for column in columns})
    return stream.getvalue().encode("utf-8-sig")


def _render(payload, fmt, *, literature):
    title = "文献核验结果" if literature else "外部 Skill 审查结果"
    if fmt in {"csv", "tsv"}:
        if literature:
            fields = (*RECORD, "submitted", "attempts", "host_coverage", "worker_status")
            rows = payload["records"]
        else:
            fields = ("finding_id", "rule_id", "severity", "observed_value", "expected_value", "locations", "locations_total", "evidence")
            rows = payload["findings"]
        return _write_csv(rows, fields, fmt)
    if fmt == "xlsx" and literature:
        from openpyxl import Workbook
        from src.core.ooxml_determinism import normalize_ooxml_bytes
        book = Workbook()
        sheet = book.active
        sheet.title = "文献逐条核验"
        fields = (*RECORD, "submitted", "attempts", "host_coverage", "worker_status")
        sheet.append(fields)
        for row_number, row in enumerate(payload["records"], 2):
            for column, field in enumerate(fields, 1):
                value = row.get(field)
                if value is None:
                    continue
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if len(text) > 32767:
                    raise ValueError("external_record_cell_size_limit")
                sheet.cell(row_number, column, text).data_type = "s"
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        notes = book.create_sheet("覆盖说明")
        notes.append(("status", payload["status"]))
        notes.append(("execution_id", payload["execution_id"]))
        notes.append(("report_sha256", payload["report_sha256"]))
        for item in payload["limitations"]:
            notes.cell(notes.max_row + 1, 1, str(item)).data_type = "s"
        out = io.BytesIO()
        book.save(out)
        book.close()
        return normalize_ooxml_bytes(out.getvalue())
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode()
    if fmt == "html":
        text = escape(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2))
        return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title></head>'
                f'<body><h1>{title}</h1><pre>{text}</pre></body></html>').encode()
    if fmt == "pdf":
        import fitz
        from textwrap import wrap
        with fitz.open() as pdf:
            document = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
            lines = [title, "", *(part for line in document.splitlines()
                                  for part in (wrap(line, width=49, drop_whitespace=False) or [""]))]
            if len(lines) > 30_000:
                raise ValueError("external_pdf_page_limit")
            for index in range(0, len(lines), 47):
                page = pdf.new_page(width=595, height=842)
                page.insert_text((39, 45), "\n".join(lines[index:index + 47]),
                                 fontsize=10, fontname="china-s", lineheight=1.5)
            return pdf.tobytes(garbage=4, deflate=True, no_new_id=True)
    if fmt == "docx":
        from docx import Document
        from src.core.ooxml_determinism import normalize_ooxml_bytes
        document = Document()
        document.add_heading(title, level=1)
        document.add_paragraph(f"状态 {payload['status']} | 执行 {payload['execution_id']}")
        for key in ("limitations", "coverage", "findings", "records", "evidence"):
            if not payload.get(key):
                continue
            document.add_heading(key, level=2)
            for item in payload[key]:
                document.add_paragraph(json.dumps(item, ensure_ascii=False, allow_nan=False))
        stream = io.BytesIO()
        document.save(stream)
        return normalize_ooxml_bytes(stream.getvalue())
    raise ValueError("external_report_format_unsupported")


_MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv", "tsv": "text/tab-separated-values", "json": "application/json", "html": "text/html", "pdf": "application/pdf",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


@contextmanager
def report_renderer(renderer):
    """The immutable artifact registry records the actual renderer, not a generic executor."""
    from src.core.artifact_kinds import RENDERER_OUTPUT_KINDS
    from src.core.tracing import get_current_tool_name, set_current_tool_name

    if renderer not in RENDERER_OUTPUT_KINDS or not renderer.startswith("fusion_"):
        raise ValueError("unknown_fusion_report_renderer")
    previous = get_current_tool_name()
    set_current_tool_name(renderer)
    try:
        yield
    finally:
        set_current_tool_name(previous)


def publish_report(report, *, execution_id, skill_id, fmt, receipt, host_coverage=None):
    """Register one exact derived listing, with all retained finding/record rows."""
    from src.core.cancel import raise_if_cancelled
    from src.research import tools

    raise_if_cancelled("external_skill_report")
    source = receipt["result"]
    literature = skill_id == "literature-verification"
    findings = [project_finding(x) for x in report.get("findings", [])]
    records = [_project_record(x, attempt_limit=None) for x in report.get("records", [])]
    if host_coverage:
        for record in records:
            annotate_record_coverage(record, host_coverage)
    payload = {"execution_id": execution_id, "skill_id": skill_id, "report_sha256": source["sha256"],
               "status": report["status"], "limitations": _bounded(report.get("limitations") or [], items=100),
               "findings_omitted": report.get("findings_omitted", 0),
               "coverage": [_project(x, COVERAGE) for x in report.get("coverage", [])],
               "findings": findings, "evidence": [_project(x, EVIDENCE) for x in report.get("evidence", [])],
               "records": records}
    if report.get("reference_batch"):
        payload["reference_batch"] = summary(report, findings_limit=0)["reference_batch"]
    body = _render(payload, fmt, literature=literature)
    if len(body) > 16 << 20:
        raise ValueError("external_report_export_size_limit")
    checksum = sha256(body).hexdigest()
    filename = ("文献核验结果" if literature else "外部审查发现清单") + "." + fmt
    renderer = {"csv": "reference_table" if literature else "report_table",
                "tsv": "reference_table" if literature else "report_table",
                "json": "reference_json" if literature else "report_json",
                "xlsx": "reference_workbook", "html": "report_html", "docx": "report_docx", "pdf": "report_pdf"}[fmt]
    with report_renderer("fusion_" + renderer):
        url = tools._save_artifact(filename, body, _MIME[fmt], source_file_id=source["file_id"],
                                   source_sha256=source["sha256"],
                                   logical_task_id=f"fusion_report:{execution_id}:{fmt}:{checksum}")
    if not url:
        raise ValueError("external_report_not_registered")
    return {"url": url, "filename": filename, "sha256": checksum, "size_bytes": len(body),
            "source_file_id": source["file_id"], "source_sha256": source["sha256"],
            "file_index": 1, "rows": len(records) if literature else len(findings)}
