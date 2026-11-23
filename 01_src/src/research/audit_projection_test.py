import json
import unittest

from src.research.audit_projection import (MAX_FINDINGS, project_findings, table_location,
                                           table_targets)

DIGEST = 'a' * 64
FILE_ID = 'b' * 32


def _report():
    signal = {'sheet': 'S', 'check': 'duplicate_row', 'severity': 'review', 'detail': 'dup',
              'row': 3, 'evidence': {'cells': [{'row': 3, 'column_index': 4},
                                                {'row': 3, 'column_index': 5}]}}
    table = {'source_name': 'a.xlsx', 'source_sha256': DIGEST, 'sheet': 'S', 'region_id': 'r1',
             'region_bounds': 'D1:E5', 'header_row': 1, 'header_rows': [1],
             'column_numbers': [4, 5], 'row_numbers': [2, 3, 4, 5], 'columns': ['v', 'w']}
    result = {'file': 'a.xlsx', 'ok': True, 'file_index': 0, 'source_file_id': FILE_ID,
              'source_sha256': DIGEST, 'region_id': 'r1', 'region_bounds': 'D1:E5',
              'table_metadata': [{'sheet_name': 'S', 'header_row': 1, 'header_rows': [1],
                                  'row_count': 4, 'column_count': 2, 'region_id': 'r1',
                                  'region_bounds': 'D1:E5'}],
              'findings': [signal], 'rule_execution': [{'rules': {'duplicate_row': {'status': 'executed'}}}]}
    envelope = {'file_index': 0, 'source_file_id': FILE_ID, 'table': table}
    return {'table_results': [result], 'source_envelopes': [envelope]}


class AuditProjectionTest(unittest.TestCase):
    def test_persisted_id_and_exact_cells_are_preserved(self):
        report = _report()
        target = next(table_targets(report))
        finding = {'finding_id': 'c' * 64, 'rule': 'duplicate_row', 'rule_version': 'table-audit/2',
                   'locations': [table_location(target, target['signals'][0])],
                   'evidence': {'detail': 'dup', 'calculation': target['signals'][0]['evidence']},
                   'summary': 'dup', 'coverage': {}, 'review_status': 'pending'}
        report['common_findings'] = [finding]
        projection = project_findings(report)
        self.assertEqual(len(projection['findings']), 1)
        item = projection['findings'][0]
        self.assertEqual(item['finding_id'], 'c' * 64)
        self.assertEqual(item['locations'][0]['cells'], ['D3', 'E3'])

    def test_legacy_report_without_envelopes_uses_reviewed_columns(self):
        report = {'table_results': [{'file': 'legacy.xlsx', 'ok': True, 'file_index': 0,
                   'source_file_id': FILE_ID, 'source_sha256': DIGEST,
                   'table_metadata': [{'sheet_name': 'S', 'header_row': 1,
                                       'row_count': 2, 'column_count': 2}],
                   'findings': [{'sheet': 'S', 'check': 'missing_cell', 'severity': 'review',
                                 'detail': 'x', 'row': 2, 'column_index': 1}]}],
                   'common_findings': []}
        projection = project_findings(report)
        self.assertEqual(projection['findings'][0]['locations'][0]['cells'], ['A2'])
        self.assertEqual(projection['status'], 'complete')

    def test_cross_source_finding_keeps_both_locations(self):
        report = _report()
        target = next(table_targets(report))
        report['common_findings'] = [{'finding_id': 'd' * 64, 'rule': 'window_search',
            'rule_version': 'audit-plan/1', 'summary': 'match',
            'locations': [table_location(target, target['signals'][0]),
                          {**table_location(target, target['signals'][0]), 'source_name': 'b.xlsx'}],
            'evidence': {}, 'coverage': {}}]
        projection = project_findings(report)
        self.assertEqual(len(projection['findings'][0]['locations']), 2)
        self.assertEqual(projection['findings'][0]['kind'], 'bound_comparison')

    def test_projection_reports_truncation(self):
        report = {'table_results': [], 'common_findings': [
            {'finding_id': f'{i:064x}', 'rule': 'x', 'rule_version': 'v',
             'locations': [{'source_id': 'attachment:0:' + FILE_ID, 'source_name': 'a.xlsx',
                            'source_sha256': DIGEST, 'sheet': 'S', 'rows': [1], 'columns': [],
                            'cells': ['A1']}], 'evidence': {}, 'coverage': {},
             'summary': 'x'} for i in range(MAX_FINDINGS + 5)], 'coverage': {}}
        projection = project_findings(report)
        self.assertEqual(len(projection['findings']), MAX_FINDINGS)
        self.assertEqual(projection['projection_omitted'], 5)
        self.assertEqual(projection['status'], 'partial')

    def test_projection_encoding_is_bounded(self):
        report = {'table_results': [], 'common_findings': [
            {'finding_id': f'{i:064x}', 'rule': 'x', 'rule_version': 'v',
             'locations': [{'source_id': 'attachment:0:' + FILE_ID, 'source_name': 'a.xlsx',
                            'source_sha256': DIGEST, 'sheet': 'S', 'rows': [1], 'columns': [],
                            'cells': ['A1']}], 'evidence': {'note': 'x' * 400_000},
             'coverage': {}, 'summary': 'x'} for i in range(6)], 'coverage': {}}
        projection = project_findings(report)
        encoded = json.dumps(projection, ensure_ascii=False).encode('utf-8')
        self.assertLess(len(encoded), 2 * 1024 * 1024 + 10_000)
        self.assertEqual(projection['status'], 'partial')
        self.assertGreater(projection['projection_omitted'], 0)


if __name__ == '__main__':
    unittest.main()
