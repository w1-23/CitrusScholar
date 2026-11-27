"""Resolve a single, bounded audit table without silently widening the user's scope."""
from __future__ import annotations

from contextlib import closing
import csv
from dataclasses import dataclass
from io import StringIO
import re

from citrus_audit._zip_safety import validate_macro_workbook
from citrus_audit.envelope import TableEnvelope
from citrus_audit.inspection import _layout, _merges
from citrus_audit.integrity import _number
from citrus_audit.readers import _a1, _region_box, open_xlsx_readonly, read_region

MAX_CELLS = 50_000
MAX_BYTES = 20 * 1024 * 1024
MAX_COLUMNS = 256
MAX_ROWS = 10_000


@dataclass(frozen=True)
class AmbiguousTable(Exception):
    code: str
    details: dict


def _limits(width, height):
    if width > MAX_COLUMNS or height > MAX_ROWS or width * height > MAX_CELLS:
        raise ValueError('dataset_scope_limit')


def _unambiguous_header(header):
    return bool(header) and all(
        isinstance(v, str) and v.strip() and len(v.strip()) <= 160 and _number(v)[0] is None
        for v in header
    ) and len({v.strip() for v in header}) == len(header)


def csv_table(data, name, digest):
    """A CSV is one strict table of logical records, not an Excel worksheet."""
    text = data.decode('utf-8-sig')
    if '\x00' in text:
        raise ValueError('dataset_csv_binary')
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',;\t|').delimiter
    except csv.Error:
        delimiter = ','
    with StringIO(text, newline='') as handle:
        rows = csv.reader(handle, delimiter=delimiter, strict=True)
        header = next(rows, [])
        if not _unambiguous_header(header):
            raise AmbiguousTable('csv_header_ambiguous', {})
        columns = tuple(v.strip() for v in header)
        values = []
        for ordinal, row in enumerate(rows, 2):
            _limits(len(columns), ordinal)
            if len(row) != len(columns):
                raise AmbiguousTable('csv_ragged_records', {'record': ordinal})
            values.append(tuple(v if v.strip() else None for v in row))
    if not values:
        raise AmbiguousTable('no_data_rows', {})
    return TableEnvelope(columns, tuple(values), tuple(range(2, len(values) + 2)),
                         tuple(range(1, len(columns) + 1)), digest, source_name=name,
                         coverage={'truncated': False, 'coordinate_system': 'logical_csv_records'}), {
        'selection': 'single_csv_table', 'unreviewed': [], 'scope_note': 'single logical CSV table',
        'reviewed': {'records': [2, len(values) + 1], 'fields': [1, len(columns)]}}


def _intersects(left, right):
    l, t, r, b = left
    ll, tt, rr, bb = right
    return l <= rr and ll <= r and t <= bb and tt <= b


def canonical_bounds(bounds):
    if not re.fullmatch(r'\$?[a-zA-Z]{1,3}\$?[1-9]\d{0,6}\s*:\s*\$?[a-zA-Z]{1,3}\$?[1-9]\d{0,6}', bounds.strip()):
        raise ValueError('invalid worksheet rectangle')
    return _a1(*_region_box(re.sub(r'[\s$]+', '', bounds)))


def _target(workbook, data, scope):
    visible = [s.title for s in workbook.worksheets if s.sheet_state == 'visible']
    if scope is not None:
        if scope.sheet not in workbook.sheetnames:
            raise AmbiguousTable('worksheet_not_found', {'visible_sheets': visible[:40]})
        sheet = workbook[scope.sheet]
        if scope.bounds is not None:
            return sheet, scope.bounds, scope.header_rows
    else:
        if len(visible) != 1:
            raise AmbiguousTable('worksheet_ambiguous', {'visible_sheets': visible[:40], 'visible_sheet_count': len(visible)})
        sheet = workbook[visible[0]]
    headers = scope.header_rows if scope is not None and scope.header_rows else (1,)
    layout = _layout(workbook, sheet, data, list(headers))
    regions = layout['regions']
    details = {'sheet': sheet.title, 'candidate_ranges': [r['bounds'] for r in regions]}
    if (not layout['detection']['scan_complete'] or layout['regions_truncated']
            or layout['sheet_layout_truncated']):
        raise AmbiguousTable('worksheet_scan_incomplete', details)
    if len(regions) != 1:
        raise AmbiguousTable('worksheet_region_ambiguous', details)
    region = regions[0]
    if tuple(region['header_rows']) != headers or not region['data_rows']:
        raise AmbiguousTable('worksheet_header_ambiguous', details)
    if 'rows_before_first_header' in region['limitations']:
        raise AmbiguousTable('worksheet_header_ambiguous', details)
    return sheet, region['bounds'], headers


def workbook_table(data, name, digest, scope):
    if name.lower().endswith('.xlsm'):
        validate_macro_workbook(data)
    with closing(open_xlsx_readonly(data, name, data_only=False)) as workbook:
        sheet, bounds, headers = _target(workbook, data, scope)
        bounds = canonical_bounds(bounds)
        l, t, r, b = _region_box(bounds)
        _limits(r - l + 1, b - t + 1)
        if headers[0] < t or headers[-1] >= b:
            raise AmbiguousTable('worksheet_header_ambiguous', {'sheet': sheet.title, 'range': bounds})
        merges, truncated = _merges(data, sheet._worksheet_path)
        if truncated or any(_intersects((l, t, r, b), _region_box(m['range'])) for m in merges):
            raise AmbiguousTable('worksheet_merged_cells', {'sheet': sheet.title, 'range': bounds})
        headers_found, formulas, preview = {}, [], {}
        sheet.reset_dimensions()
        with closing(sheet.iter_rows(min_row=t, max_row=b, min_col=l, max_col=r)) as rows:
            for row_number, row in enumerate(rows, t):
                if headers[-1] < row_number <= headers[-1] + 2:
                    preview[row_number] = [cell.value for cell in row]
                if row_number in headers:
                    headers_found[row_number] = [cell.value for cell in row]
                if row_number >= headers[0]:
                    formulas.extend((row_number, column_number) for column_number, cell in enumerate(row, l)
                                    if cell.data_type == 'f')
        if not _unambiguous_header(headers_found.get(headers[-1], [])) or any(row <= headers[-1] for row, _ in formulas):
            raise AmbiguousTable('worksheet_header_ambiguous', {'sheet': sheet.title, 'range': bounds})
        if (scope is None or not scope.header_rows) and len(preview) == 2 and (
                _unambiguous_header(preview[headers[-1] + 1])
                and not any(row == headers[-1] + 1 for row, _ in formulas)
                and any(_number(v)[0] is not None for v in preview[headers[-1] + 2])):
            raise AmbiguousTable('worksheet_header_ambiguous',
                                 {'sheet': sheet.title, 'range': bounds, 'candidate_header_rows': [headers[-1], headers[-1] + 1]})
        if headers != tuple(range(headers[0], headers[-1] + 1)):
            raise AmbiguousTable('worksheet_header_ambiguous', {'sheet': sheet.title, 'range': bounds})
        sheet_name = sheet.title
        other_sheets = [s.title for s in workbook.worksheets if s.title != sheet_name]
    table = read_region(data, name, bounds=bounds, header_rows=list(headers), sheet_name=sheet_name)
    if (table.source_sha256 != digest or table.sheet != sheet_name or table.bounds != bounds
            or table.header_rows != headers or not table.values or table.coverage.get('truncated')):
        raise ValueError('dataset_parser_scope_mismatch')
    if any(table.values[row - headers[-1] - 1][col - l] is None for row, col in formulas):
        raise AmbiguousTable('worksheet_formula_cache_missing', {'sheet': sheet_name, 'range': bounds})
    return table, {
        'selection': ('single_visible_table' if scope is None else
                      'explicit_range' if scope.bounds else 'explicit_sheet_auto_region'),
        'reviewed': {'sheet': sheet_name, 'range': bounds, 'header_rows': list(headers)},
        'unreviewed': other_sheets[:40], 'unreviewed_sheet_count': len(other_sheets),
        'scope_note': 'outside the selected rectangle was not audited',
        'formula_policy': 'saved cached values only, no recalculation',
    }
