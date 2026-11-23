"""Offline Linux checks for real parsers, stored attempts and task receipts."""
from contextlib import nullcontext
from hashlib import sha256
from io import BytesIO
import json

from openpyxl import Workbook
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from citrus_audit.inspection import inspect_workbook
from citrus_refcheck.inventory import extract_inventory, digest
from src.core.op_receipt import make_receipt
from src.core.task_result import build_task_result, effective_tool_records
from src.core.turn_outcome import completion_errors, completion_from_answer
from src.research import reference_audit as service


def _facts(*rows, errors=None):
    return {'records': [{**row, 'record_sha256': digest(row)} for row in rows],
            'errors': errors or {}, 'http_evidence': [], 'providers': {}}


def _record(payload, cid):
    receipt = make_receipt('verify_uploaded_references', cid, json.dumps(payload), None)
    return {'name': receipt['tool_name'], 'tool_call_id': cid, 'status': receipt['status'],
            'reason': receipt['reason_code'], 'op_receipt': receipt}


class P0CRegressionTests(unittest.TestCase):
    def test_workbook_layout_and_wide_coordinate_pages(self):
        wb = Workbook()
        sheet = wb.active
        sheet.merge_cells('A1:B1')
        sheet['A1'], sheet['A2'], sheet['B2'], sheet['D2'] = 'group', 'id', 'value', 'other'
        sheet['B3'], sheet['C4'] = '=1+1', '汉' * 9000
        wb.create_sheet('hidden').sheet_state = 'hidden'
        stream = BytesIO()
        wb.save(stream)
        data = stream.getvalue()
        opts = dict(sheet_name=sheet.title, header_rows=[1, 2], cell_range='A1:IV5', page_rows=10, cursor='')
        layout = inspect_workbook(data, 'fixture.xlsx', mode='layout', **opts)
        assert layout['merged_ranges'] == [{'range': 'A1:B1', 'anchor': 'A1'}]
        assert layout['sheet_layout'][1] == {'name': 'hidden', 'visibility': 'hidden'}
        assert 'A2:B2' in layout['observed_blocks'] and 'D2:D2' in layout['observed_blocks']
        assert layout['source_sha256'] == sha256(data).hexdigest()
        cells, cursors = [], set()
        while True:
            page = inspect_workbook(data, 'fixture.xlsx', mode='cells', **opts)
            assert 0 < len(page['cells']) <= 200
            assert len(json.dumps(page, ensure_ascii=False).encode()) <= 12_000
            cells.extend(page['cells'])
            opts['cursor'] = page['next_cursor']
            if not opts['cursor']:
                break
            assert opts['cursor'] not in cursors
            cursors.add(opts['cursor'])
        by_cell = {c['cell']: c for c in cells}
        assert len(cells) == len(by_cell) == 1280
        assert by_cell['B3'] == {'cell': 'B3', 'type': 'f', 'value': '=1+1'}
        assert by_cell['C4']['truncated'] and by_cell['C4']['value_characters'] == 9000
        opts.update(cursor=next(iter(cursors)), cell_range='B1:IV5')
        with self.assertRaisesRegex(ValueError, '游标'):
            inspect_workbook(data, 'fixture.xlsx', mode='cells', **opts)

    def test_delimited_inventory_with_exact_encoding_duplicate_and_bad_rows(self):
        source = '编号\t标题\t年份\r\n10.1234/a\t"上\r\n下"\t2024\r\n10.1234/a\t别名\t2024\r\nshort\r\n'.encode('gb18030')
        column_map = {'doi': '编号', 'title': '标题', 'year': '年份'}
        with self.assertRaisesRegex(ValueError, 'decoding'):
            extract_inventory(name='input.tsv', content=source, column_map=column_map)
        result = extract_inventory(name='input.tsv', content=source, column_map=column_map, encoding='gb18030')
        first, second, bad = result['entries']
        assert result['source']['sha256'] == sha256(source).hexdigest()
        assert first['positions'] == [{'line': 2, 'line_end': 3}]
        assert first['claims']['title'] == '上\r\n下'
        assert first['record_id'] != second['record_id'] and first['claims']['doi'] == second['claims']['doi']
        assert bad['parse_status'] == 'invalid_row' and bad['positions'] == [{'line': 5, 'line_end': 5}]
        assert result['extraction']['status'] == 'partial'

    def test_submitted_field_verdict_and_documented_years(self):
        claim = {'doi': '10.1234/a', 'title': 'Known article', 'year': '2024'}
        first = {**claim, 'source': 'one', 'journal': 'Name One', 'pages': '1-4'}
        second = {**claim, 'source': 'two', 'journal': 'Different Review', 'pages': '62-64'}
        clean = service._project_verification(claim, _facts(first, second))
        assert clean['status'] == 'metadata_matched' and clean['provider_conflicts']
        assert clean['fields']['pages']['status'] == 'not_checked'
        second.update(year='2025', years=[{'kind': 'published-online', 'year': 2024}])
        assert service._project_verification(claim, _facts(first, second))['fields']['year']['status'] == 'matched'
        second.update(year='2021', years=[])
        disputed = service._project_verification(claim, _facts(first, second))
        assert disputed['fields']['year']['status'] == 'uncertain'
        assert disputed['status'] == 'metadata_uncertain'
        uncertain = service._project_verification({**claim, 'year': '2023'}, _facts(first))
        assert uncertain['fields']['year']['reason'] == 'possible_online_print_difference_not_documented'

    def test_parallel_reference_lookup_reuses_requests_and_stops_at_confirmed_identity(self):
        from contextvars import ContextVar
        from threading import Barrier, Lock
        from src import config
        from src.core import cancel, task_events, tracing

        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, 'PROJECT_ROOT', root))
        self.enterContext(patch.object(service, '_scope', lambda owner, session: None))
        self.enterContext(patch.object(service, '_lease', lambda owner: nullcontext()))
        self.enterContext(patch.object(service, '_CACHE', {}))
        permission = ContextVar('fixture_network_permission', default=False)
        permission.set(True)
        self.enterContext(patch.object(tracing, 'web_search_enabled', permission.get))
        self.enterContext(patch.object(cancel, 'is_cancelled', lambda: False))
        self.enterContext(patch.object(task_events, 'remaining_seconds', lambda _: 600))
        first_wave = Barrier(4, timeout=5)
        lock = Lock()
        requests = []

        def lookup(doi):
            assert permission.get()
            lookup_context = service.lit._LOOKUP_CONTEXT.get()
            lookup_context.debit()
            with lock:
                requests.append(doi)
                wave = len(requests)
            if wave <= 4:
                first_wave.wait()
            return 200, [{'source': 'Crossref', 'doi': doi, 'title': 'Fixture ' + doi.split('/')[-1]}], ''

        self.enterContext(patch.object(service.lit, '_crossref_work', lookup))
        self.enterContext(patch.object(service.lit, '_datacite_work', side_effect=AssertionError('unnecessary registry')))
        self.enterContext(patch.object(service.lit, '_crossref_search', side_effect=AssertionError('unnecessary search')))
        entries = [{'doi': f'10.1234/{index}', 'title': f'Fixture {index}'} for index in range(9)]
        entries.append(entries[0].copy())
        audit = service.create_audit(21, 'parallel-fixture', references=entries, request_budget=9)
        result = service.run_audit(21, 'parallel-fixture', audit['audit_id'], max_rows=10, time_budget=15)

        assert sorted(requests) == [f'10.1234/{index}' for index in range(9)]
        assert result['processed_indices'] == list(range(10))
        assert result['budget']['requests_used'] == 9
        assert result['terminal']
        assert result['verdict_counts']['verified'] == 10
        assert len({row['record_id'] for row in result['rows']}) == 10

    def test_title_lookup_uses_deepseek_metadata_then_checks_registry_identity(self):
        from contextlib import contextmanager
        from src.core import tracing
        from src.tools import deepseek_search_fetch as search

        title = '合成引用题名用于验证原文检索'
        claims = {'title': title, 'authors': 'Alex Chen', 'year': '2024'}
        calls = []

        class Response:
            status_code = 200
            headers = {'content-type': 'application/json'}

            def iter_bytes(self):
                yield json.dumps({'results': [{'title': title, 'url': 'https://doi.org/10.1234/title-route',
                                               'content': 'fixture body must not be used'}]}).encode()

        @contextmanager
        def transport(method, url, **kwargs):
            assert (method, url) == ('POST', search.DEEPSEEK_SEARCH_URL)
            assert kwargs['json'] == {'query': title, 'ori_question': title, 'count': 5, 'need_content': False}
            assert kwargs['follow_redirects'] is False
            calls.append('search')
            yield Response()

        def registry(doi):
            assert doi == '10.1234/title-route'
            service.lit._LOOKUP_CONTEXT.get().debit()
            calls.append('registry')
            return 200, [{**claims, 'source': 'Crossref', 'doi': doi}], ''

        with (patch.object(tracing, 'web_search_enabled', return_value=True),
              patch.object(search, '_headers', return_value={'Content-Type': 'application/json'}),
              patch.object(service.lit.httpx, 'stream', transport),
              patch.object(service.lit, '_crossref_work', registry),
              patch.object(service.lit, '_crossref_search', side_effect=AssertionError('unnecessary source')),
              patch.object(service, '_CACHE', {})):
            debits = []
            result = service.verify_record(claims, owner=22, session_id='title-route', debit=lambda: debits.append(True))

        from citrus_refcheck.verdict import project_verdict
        verdict = project_verdict({'claims': claims}, result, audit_id='fixture')
        assert calls == ['search', 'registry'] and len(debits) == 2
        assert verdict['decision'] == 'verified'
        assert result['metadata_title'] == title
        assert 'fixture body must not be used' not in json.dumps(result)

    def test_100_structured_rows_resume_export_and_receipts(self):
        from src import config
        from src.core import tracing
        from src.core import cancel
        from src.core import task_events

        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, 'PROJECT_ROOT', root))
        self.enterContext(patch.object(service, '_scope', lambda owner, session: None))
        self.enterContext(patch.object(service, '_lease', lambda owner: nullcontext()))
        self.enterContext(patch.object(tracing, 'web_search_enabled', lambda: True))
        self.enterContext(patch.object(cancel, 'is_cancelled', lambda: False))
        self.enterContext(patch.object(task_events, 'remaining_seconds', lambda _: 600))
        attempted = []

        def lookup(doi):
            attempted.append(doi)
            index = int(doi.rsplit('/', 1)[-1])
            if index == 17 and attempted.count(doi) == 1:
                return 0, [], 'timeout'
            return 200, [{'source': 'Crossref', 'doi': doi,
                          'title': f'Citation {index}', 'year': '2024',
                          'url': 'https://doi.org/' + doi}], ''

        self.enterContext(patch.object(service.lit, '_crossref_work', lookup))
        self.enterContext(patch.object(service.lit, '_datacite_work',
                                       side_effect=AssertionError('unnecessary registry')))
        self.enterContext(patch.object(service.lit, '_crossref_search',
                                       side_effect=AssertionError('unnecessary search')))
        self.enterContext(patch.object(service.lit, '_openalex_search',
                                       side_effect=AssertionError('unnecessary search')))
        source = ('doi,title,year\n' + ''.join(f'10.1234/{i},Citation {i},2024\n' for i in range(100))).encode()
        audit = service.create_audit(5, 'test', name='input.csv', content=source, request_budget=101)
        audit_id = audit['audit_id']
        journal = []
        for i in range(5):
            step = service.run_audit(5, 'test', audit_id, max_rows=20)
            assert step['processed_indices'] == list(range(i * 20, i * 20 + 20))
            journal.append(_record(step, f'call-{i}'))
        assert step['coverage']['pending'] == 0 and step['coverage']['retryable'] == 1 and step['terminal']
        already = service.run_audit(5, 'test', audit_id)
        assert already['processed_indices'] == [] and already['run_reason'] == 'already_completed'
        from src.research.reference_tools import verify_uploaded_references
        with patch('src.core.tracing.get_user_id', return_value=5), patch('src.core.tracing.get_session_id', return_value='test'):
            report = json.loads(verify_uploaded_references.invoke({'audit_id': audit_id}))
        assert report['audit_status']['ready_for_report']
        assert report['terminal'] is True
        assert report['coverage']['retryable'] == 1 and not report['coverage']['complete']
        assert report['_tool_outcome']['reason_code'] == 'audit_completed_with_review'
        assert report['_tool_outcome']['status'] == 'complete'
        assert report['budget']['requests_used'] == 100
        report_record = _record(report, 'report')
        effective = effective_tool_records([*journal, report_record])
        assert all(row['reason'] == 'cumulative_identity_report_ready' for row in effective[:-2])
        assert effective[-2]['status'] == 'complete' and effective[-2]['reason'] == 'audit_completed_with_review'
        assert effective[-1]['status'] == 'complete' and effective[-1]['reason'] == 'audit_completed_with_review'
        assert journal[0]['status'] == 'partial'
        other_report = _record({**report, 'audit_id': 'f' * 32}, 'other-report')
        assert effective_tool_records([journal[0], other_report])[0]['status'] == 'partial'

        from src.agent.preset_catalog import canonical_tool_schema
        properties = canonical_tool_schema(verify_uploaded_references)['function']['parameters']['properties']
        assert not {'allow_web', 'batch_size', 'start_index'} & properties.keys()
        settled = service.run_audit(5, 'test', audit_id, resume=True)
        assert settled['processed_indices'] == [17] and settled['coverage']['complete']
        assert len(attempted) == 101 and settled['budget']['requests_remaining'] == 0
        assert len({row['record_id'] for row in settled['rows']}) == 100
        assert [a['result']['status'] for a in settled['rows'][17]['attempt_history']] == ['network_error', 'metadata_matched']
        journal.append(_record(settled, 'settled'))
        assert all(row['status'] == 'complete' for row in effective_tool_records(journal))
        assert journal[0]['status'] == 'partial'
        answer = completion_from_answer('100 source rows reviewed.', journal)
        assert not completion_errors(answer, journal)
        assert build_task_result(journal).status == 'completed'
        assert completion_errors(answer, journal, missing=['required_xlsx'])
        data, _, _ = service.export_audit(5, 'test', audit_id, format='json')
        assert len(json.loads(data)['rows']) == 100
        from src.research.reference_tools import _compact_audit
        offsets, projected = set(), []
        offset = 0
        while offset is not None:
            assert offset not in offsets
            offsets.add(offset)
            page = _compact_audit(service.get_audit(5, 'test', audit_id, offset=offset, limit=10))
            assert 0 < len(page['rows']) <= 10
            assert len(json.dumps(page, ensure_ascii=False).encode()) <= 12_500
            projected.extend(row['record_index'] for row in page['rows'])
            offset = page['next_offset']
        assert projected == list(range(100))
        xlsx, _, _ = service.export_audit(5, 'test', audit_id, format='xlsx')
        from openpyxl import load_workbook
        exported = load_workbook(BytesIO(xlsx), read_only=True)
        try:
            assert any(sheet.max_row == 101 for sheet in exported.worksheets)
        finally:
            exported.close()
        other = {**settled, 'audit_id': 'f' * 32}
        assert effective_tool_records([journal[0], _record(other, 'other')])[0]['status'] == 'partial'
        bad_publish = {**step, 'terminal': False, 'export_status': 'failed',
                       '_tool_outcome': {**step['_tool_outcome'], 'status': 'partial',
                                         'reason_code': 'publication_failed'},
                       'coverage': {**step['coverage'], 'processing_complete': False}}
        assert effective_tool_records([_record(bad_publish, 'bad'), journal[-1]])[0]['status'] == 'partial'

    def test_compact_reference_batch_reaches_the_model_without_readback(self):
        from langchain_core.messages import ToolMessage
        from src.core.tool_result_view import FIRST_DELIVERY_BYTES, content_wire_bytes, first_delivery
        from src.research.reference_tools import _compact_audit

        payload = {"schema": "reference_audit/2", "audit_id": "a" * 32,
                   "coverage": {"total": 10, "pending": 0, "retryable": 1}, "rows": [
                       {"record_id": f"ref-{index}", "record_index": index, "status": "metadata_matched",
                        "claims": {"title": "A long bibliographic title with authors and necessary distinguishing information " +
                                  "for a study with a sufficiently descriptive subtitle " + str(index)},
                        "source_url": f"https://doi.org/10.1234/fixture-{index}", "provider": "Crossref"}
                       for index in range(10)]}
        compact = _compact_audit(payload)
        content = json.dumps(compact, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        assert content_wire_bytes(content) > FIRST_DELIVERY_BYTES
        message = ToolMessage(content=content, tool_call_id="audit", name="verify_uploaded_references")
        assert first_delivery(message, call={"args": {"operation": "run"}}, reader_available=True) is message
        assert [row["record_index"] for row in compact["rows"]] == list(range(10))
        assert compact["next_offset"] is None

    def test_catalog_installs_new_tool_contracts(self):
        from src.agent.preset_catalog import _PRESETS, _load, _read_manifest

        manifest = _read_manifest()
        for preset in _PRESETS:
            if preset.name in {'inspect_uploaded_table', 'verify_uploaded_references'}:
                assert _load(preset, manifest[preset.name]).name == preset.name
