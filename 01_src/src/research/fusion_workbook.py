"""Publish a validated external data-audit report on its authorized XLSX copy.

This adapter binds locations and formats evidence. It never evaluates a workbook
rule, substitutes an audit method, or uses a display label to locate cells.
"""
from __future__ import annotations

from collections import defaultdict
import csv
from io import BytesIO, TextIOWrapper
from hashlib import sha256
import json
from pathlib import Path
import re

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils.cell import get_column_letter, range_boundaries


_A1 = re.compile(r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?", re.IGNORECASE | re.ASCII)
_LABELS = {
    "required": "必填值缺失", "numeric": "数值类型不符", "range": "越界", "unique": "重复值",
    "biological_unit_count": "生物学重复数不符", "formula_cache_unavailable": "公式缓存不可用",
    "BROKEN_REF": "公式引用失效", "BROKEN_REFERENCE": "公式引用失效", "MISSING_SHEET": "公式引用工作表缺失",
    "EXTERNAL_LINK": "公式外部链接", "FORMULA_ERROR": "公式错误", "REF_ERROR": "公式引用错误",
    "CIRCULAR_REFERENCE": "公式循环引用", "CIRCULAR_REF": "公式循环引用",
    "duplicate_numeric_block": "重复数值区段（复核线索）", "terminal_digits": "末位数字分布（列级复核线索）",
}


def _a1(value):
    if not isinstance(value, str) or not _A1.fullmatch(value):
        return None
    left, top, right, bottom = range_boundaries(value.upper())
    if not (1 <= left <= right <= 16384 and 1 <= top <= bottom <= 1048576):
        return None
    return left, top, right, bottom


def _address(bounds):
    left, top, right, bottom = bounds
    start, stop = f"{get_column_letter(left)}{top}", f"{get_column_letter(right)}{bottom}"
    return start if start == stop else f"{start}:{stop}"


def _within(inner, outer):
    left, top, right, bottom = inner
    return outer[0] <= left <= right <= outer[2] and outer[1] <= top <= bottom <= outer[3]


def _text(value, limit=500):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    value = " ".join(ILLEGAL_CHARACTERS_RE.sub("", value).split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _review_targets(report, digest, suffix):
    execution = report.get("execution") or {}
    inputs = execution.get("inputs") or []
    if (execution.get("skill") != "data-audit" or len(inputs) != 1
            or inputs[0].get("role") != "workbook" or inputs[0].get("sha256") != digest
            or Path(inputs[0].get("path") or "").suffix.lower() != suffix):
        raise ValueError("Fusion report does not identify the authorized workbook")
    regions = (execution.get("options") or {}).get("regions") or []
    if not 1 <= len(regions) <= 16:
        raise ValueError("Fusion report does not identify its reviewed regions")
    targets, bounds = {}, {}
    for region in regions:
        region_id, sheet = region.get("id"), region.get("sheet")
        extent = _a1(region.get("range"))
        if (not isinstance(region_id, str) or not region_id or region_id in targets
                or not isinstance(sheet, str) or not sheet or extent is None):
            raise ValueError("Invalid Fusion reviewed region")
        left, top, right, bottom = extent
        header = region.get("header_row", top)
        if type(header) is not int or not top <= header <= bottom or (right - left + 1) * (bottom - top + 1) > 50_000:
            raise ValueError("Invalid Fusion reviewed bounds")
        bounds[region_id] = extent
        targets[region_id] = {
            "sheet_name": sheet, "region_id": region_id, "region_bounds": _address(extent),
            "header_row": header, "column_numbers": range(left, right + 1),
            "row_numbers": range(top, bottom + 1), "findings": [],
        }
    return targets, bounds


def _exact_location(location):
    row, column = location.get("row_index"), location.get("column_index")
    raw = location.get("cell_range")
    if raw is None and type(row) is type(column) is int:
        if 1 <= row <= 1048576 and 1 <= column <= 16384:
            return column, row, column, row
    extent = _a1(raw)
    if extent is None:
        return None
    left, top, right, bottom = extent
    if row is not None and (type(row) is not int or row != top or top != bottom):
        return None
    if column is not None and (type(column) is not int or column != left or left != right):
        return None
    return extent


def _project_location(location, digest, targets, bounds):
    source_hash = location.get("source_file_hash")
    if source_hash is not None and source_hash != digest:
        raise ValueError("Fusion finding points to a different source file")
    sheet = location.get("sheet_name") or ""
    region = location.get("region_id") or ""
    scope = location.get("scope") or ""
    label = _text(sheet or "未指定工作表", 50)
    suffix = f"（区域 {_text(region, 100)}）" if region else ""
    if scope == "group":
        group = location.get("group")
        group_label = "未指定" if group is None else _text(group, 120)
        return f"{label} 的组 {group_label}{suffix}", None, None
    if scope in ("sheet", "worksheet", "workbook", "table", "region"):
        area = {"sheet": "整张工作表", "worksheet": "整张工作表", "workbook": "整份工作簿",
                "table": "表格范围", "region": "区域范围"}[scope]
        return f"{label} 的{area}{suffix}", None, None
    extent = _exact_location(location)
    if extent is None or scope not in ("", "cell", "cells", "range"):
        return f"{label} 未提供精确单元格{suffix}", None, None
    address = _address(extent)
    display = f"{label}!{address}"
    if not region:
        matching = [key for key, candidate in targets.items()
                    if candidate["sheet_name"] == sheet and _within(extent, bounds[key])]
        if len(matching) == 1:
            region = matching[0]
    target = targets.get(region)
    if (source_hash != digest or not target or target["sheet_name"] != sheet
            or not _within(extent, bounds[region])):
        return f"{display}（未能绑定已审范围）", None, None
    link = (sheet, address)
    if location.get("merged_placeholder") is True:
        return f"{display}（合并区域占位）", link, None
    return display, link, (region, extent)


def _finding_detail(finding):
    severity = finding.get("severity") or "review"
    parts = [{"error": "显式规则不符", "coverage": "覆盖缺口"}.get(severity, "需人工复核")]
    for key, label in (("observed_value", "观察值"), ("expected_value", "预期")):
        if finding.get(key) is not None:
            parts.append(f"{label} {_text(finding[key], 250)}")
    evidence = finding.get("evidence") or {}
    for key, label in (("detector", "检测器"), ("formula", "公式"), ("notes", "证据"),
                       ("reason", "原因"), ("interpretation", "解释"), ("message", "说明"), ("statistics", "统计")):
        if evidence.get(key) is not None:
            parts.append(f"{label} {_text(evidence[key], 250)}")
    return "。".join(parts)


def _project_findings(report, digest, targets, bounds):
    summaries = []
    for finding in report["findings"]:
        rule = _text(finding["rule_id"], 150)
        severity = finding.get("severity") or "review"
        display, links, cells = set(), set(), defaultdict(set)
        for location in finding["locations"]:
            label, link, anchor = _project_location(location, digest, targets, bounds)
            display.add(label)
            if link is not None:
                links.add(link)
            if anchor is not None and severity != "coverage":
                region, (left, top, right, bottom) = anchor
                cells[region].update((row, column) for row in range(top, bottom + 1)
                                     for column in range(left, right + 1))
        detail = _finding_detail(finding)
        item = {"check": rule, "severity": severity, "detail": detail}
        for region, positions in cells.items():
            targets[region]["findings"].append({**item, "finding_id": _text(finding["finding_id"], 128),
                "evidence": {"cells": [{"row": row, "column_index": column} for row, column in sorted(positions)]}})
        summaries.append({**item, "summary_location": _text(" ↔ ".join(sorted(display)), 1000) or "未提供精确位置",
                          "summary_label": _LABELS.get(rule, rule), "summary_link": min(links, default=None)})
    return sorted(summaries, key=lambda item: (item["summary_location"], item["check"], item["detail"]))


_XLSM_COPY_NOTICE = "由原始 XLSM 派生的无宏 XLSX 标注副本。VBA 和外部链接定义未保留，未运行宏或重算公式，原文件未修改。"
_CSV_SHEET = "CSV 原始记录"
_MAX_CSV_CELLS = 250_000
_MAX_CSV_RECORDS = 5001
_MAX_CSV_RECORD_CHARS = 1 << 20


def _csv_records(data):
    """Cap physical reads and logical records before csv.reader allocates their fields."""
    with TextIOWrapper(BytesIO(data), encoding="utf-8-sig", newline="") as stream:
        remaining = _MAX_CSV_RECORD_CHARS

        def lines():
            nonlocal remaining
            while line := stream.readline(remaining + 1):
                remaining -= len(line)
                if remaining < 0:
                    raise ValueError("CSV logical record exceeds publication budget")
                yield line

        try:
            for row in csv.reader(lines(), strict=True):
                remaining = _MAX_CSV_RECORD_CHARS
                yield row
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError("CSV record positions could not be reproduced") from exc


def _csv_workbook(report, data, digest):
    """Mirror UTF-8 CSV record positions, never literal text into formulas."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    execution = report.get("execution") or {}
    options = execution.get("options") or {}
    inputs = execution.get("inputs") or []
    matches = [item for item in inputs if item.get("role") == "table"]
    if (execution.get("skill") != "research-integrity" or len(matches) != 1
            or matches[0].get("sha256") != digest or "numeric" not in options.get("checks", [])):
        raise ValueError("Fusion report does not identify the authorized CSV numeric review")
    header, widths, width = [], [], 0
    for height, row in enumerate(_csv_records(data), 1):
        if height == 1:
            header = row
        if not header or height > _MAX_CSV_RECORDS:
            raise ValueError("CSV record count exceeds publication budget")
        width = max(width, len(row))
        if width > 16384 or height * width > _MAX_CSV_CELLS:
            raise ValueError("CSV cell count exceeds publication budget")
        if any(len(value) > 32767 or ILLEGAL_CHARACTERS_RE.search(value) for value in row):
            raise ValueError("CSV contains text that cannot be copied faithfully to Excel")
        widths.append(len(row))
    if not widths:
        raise ValueError("CSV record count exceeds publication budget")
    height = len(widths)
    wb = Workbook(write_only=True)
    sheet = wb.create_sheet(_CSV_SHEET)
    for row in _csv_records(data):
        cells = []
        for value in row:
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    # Openpyxl drops empty strings on reopen, but they remain distinct from a
    # missing CSV field in the retained original. The file labels record indexing.
    workbook = BytesIO()
    wb.save(workbook)
    wb.close()
    target = {"sheet_name": _CSV_SHEET, "region_id": "csv_records", "region_bounds": _address((1, 1, width, height)),
              "header_row": 1, "column_numbers": range(1, width + 1), "row_numbers": range(2, height + 1),
              "findings": []}
    summaries = [{"check": "csv_derived", "summary_label": "CSV 派生副本",
                  "summary_location": _CSV_SHEET, "summary_link": None, "severity": "review",
                  "detail": "CSV 逻辑记录按原顺序映射，含表头。仅 Worker 精确区段标红；线索不是定论。原 CSV 未修改。"}]
    for finding in report["findings"]:
        label, links, positions = set(), set(), set()
        rule, severity = _text(finding["rule_id"], 150), finding.get("severity") or "review"
        for location in finding["locations"]:
            if location.get("source_file_hash") != digest:
                label.add("非当前 CSV 来源（未标注）")
                continue
            scope = location.get("scope")
            row, last, col = (location.get(key) for key in ("row_index", "end_row_index", "column_index"))
            if (scope == "csv_range" and all(type(i) is int for i in (row, last, col))
                    and 2 <= row <= last <= height and 1 <= col <= len(header)
                    and all(col <= widths[n - 1] for n in range(row, last + 1))):
                address = _address((col, row, col, last))
                label.add(f"{_CSV_SHEET}!{address}")
                links.add((_CSV_SHEET, address))
                if severity != "coverage":
                    positions.update((n, col) for n in range(row, last + 1))
            elif scope == "column" and type(col) is int and 1 <= col <= len(header):
                label.add(f"CSV 第 {col} 列（{_text(header[col - 1], 100)}，非单元格发现）")
            else:
                label.add("未能绑定 CSV 精确记录（未标注）")
        item = {"check": rule, "severity": severity, "detail": _finding_detail(finding)}
        if positions:
            target["findings"].append({**item, "finding_id": _text(finding["finding_id"], 128),
                "evidence": {"cells": [{"row": r, "column_index": c} for r, c in sorted(positions)]}})
        summaries.append({**item, "summary_location": _text(" ↔ ".join(sorted(label)), 1000) or "未提供精确位置",
                          "summary_label": _LABELS.get(rule, rule), "summary_link": min(links, default=None)})
    return workbook.getvalue(), [target], summaries


def publish_fusion_workbook(report, *, source_file_id, source_sha256, source_name, source_bytes):
    """Render and register one annotated copy, returning the host artifact receipt."""
    from citrus_audit.workbook import export_reviewed_workbook
    from src.core.cancel import raise_if_cancelled
    from src.core.ooxml_determinism import normalize_ooxml_bytes
    from src.research import tools
    from src.research.fusion_delivery import report_renderer

    raise_if_cancelled("data_audit_workbook")
    suffix = Path(source_name).suffix.lower()
    if (not isinstance(source_file_id, str) or not source_file_id.strip() or len(source_file_id) > 256
            or suffix not in {".xlsx", ".xlsm", ".csv"} or not isinstance(source_bytes, bytes)
            or sha256(source_bytes).hexdigest() != source_sha256):
        raise ValueError("Authorized Fusion workbook identity does not match its bytes")
    status = report.get("status")
    if status not in {"complete", "partial"}:
        raise ValueError("Fusion workbook requires a completed or partial audit")
    if suffix == ".csv":
        data, targets, summaries = _csv_workbook(report, source_bytes, source_sha256)
    else:
        data = source_bytes
        areas, bounds = _review_targets(report, source_sha256, suffix)
        summaries = _project_findings(report, source_sha256, areas, bounds)
        targets = list(areas.values())
    macro_free = suffix == ".xlsm"
    content, stats = export_reviewed_workbook(data, targets=targets,
        source_sha256=source_sha256, original=suffix != ".csv", status=status, presentation="annotated",
        exact_locations=True, summary_findings=summaries, macro_free_copy=macro_free,
        summary_note=_XLSM_COPY_NOTICE if macro_free else "")
    content = normalize_ooxml_bytes(content)
    digest = sha256(content).hexdigest()
    tag = {".csv": "_CSV派生", ".xlsm": "_XLSM无宏副本"}.get(suffix, "")
    filename = tools._short_stem(source_name) + tag + "_审查标注.xlsx"
    publication_key = sha256(json.dumps([source_file_id, source_sha256, digest, "fusion-annotated/1"],
                                         separators=(",", ":")).encode()).hexdigest()
    raise_if_cancelled("data_audit_workbook")
    with report_renderer("fusion_annotated_workbook"):
        url = tools._save_artifact(filename, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   source_file_id=source_file_id, source_sha256=source_sha256,
                                   logical_task_id="audit_workbook:" + publication_key)
    if not url:
        raise ValueError("Fusion workbook was not registered")
    return {"url": url, "filename": filename, "sha256": digest, "size_bytes": len(content),
            "source_file_id": source_file_id, "source_sha256": source_sha256,
            "file_index": 1, "cells": stats["marked_cells"], "source_format": suffix[1:],
            **({"notice": _XLSM_COPY_NOTICE} if macro_free else {})}
