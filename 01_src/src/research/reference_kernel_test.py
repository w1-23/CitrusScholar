"""Offline contract tests for the chat default reference-identity kernel."""
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
from unittest.mock import patch

from citrus_refcheck import verification
from src.research import reference_audit as service


class ReferenceKernelTests(unittest.TestCase):
    def setUp(self):
        from src import config
        from src.core import cancel, task_events, tracing
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, 'PROJECT_ROOT', self.root))
        self.enterContext(patch.object(service, '_scope', lambda owner, session: None))
        self.enterContext(patch.object(service, '_lease', lambda owner: nullcontext()))
        self.enterContext(patch.object(service, '_CACHE', {}))
        self.enterContext(patch.object(tracing, 'web_search_enabled', lambda: True))
        self.enterContext(patch.object(cancel, 'is_cancelled', lambda: False))
        self.enterContext(patch.object(task_events, 'remaining_seconds', lambda _: 600))
        self.pool = verification.ThreadPoolExecutor(max_workers=4,
                                                    thread_name_prefix='reference-kernel-test')
        self.enterContext(patch.object(verification, '_WORKERS', self.pool))
        self.addCleanup(self.pool.shutdown, wait=False)

    def _audit(self, entries, budget=50):
        return service.create_audit(7, 'kernel', references=entries, request_budget=budget)

    def test_duplicate_queries_are_shared_and_ordinals_preserved(self):
        from src.core import tracing
        from src.research import reference_tools
        calls = []

        def lookup(doi):
            calls.append(doi)
            return 200, [{'source': 'Crossref', 'doi': doi,
                          'title': 'Shared ' + doi.rsplit('/', 1)[-1],
                          'year': '2024', 'url': 'https://doi.org/' + doi}], ''

        entries = [{'doi': f'10.1234/{i}', 'title': f'Shared {i}', 'year': '2024'}
                   for i in range(3)]
        entries = [entries[0], entries[1], entries[2],
                   dict(entries[0]), dict(entries[1]), dict(entries[2])]
        with (patch.object(service.lit, '_crossref_work', lookup),
              patch.object(service.lit, '_datacite_work',
                           side_effect=AssertionError('unnecessary registry')),
              patch.object(service.lit, '_crossref_search',
                           side_effect=AssertionError('unnecessary search')),
              patch.object(service.lit, '_openalex_search',
                           side_effect=AssertionError('unnecessary search')),
              patch.object(service.lit, '_deepseek_reference_candidates',
                           side_effect=AssertionError('zero default body fetch')),
              patch.object(service.lit.httpx, 'stream',
                           side_effect=AssertionError('zero default body fetch'))):
            audit = self._audit(entries, budget=3)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=10, time_budget=15)

        self.assertEqual(sorted(calls), ['10.1234/0', '10.1234/1', '10.1234/2'])
        self.assertEqual(result['processed_indices'], list(range(6)))
        self.assertEqual(result['budget']['requests_used'], 3)
        self.assertEqual([row['record_index'] for row in result['rows']], list(range(6)))
        self.assertEqual(result['verdict_counts']['verified'], 6)
        self.assertTrue(result['terminal'])
        self.assertEqual(result['rule_version'], 'reference-fields/4')
        self.assertEqual(result['rows'][0]['limits']['kernel'],
                         'citrus_refcheck/reference-verification/1')

        with (patch.object(tracing, 'get_user_id', return_value=7),
              patch.object(tracing, 'get_session_id', return_value='kernel'),
              patch.object(tracing, 'get_client_request_id', return_value='test')):
            report = json.loads(reference_tools.verify_uploaded_references.invoke(
                {'audit_id': audit['audit_id']}))
        self.assertTrue(report['audit_status']['ready_for_report'])
        self.assertEqual(len(report['rows']), 6)
        self.assertEqual([row['record_index'] for row in report['rows']], list(range(6)))
        for row in report['rows']:
            self.assertEqual(row['processing_status'], 'processed')
            self.assertEqual(row['identity'], 'matched')
            self.assertEqual(row['reason'], 'identifier_match')
            self.assertFalse(row['retryable'])
        self.assertIsNone(report['next_offset'])

    def test_run_tolerates_read_only_include_evidence_flag(self):
        from src.core import tracing
        from src.research import reference_tools

        def lookup(doi):
            return 200, [{'source': 'Crossref', 'doi': doi,
                          'title': 'Compat flag', 'year': '2024',
                          'url': 'https://doi.org/' + doi}], ''

        audit = self._audit([{'doi': '10.1234/compat', 'title': 'Compat flag',
                              'year': '2024'}], budget=2)
        with (patch.object(service.lit, '_crossref_work', lookup),
              patch.object(tracing, 'get_user_id', return_value=7),
              patch.object(tracing, 'get_session_id', return_value='kernel'),
              patch.object(tracing, 'get_client_request_id', return_value='test')):
            report = json.loads(reference_tools.verify_uploaded_references.invoke({
                'audit_id': audit['audit_id'], 'operation': 'run',
                'include_evidence': True}))

        self.assertNotEqual(report['_tool_outcome']['status'], 'not_run')
        self.assertEqual(report['result_view'], 'compact')
        self.assertEqual(report['rows'][0]['identity'], 'matched')

    def test_rate_limit_is_reported_as_unconfirmed_gap(self):
        with (patch.object(service.lit, '_crossref_work', lambda doi: (429, [], '')),
              patch.object(service.lit, '_datacite_work',
                           side_effect=AssertionError('no fallback on 429'))):
            audit = self._audit([{'doi': '10.1234/limited', 'title': 'Limited',
                                  'year': '2024'}], budget=2)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=5, time_budget=10)

        row = result['rows'][0]
        self.assertEqual(result['processed_indices'], [0])
        self.assertEqual(row['status'], 'network_error')
        self.assertTrue(row['retryable'])
        self.assertEqual(row['verdict']['decision'], 'not_confirmed')
        self.assertTrue(result['terminal'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['run_reason'], 'batch_finished')

    def test_documented_author_abbreviation_is_benign(self):
        record = {'source': 'Crossref', 'doi': '10.1234/abbrev',
                  'title': 'Abbreviated authors', 'authors': 'John Smith; Jane Doe',
                  'year': '2024', 'url': 'https://doi.org/10.1234/abbrev'}
        with patch.object(service.lit, '_crossref_work', lambda doi: (200, [record], '')):
            audit = self._audit([{'doi': '10.1234/abbrev', 'title': 'Abbreviated authors',
                                  'authors': 'J. Smith et al.', 'year': '2024'}], budget=2)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=5, time_budget=10)

        row = result['rows'][0]
        self.assertEqual(row['status'], 'metadata_matched')
        self.assertEqual(row['verdict']['decision'], 'verified')
        self.assertTrue(any(item['variant_type'] == 'documented_author_abbreviation'
                            for item in row['verdict']['benign_variants']))

    def test_identifier_title_conflict_requires_correction(self):
        record = {'source': 'Crossref', 'doi': '10.1234/conflict',
                  'title': 'Actual registered title', 'authors': 'A Author',
                  'year': '2024', 'url': 'https://doi.org/10.1234/conflict'}
        with patch.object(service.lit, '_crossref_work', lambda doi: (200, [record], '')):
            audit = self._audit([{'doi': '10.1234/conflict',
                                  'title': 'Different submitted title',
                                  'authors': 'A Author', 'year': '2024'}], budget=2)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=5, time_budget=10)

        row = result['rows'][0]
        self.assertEqual(row['status'], 'metadata_mismatch')
        self.assertEqual(row['match_status'], 'conflict')
        self.assertEqual(row['reason'], 'identifier_title_conflict')
        self.assertEqual(row['verdict']['decision'], 'correction_required')
        self.assertTrue(any(item['field'] == 'title'
                            for item in row['verdict']['material_differences']))

    def test_multiple_source_titles_are_conflict(self):
        records = [{'source': 'Crossref', 'doi': '10.1234/two',
                    'title': 'First title', 'year': '2024'},
                   {'source': 'DataCite', 'doi': '10.1234/two',
                    'title': 'Second title', 'year': '2024'}]
        with patch.object(service.lit, '_crossref_work', lambda doi: (200, records, '')):
            audit = self._audit([{'doi': '10.1234/two', 'title': 'First title',
                                  'year': '2024'}], budget=2)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=5, time_budget=10)

        row = result['rows'][0]
        self.assertEqual(row['status'], 'metadata_mismatch')
        self.assertEqual(row['match_status'], 'conflict')
        self.assertEqual(row['reason'], 'source_title_conflict')
        self.assertEqual(row['verdict']['decision'], 'not_confirmed')

    def test_missing_record_is_unconfirmed_not_a_failure(self):
        with (patch.object(service.lit, '_crossref_work', lambda doi: (404, [], '')),
              patch.object(service.lit, '_datacite_work', lambda doi: (404, [], ''))):
            audit = self._audit([{'doi': '10.1234/missing', 'title': 'Missing',
                                  'year': '2024'}], budget=2)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=5, time_budget=10)

        row = result['rows'][0]
        self.assertEqual(row['status'], 'not_found_in_checked_registries')
        self.assertFalse(row['retryable'])
        self.assertEqual(row['verdict']['decision'], 'not_confirmed')
        self.assertTrue(result['terminal'])
        self.assertTrue(result['coverage']['processing_complete'])

    def test_deadline_does_not_hang(self):
        def slow(doi):
            time.sleep(2.0)
            return 200, [], ''

        with patch.object(service.lit, '_crossref_work', slow):
            audit = self._audit([{'doi': '10.1234/slow', 'title': 'Slow',
                                  'year': '2024'}], budget=2)
            started = time.monotonic()
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=5, time_budget=0.5)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.8)
        self.assertEqual(result['processed_indices'], [])
        self.assertEqual(result['coverage']['pending'], 1)
        self.assertEqual(result['run_reason'], 'deadline')

    def test_title_lookup_stops_after_confirmed_identity(self):
        calls = []

        def search(title):
            calls.append('search')
            return 200, [{'doi': '10.1234/title-match'}], ''

        def crossref(doi):
            calls.append('doi')
            return 200, [{'source': 'Crossref', 'doi': '10.1234/title-match',
                          'title': 'Exact shared title', 'authors': 'Alice Example',
                          'year': '2024'}], ''

        entries = [{'doi': '', 'title': 'Exact shared title',
                    'authors': 'Alice Example', 'year': '2024'}]
        with (patch.object(service.lit, '_deepseek_reference_candidates', search),
              patch.object(service.lit, '_crossref_work', crossref),
              patch.object(service.lit, '_crossref_search',
                           side_effect=AssertionError('early stop must skip Crossref search')),
              patch.object(service.lit, '_openalex_search',
                           side_effect=AssertionError('early stop must skip OpenAlex'))):
            audit = self._audit(entries, budget=5)
            result = service.run_audit(7, 'kernel', audit['audit_id'],
                                       max_rows=10, time_budget=15)

        self.assertEqual(calls, ['search', 'doi'])
        self.assertEqual(result['budget']['requests_used'], 2)
        self.assertEqual(result['budget']['queries_used'], 1)
        self.assertEqual(result['rows'][0]['identity'], 'metadata_corroborated')


if __name__ == '__main__':
    unittest.main()
