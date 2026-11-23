"""Project recorded audit findings and bind workbook targets to reviewed sources."""
from collections import defaultdict
import json
import re

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

from citrus_audit.integrity import METHODS
from citrus_audit.methods import COVERAGE_CHECKS, METHOD_VERSION
from citrus_audit.workbook import finding_coordinates

PROJECTION_VERSION = 'data-audit-findings/1'
MAX_FINDINGS = 1000
MAX_PROJECTION_BYTES = 2 * 1024 * 1024
_ACCOUNT_ID = re.compile(r'[a-f0-9]{32}')
_ATTACHMENT = re.compile(r'attachment:(\d+):(.+)')


def source_key(source):
    digest = source.get('source_sha256') or source.get('sha256')
    file_id = source.get('file_id') or source.get('source_file_id') or ''
    index = source.get('file_index')
    match = _ATTACHMENT.fullmatch(source.get('source_id') or '')
    if match:
        index = int(match[1]) if index is None else index
        file_id = file_id or match[2]
    if not digest:
        return None
    if _ACCOUNT_ID.fullmatch(file_id):
        return file_id, digest
    return (index, digest) if type(index) is int and index >= 0 else None


def source_fields(index, file_id, name, digest):
    return {'file_index': index, 'source_file_id': file_id, 'source_sha256': digest,
            'source_id': f'attachment:{index}:{file_id or "upload:" + digest}', 'source_name': name}


def table_targets(report):
    """Read saved target metadata and recover legacy regional identities when unique."""
    envelopes = report.get('source_envelopes', ())
    for result in report.get('table_results', ()):
        if not result.get('ok'):
            continue
        for offset, entry in enumerate(result.get('table_metadata') or ()):
            sheet = entry.get('sheet_name', '')
            region = entry.get('region_id') or result.get('region_id') or ''
            digest = result.get('source_sha256', '')
            matches = [item for item in envelopes if item['table'].get('sheet') == sheet
                       and (item['table'].get('region_id') or '') == region
                       and item['table'].get('source_sha256') == digest
                       and (not item['table'].get('source_name') or item['table']['source_name'] == result['file'])
                       and (result.get('file_index') is None or item.get('file_index') == result['file_index'])
                       and (not result.get('source_file_id') or item.get('source_file_id') == result['source_file_id'])]
            saved = matches[0] if len(matches) == 1 else {}
            envelope = saved.get('table', {})
            index = result.get('file_index', saved.get('file_index'))
            file_id = result.get('source_file_id') or saved.get('source_file_id') or ''
            if not _ACCOUNT_ID.fullmatch(file_id):
                file_id = ''
            columns = entry.get('column_numbers') or envelope.get('column_numbers')
            rows = entry.get('row_numbers') or envelope.get('row_numbers')
            if region and not columns and entry.get('data_bounds'):
                left, top, right, bottom = range_boundaries(entry['data_bounds'])
                columns = list(range(left, right + 1))
                rows = range(top, bottom + 1)
            if columns is None and not region:
                columns = list(range(1, int(entry.get('column_count') or 0) + 1))
            header = entry.get('header_row') or result.get('header_row') or 1
            receipts = result.get('rule_execution') or []
            yield {**entry, **source_fields(index, file_id, result['file'], digest),
                   'sheet': sheet, 'region_id': region, 'row_numbers': rows,
                   'region_bounds': entry.get('region_bounds') or result.get('region_bounds') or '',
                   'header_row': header, 'header_rows': entry.get('header_rows') or result.get('header_rows') or [header],
                   'column_numbers': columns, 'columns': entry.get('columns') or envelope.get('columns') or [],
                   'rule_execution': receipts[offset] if offset < len(receipts) else {},
                   'signals': [signal for signal in result.get('findings', ()) if signal.get('sheet', '') == sheet]}


def table_location(target, signal):
    """Keep legacy row/column fields and add exact original cells when available."""
    row, column = signal.get('row'), signal.get('column_index')
    location = {key: target[key] for key in ('source_id', 'source_name', 'source_sha256', 'sheet', 'header_row')}
    location['rows'] = [row] if type(row) is int else []
    location['columns'] = [{'index': column, 'name': signal.get('field')}] if type(column) is int else []
    if target.get('region_id'):
        location.update(region_id=target['region_id'], region_bounds=target['region_bounds'])
    if target.get('source_file_id'):
        location['file_id'] = target['source_file_id']
    cells = finding_coordinates(signal, target.get('column_numbers') or ())
    if cells:
        location['cells'] = [f'{get_column_letter(column)}{row}' for row, column in cells
                             if _valid_cell(row, column)]
    return location


def _valid_cell(row, column):
    return type(row) is int and type(column) is int and 1 <= row <= 1048576 and 1 <= column <= 16384


def location_cells(location):
    """Normalize verified A1 lists and matrices into absolute coordinate pairs."""
    coords = {}
    for entry in location.get('cells') or ():
        for cell in entry if isinstance(entry, list) else (entry,):
            if isinstance(cell, str) and re.fullmatch(r'[A-Z]{1,3}[1-9]\d{0,6}', cell):
                row, column = coordinate_to_tuple(cell)
                if _valid_cell(row, column):
                    coords[row, column] = cell
    return coords


def _signal_key(rule, detail, evidence, location):
    return (source_key(location), location.get('sheet'), location.get('region_id') or '', rule, detail,
            tuple(location.get('rows') or ()), tuple(col['index'] for col in location.get('columns') or ()),
            json.dumps(evidence or {}, sort_keys=True, ensure_ascii=False, default=str))


def _finding_kind(rule, version):
    if rule in COVERAGE_CHECKS:
        return 'coverage_diagnostic'
    if rule == 'row_annotation':
        return 'manual_or_model_annotation'
    if rule.startswith('custom.'):
        return 'code'
    if version == METHOD_VERSION:
        return 'table_rule'
    if version == 'audit-plan/1':
        return 'bound_comparison'
    if rule in METHODS:
        return 'cross_file_numeric'
    return 'summary_consistency'


def iter_findings(report, targets):
    """Yield findings with stable review IDs, plus table signals without review IDs."""
    saved = defaultdict(list)
    for target in targets:
        for signal in target['signals']:
            location = table_location(target, signal)
            key = _signal_key(signal['check'], signal.get('detail', ''), signal.get('evidence'), location)
            saved[key].append((signal, location))
    seen_ids = set()
    for finding in report.get('common_findings', ()):
        finding_id = finding['finding_id']
        if finding_id in seen_ids:
            continue
        seen_ids.add(finding_id)
        rule, version = finding['rule'], finding['rule_version']
        evidence = finding.get('evidence') or {}
        detail = finding.get('summary') or evidence.get('detail', '')
        locations = finding.get('locations') or []
        severity = 'review'
        if version == METHOD_VERSION and len(locations) == 1:
            key = _signal_key(rule, detail, evidence.get('calculation'), locations[0])
            records = saved.pop(key, [])
            if records:
                signal, location = records[0]
                locations = [{**locations[0], **location}]
                severity = signal.get('severity') or severity
        yield {'finding_id': finding_id, 'kind': _finding_kind(rule, version), 'rule': rule,
               'rule_version': version, 'severity': severity, 'detail': detail,
               'locations': locations, 'evidence': finding.get('evidence') or {},
               'coverage': finding.get('coverage') or {},
               'review': finding.get('review') or {'state': finding.get('review_status', 'pending'), 'revision': 0}}
    for records in saved.values():
        for signal, location in records:
            rule = signal['check']
            yield {'kind': _finding_kind(rule, METHOD_VERSION), 'rule': rule,
                   'rule_version': METHOD_VERSION if rule != 'row_annotation' else '',
                   'severity': signal.get('severity') or signal.get('level') or '',
                   'detail': signal.get('detail', ''), 'locations': [location],
                   'evidence': signal.get('evidence') or {}, 'coverage': {}}

def project_findings(report):
    """Expose a bounded read view without duplicating immutable reports in storage."""
    findings, size, omitted = [], 0, 0
    counts = defaultdict(int)
    for finding in iter_findings(report, table_targets(report)):
        counts[finding['kind']] += 1
        encoded = len(json.dumps(finding, ensure_ascii=False).encode('utf-8'))
        if len(findings) >= MAX_FINDINGS or size + encoded > MAX_PROJECTION_BYTES:
            omitted += 1
            continue
        findings.append(finding)
        size += encoded
    scan_omitted = (report.get('coverage') or {}).get('scan_findings_omitted', 0)
    review_omitted = (report.get('coverage') or {}).get('findings_omitted', 0)
    return {'schema_version': PROJECTION_VERSION, 'findings': findings, 'counts': dict(counts),
            'projection_omitted': omitted, 'scan_findings_omitted': scan_omitted,
            'review_findings_omitted': review_omitted,
            'status': 'partial' if omitted or scan_omitted or review_omitted else 'complete'}
