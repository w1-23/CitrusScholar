"""Release regressions for audit coverage and bibliographic exports."""
from contextlib import contextmanager
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from src.research import custom_audit, reference_audit


@pytest.fixture
def worker(monkeypatch):
    from src.agent import code_tools

    fingerprint = 'a' * 64
    state = SimpleNamespace(payload=[], status='succeeded', process_stopped=True)
    request = SimpleNamespace(fingerprint=fingerprint, network_policy='offline', environment_id='base',
                              environment_revision='v1', script=SimpleNamespace(sha256='b' * 64),
                              inputs=[SimpleNamespace(file_id='sheet', sha256='c' * 64)])

    def status(_):
        receipt = {'process_stopped': state.process_stopped, 'network_policy': 'offline',
                   'request_fingerprint': fingerprint,
                   'verified_environment': {'image_id': 'sha256:' + 'f' * 64,
                                            'environment_id': 'base', 'revision': 'v1',
                                            'dependency_versions': {'python': '3.12'}}}
        return {'status': state.status, 'fence': 1, 'receipt': receipt}

    @contextmanager
    def accepted(*args, **kwargs):
        yield request, {'sha256': 'd' * 64}, json.dumps(state.payload).encode()

    monkeypatch.setattr(code_tools, '_identity', lambda: ('owner', None))
    monkeypatch.setattr(code_tools, '_service', lambda _: SimpleNamespace(status=status))
    monkeypatch.setattr(code_tools, '_accepted_file', accepted)
    return state


def admit():
    return custom_audit.admit({'execution_id': '0' * 32, 'path': '/outputs/audit.json'},
                              {('sheet', 'Sheet1'): SimpleNamespace(source_sha256='c' * 64)})


def report(**changes):
    return {'schema': custom_audit.REPORT_SCHEMA, 'method_id': 'scan', 'method_version': '1',
            'input_sha256': {'sheet': 'c' * 64}, 'findings': [],
            'coverage': {'requested': 3, 'executed': 3, 'skipped': 0, 'truncated': 0, 'reason': ''},
            **changes}


def test_zero_findings_require_report_level_coverage(worker):
    assert admit()[1]['status'] == 'partial'
    worker.payload = report()
    findings, receipt = admit()
    assert findings == [] and receipt['status'] == 'complete'
    assert receipt['coverage']['coverage_basis'] == 'script_reported'
    assert receipt['runtime_receipt']['output_sha256'] == 'd' * 64


@pytest.mark.parametrize('omitted', ['skipped', 'truncated'])
def test_partial_report_records_omitted_work(worker, omitted):
    worker.payload = report()
    worker.payload['coverage'].update(executed=2, **{omitted: 1}, reason='fixture limit')
    assert admit()[1]['status'] == 'partial'


def test_reject_wrong_input_and_inconsistent_coverage(worker):
    worker.payload = report(input_sha256={'sheet': '0' * 64})
    with pytest.raises(ValueError, match='input_mismatch'):
        admit()
    worker.payload = report()
    worker.payload['coverage']['executed'] = 2
    with pytest.raises(ValueError, match='coverage_invalid'):
        admit()


@pytest.mark.parametrize('status,stopped', [('failed', True), ('succeeded', False)])
def test_report_requires_verified_worker_completion(worker, status, stopped):
    worker.status, worker.process_stopped, worker.payload = status, stopped, report()
    with pytest.raises(ValueError, match='verified_worker_receipt_required'):
        admit()


def test_all_exports_report_verdict_without_changing_claims(monkeypatch):
    decisions = ('verified', 'correction_required', 'not_confirmed', None)
    rows = []
    for i, decision in enumerate(decisions):
        verdict = None if decision is None else {'decision': decision, 'identity': 'confirmed',
                   'benign_variants': [{'field': 'authors'}], 'material_differences': [{'field': 'doi'}],
                   'critical_unknowns': [{'field': 'year'}], 'coverage': {'queried_sources': ['Crossref:doi']}}
        rows.append(dict(record_index=i, record_id=f'record-{i}', citation='=Synthetic',
                         status='metadata_matched', claims={'doi': '10.5555/submitted'}, positions=[], result={}, verdict=verdict))
    monkeypatch.setattr(reference_audit, 'get_audit', lambda *a, **kw: {'rows': rows})
    data, _, _ = reference_audit.export_audit(1, 'test', 'id', format='xlsx')
    book = load_workbook(BytesIO(data), read_only=True)
    try:
        header, *values = list(book['References'].values)
        records = [dict(zip(header, row)) for row in values]
        assert [row['verdict'] for row in records] == ['已核实', '建议更正', '尚未确认', '尚未确认']
        assert records[0]['citation'] == "'=Synthetic"
        assert json.loads(records[1]['material_differences']) == [{'field': 'doi'}]
        assert json.loads(records[0]['benign_variants']) == [{'field': 'authors'}]
        assert json.loads(records[2]['critical_unknowns']) == [{'field': 'year'}]
        assert json.loads(records[0]['source_coverage']) == {'queried_sources': ['Crossref:doi']}
        assert json.loads(records[3]['source_coverage']) == {'status': 'not_checked'}
        assert 'Evidence' in book.sheetnames
    finally:
        book.close()
    for fmt in ('ris', 'bibtex'):
        data, _, _ = reference_audit.export_audit(1, 'test', 'id', format=fmt)
        text = data.decode()
        for decision in decisions[:3]:
            assert f'verdict={decision}' in text
        assert text.count('verdict=not_confirmed') == 2
        assert text.count('10.5555/submitted') == len(decisions)
