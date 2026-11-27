from contextlib import nullcontext
from io import BytesIO

import pytest

from src.research import dataset_audit as audit

OWNER = 41
SESSION = 'dataset-audit-fixture'
OTHER_SESSION = 'dataset-audit-other'


@pytest.fixture
def source(tmp_path, monkeypatch):
    from src import config

    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    from src.session import files, manager

    monkeypatch.setattr(files, 'DB_PATH', state / 'files.sqlite3')
    monkeypatch.setattr(files, 'FILES_ROOT', state / 'files')
    monkeypatch.setattr(manager, 'DB_PATH', state / 'history.sqlite3')
    monkeypatch.setattr(manager.session_manager, 'get_session_owner', lambda sid: {
        SESSION: OWNER, OTHER_SESSION: OWNER, 'different-owner': OWNER + 1}.get(sid))
    monkeypatch.setattr('src.core.account_leases.account_lease', lambda _owner: nullcontext())

    def new(name, data):
        item = files.register_file(session_id=SESSION, owner_user_id=OWNER, filename=name,
                                   content=data, mime='application/octet-stream', origin='uploaded')
        return dict(owner=OWNER, session_id=SESSION, execution_id='synthetic-execution',
                    file_id=item['file_id'], source_sha256=item['sha256'], source_name=name,
                    source_bytes=data)
    return new


def _workbook(rows=(), edit=None):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.title = 'Sample data'
    for row in rows:
        workbook.active.append(row)
    if edit:
        edit(workbook)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _audit(prepared, **checks):
    assert prepared['status'] == 'complete', prepared
    return audit.audit_dataset(owner=OWNER, session_id=SESSION, dataset_ref=prepared['dataset_ref'], checks=checks)


def test_layout_to_audit_reuses_frozen_coordinates(source, monkeypatch):
    data = _workbook([('specimen', 'score'), ('A', 2), ('A', 4), ('B', 10), (None, 5)])
    prepared = audit.prepare_dataset(**source('synthetic.xlsx', data))
    assert prepared['status'] == 'complete', prepared
    assert prepared['scope']['reviewed'] == {'sheet': 'Sample data', 'range': 'A1:B5', 'header_rows': [1]}
    assert 'values' not in prepared
    monkeypatch.setattr(audit.reader, 'workbook_table', lambda *_: pytest.fail('source parser was reused'))
    reviewed = _audit(prepared, checks=['missing', 'duplicate', 'range'], required_fields=['specimen'],
                      unique_fields=['specimen'], ranges={'score': {'minimum': 0, 'maximum': 5}})
    assert reviewed['status'] == 'complete', reviewed
    assert reviewed['validation_engine'] == 'pandera'
    assert reviewed['findings_total'] == 4
    assert {(r['check'],r['row'],r['column_index']) for r in reviewed['findings']} == {
        ('missing',5,1), ('duplicate',2,None), ('duplicate',3,None), ('range',4,2)}


def test_explicit_range_does_not_expand(source):
    def edit(w):
        for row, values in enumerate([('code', 'score'), ('A', 2), ('A', 3), ('A', 99)], 3):
            for col, value in enumerate(values, 4):
                w.active.cell(row, col, value)
        w.active['H1'] = 'outside'
        w.create_sheet('Not audited')['A1'] = 'outside'
    prepared = audit.prepare_dataset(**source('scoped.xlsx', _workbook(edit=edit)),
        scope=audit.WorkbookScope(sheet='Sample data', bounds='$D$3:$E$5', header_rows=[3]))
    reviewed = _audit(prepared, checks=['duplicate','range'], unique_fields=['code'],
        ranges={'score': {'minimum': 0, 'maximum': 5}})
    assert reviewed['status'] == 'complete', reviewed
    assert reviewed['scope']['reviewed']['range'] == 'D3:E5'
    assert reviewed['scope']['unreviewed'] == ['Not audited']
    assert [(r['check'], r['row']) for r in reviewed['findings']] == [('duplicate', 4), ('duplicate', 5)]


@pytest.mark.parametrize('edit,expected', [
    (lambda w: w.create_sheet('Another'), 'worksheet_ambiguous'),
    (lambda w: w.active.__setitem__('F2', 19), 'worksheet_region_ambiguous'),
    (lambda w: w.active.merge_cells('A1:B1'), 'worksheet_merged_cells'),
    (lambda w: w.active.__setitem__('B2', '=1+1'), 'worksheet_formula_cache_missing'),
])
def test_ambiguous_workbook_cannot_issue_ref(source, edit, expected):
    data = _workbook([('code', 'score'), ('A', 2), ('B', 3)], edit)
    prepared = audit.prepare_dataset(**source('ambiguous.xlsx', data))
    assert prepared['status'] == 'needs_input' and prepared['reason_code'] == expected, prepared
    assert 'dataset_ref' not in prepared


def test_csv_has_records_not_worksheet_and_no_ragged_normalization(source):
    prepared = audit.prepare_dataset(**source('synthetic.csv', b'code,value\n0001,"some\ntext"\n0002,1\n'))
    assert prepared['status'] == 'complete', prepared
    assert prepared['scope']['reviewed'] == {'records': [2, 3], 'fields': [1, 2]}
    assert _audit(prepared, checks=['duplicate'])['findings_total'] == 0
    ragged = audit.prepare_dataset(**source('ragged.csv', b'code,value\nA,1\nB\n'))
    assert ragged['reason_code'] == 'csv_ragged_records' and 'dataset_ref' not in ragged
    result = audit.prepare_dataset(**source('scoped.csv', b'code,value\nA,1\n'),
        scope=audit.WorkbookScope(sheet='x', bounds='A1:B2', header_rows=[1]))
    assert result['reason_code'] == 'dataset_csv_does_not_accept_workbook_scope'


def test_ref_scope_hash_and_rules_fail_closed(source, monkeypatch):
    args = source('source.csv', b'code,value\nA,1\nB,2\n')
    assert audit.prepare_dataset(**{**args,'source_sha256':'0'*64})['reason_code'] == 'dataset_source_changed'
    assert audit.prepare_dataset(**args,scope={'sheet':'fake','bounds':'A1:B2','header_rows':[1]})['reason_code'] == 'dataset_scope_not_authorized'
    prepared = audit.prepare_dataset(**args)
    ref = prepared['dataset_ref']
    assert audit.audit_dataset(owner=OWNER,session_id=OTHER_SESSION,dataset_ref=ref)['status'] == 'not_run'
    assert audit.audit_dataset(owner=OWNER+1,session_id='different-owner',dataset_ref=ref)['status'] == 'not_run'
    assert audit.audit_dataset(owner=OWNER,session_id=SESSION,dataset_ref={**ref,'sha256':'0'*64})['status'] == 'not_run'
    assert _audit(prepared,checks=['missing'],required_fields=['unknown'])['reason_code'] == 'dataset_check_field_unknown'
    monkeypatch.setattr(audit, '_source', lambda *_: {'sha256': '0'*64})
    assert _audit(prepared,checks=['missing'])['reason_code'] == 'dataset_source_changed'


def test_unexecuted_checks_and_finding_preview_are_reported(source):
    values = b'code,value\n' + b'A,1\n'*240
    prepared = audit.prepare_dataset(**source('counts.csv', values))
    report = _audit(prepared, checks=['duplicate','range','outlier'], outlier_fields=['value'])
    assert report['status'] == 'partial', report
    states = {r['check']: r for r in report['checks']}
    assert states['range']['status'] == 'not_run'
    assert states['outlier']['limitations'] == [{'column':'value', 'code':'zero_iqr'}]
    assert report['findings_total'] == 240 and report['findings_omitted'] == 40


def test_sheet_only_and_run_scope_is_not_substitutable(source):
    data = _workbook([('code', 'value'), ('A', 1), ('B', 2)],
                     lambda w: w.create_sheet('Ignore').cell(1, 1, 'outside'))
    args = source('multi.xlsx', data)
    sheet = audit.WorkbookScope(sheet='Sample data')
    prepared = audit.prepare_dataset(**args, scope=sheet)
    assert prepared['status'] == 'complete', prepared
    assert prepared['scope']['selection'] == 'explicit_sheet_auto_region'
    assert prepared['scope']['unreviewed'] == ['Ignore']
    default_args = dict(owner=OWNER, session_id=SESSION, dataset_ref=prepared['dataset_ref'],
                        required_execution_id=args['execution_id'])
    assert audit.audit_dataset(**default_args, required_scope=sheet)['status'] == 'complete'
    smaller = audit.WorkbookScope(sheet='Sample data', bounds='A1:B2', header_rows=[1])
    assert audit.audit_dataset(**default_args, required_scope=smaller)['reason_code'] == 'dataset_reference_scope_mismatch'
    assert audit.audit_dataset(**{**default_args, 'required_execution_id': 'new-execution'},
                               required_scope=sheet)['reason_code'] == 'dataset_reference_scope_mismatch'


def test_separate_header_not_guessed_and_source_replacement_detected(source):
    ambiguous = _workbook([('specimen', 'measurement'), ('sample id', 'reading'), ('A', 1), ('B', 2)])
    assert audit.prepare_dataset(**source('headers.xlsx', ambiguous))['reason_code'] == 'worksheet_header_ambiguous'
    args = source('source.csv', b'code,value\nA,1\nB,2\n')
    prepared = audit.prepare_dataset(**args)
    original = audit._source(OWNER, args['file_id'])['path']
    original.write_bytes(b'code,value\nA,8\nB,9\n')  # Same length and registry ID, changed storage bytes.
    assert _audit(prepared, checks=['missing'])['reason_code'] == 'dataset_source_changed'


def test_iqr_finds_candidate_and_dependency_outage_fails_closed(source, monkeypatch):
    data = _workbook([('specimen', 'score')] + [(str(i), i) for i in range(1, 9)] + [('extreme', 100)])
    prepared = audit.prepare_dataset(**source('outlier.xlsx', data))
    checked = _audit(prepared, checks=['outlier'], outlier_fields=['score'])
    assert checked['status'] == 'complete' and checked['findings'] == [
        {'check': 'outlier', 'row': 10, 'column': 'score', 'column_index': 2}]
    import sys
    monkeypatch.setitem(sys.modules, 'pandera.pandas', None)
    assert _audit(prepared, checks=['missing'])['reason_code'] == 'dataset_validation_engine_unavailable'


def test_rejected_scope_does_not_hydrate_snapshot_payload(source, monkeypatch):
    args = source('private.csv', b'code,value\nA,1\nB,2\n')
    prepared = audit.prepare_dataset(**args)
    assert prepared['status'] == 'complete'
    from src.research.analysis_results import DatasetSnapshot
    monkeypatch.setattr(DatasetSnapshot, 'to_frame', lambda *_: pytest.fail('payload hydration denied'))
    assert audit.audit_dataset(owner=OWNER, session_id=OTHER_SESSION,
        dataset_ref=prepared['dataset_ref'])['reason_code'] == 'dataset_reference_unavailable'
    assert audit.audit_dataset(owner=OWNER, session_id=SESSION, required_execution_id='wrong-turn',
        dataset_ref=prepared['dataset_ref'])['reason_code'] == 'dataset_reference_scope_mismatch'
