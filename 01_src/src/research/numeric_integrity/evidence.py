"""Project pinned PaperConan schema 2 into non-verdict, source-addressed evidence."""
import math
import re

CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
SPAN = re.compile(r"(?:rows? )?([1-9][0-9]{0,6})(?:-([1-9][0-9]{0,6}))?")
COUNTS = ("files_discovered", "files_succeeded", "files_failed", "sheets_succeeded",
          "sheets_skipped", "blocks_analyzed", "blocks_skipped", "limitations_omitted")
GROUPS = ("relations", "progressions", "equal_pairs", "row_pairs", "row_relations", "within_col",
          "identical_after_rounding", "grim", "block_dups")
METRICS = ("n", "n_rows_checked", "n_failed", "n_occurrences", "same_position_count", "run_length",
           "fraction_of_smaller", "p", "p_adj", "fdr_significant", "rows_matched")
MAX_FINDINGS = 1000


def _token(value, fallback):
    return value if isinstance(value, str) and CODE.fullmatch(value) else fallback


def _span(value):
    match = SPAN.fullmatch(value) if isinstance(value, str) else None
    if match:
        start = int(match[1])
        end = int(match[2] or match[1])
        if end >= start:
            return start, end
    return None


def _location(sources, filename, sheet=None, *, rows=None, cols=None):
    source = sources.get(filename)
    if source is None:
        return None
    result = {"source": source.reference, "scope": "file"}
    if source.format == "xlsx" and isinstance(sheet, str):
        result.update(scope="sheet", sheet=sheet[:31])
    if rows:
        result.update(start_row=rows[0], end_row=rows[1])
    if cols:
        result.update(start_column=cols[0], end_column=cols[1])
    if rows and cols:
        result["scope"] = "csv_range" if source.format == "csv" else "sheet_range"
    elif rows:
        result["scope"] = "rows"
    elif cols:
        result["scope"] = "columns"
    return result


def _metrics(row):
    return {key: row[key] for key in METRICS if key in row and
            (type(row[key]) in (int, bool) or type(row[key]) is float and math.isfinite(row[key]))}


def _finding(kind, row, locations):
    locs = [loc for idx, loc in enumerate(locations) if loc is not None and loc not in locations[:idx]]
    return {"detector_id": "paperconan." + _token(row.get("kind"), kind), "locations": locs,
            "location_status": "localized" if locs else "not_reported_by_engine",
            "statistics": _metrics(row)}


def _block_finding(group, row, block, sources):
    file, sheet, bounds = block.get("file"), block.get("sheet"), block.get("block", {})
    rows, cols = _span(bounds.get("rows")), _span(bounds.get("cols"))
    locs = [_location(sources, file, sheet, rows=rows, cols=cols)]
    for sample in (row.get("example_cells") or [])[:8]:
        if (type(sample) in (list, tuple) and len(sample) == 2
                and all(type(index) is int and index > 0 for index in sample)):
            r, c = sample
            locs.append(_location(sources, file, sheet, rows=(r, r), cols=(c, c)))
    for side in ("", "_a", "_b"):
        r, c = row.get(f"row{side}_idx"), row.get(f"col{side}_idx")
        if type(r) is int and r >= 0 or type(c) is int and c >= 0:
            rr = (r + 1, r + 1) if type(r) is int and r >= 0 else rows
            cc = (c + 1, c + 1) if type(c) is int and c >= 0 else cols
            locs.append(_location(sources, file, sheet, rows=rr, cols=cc))
    return _finding(group, row, locs)


def _cross_finding(row, sources):
    locs = []
    for side in ("a", "b"):
        row_number = row.get(f"row_{side}_idx")
        rows = ((row_number + 1,) * 2 if type(row_number) is int and row_number >= 0
                else _span(row.get(f"block_{side}")))
        locs.append(_location(sources, row.get(f"file_{side}"), row.get(f"sheet_{side}"), rows=rows))
        if row.get("kind") == "cross_sheet_position_identical":
            for point in (row.get("examples") or [])[:8]:
                r, c = point.get("row"), point.get("col")
                if type(r) is int and r > 0 and type(c) is int and c > 0:
                    locs.append(_location(sources, row.get(f"file_{side}"), row.get(f"sheet_{side}"),
                                          rows=(r, r), cols=(c, c)))
    return _finding("cross_sheet", row, locs)


def normalize(raw, sources):
    if raw.get("schema_version") != 2 or raw.get("tool_version") != "0.9.0":
        raise ValueError("unexpected_paperconan_schema")
    source_map = {src.staged_name: src for src in sources}
    findings, coverage = [], {}
    for block in raw.get("relations_blocks", []):
        for group in GROUPS:
            findings.extend(_block_finding(group, row, block, source_map) for row in block.get(group, []))
    findings.extend(_cross_finding(row, source_map) for row in raw.get("cross_sheet_findings", []))
    for group in ("digit_distribution", "decimal_endings", "decimal_tail_clusters"):
        for row in raw.get(group, []):
            file, _, sheet = str(row.get("label", "")).partition("::")
            findings.append(_finding(group, row, [_location(source_map, file, sheet)]))
    source_coverage = raw.get("coverage", {})
    for key in COUNTS:
        value = source_coverage.get(key, 0)
        coverage[key] = value if type(value) is int and value >= 0 else 0
    limitations = []
    for item in source_coverage.get("limitations", []):
        issue = {"scope": _token(item.get("scope"), "unknown"), "reason": _token(item.get("reason"), "upstream_limitation")}
        location = _location(source_map, item.get("file"), item.get("sheet"))
        if location:
            issue["location"] = location
        if type(item.get("count")) is int:
            issue["count"] = item["count"]
        if "detector" in item:
            issue["detector"] = _token(item.get("detector"), "unknown")
        limitations.append(issue)
    omitted = max(0, len(findings) - MAX_FINDINGS)
    if omitted:
        limitations.append({"scope": "adapter", "reason": "report_finding_limit", "count": omitted})
    coverage.update(limitations=limitations, upstream_findings_omitted=raw.get("findings_omitted", 0),
                    adapter_findings_omitted=omitted,
                    findings_without_location=sum(not item["locations"] for item in findings),
                    truncated=bool(source_coverage.get("truncated") or omitted or raw.get("findings_omitted")))
    state = raw.get("scan_status")
    if state not in ("complete", "partial", "failed"):
        raise ValueError("unexpected_paperconan_status")
    if state != "failed" and (coverage["truncated"] or limitations or coverage["findings_without_location"]):
        state = "partial"
    elif state == "complete" and not coverage["blocks_analyzed"]:
        state = "not_applicable"
    return state, findings[:MAX_FINDINGS], coverage
