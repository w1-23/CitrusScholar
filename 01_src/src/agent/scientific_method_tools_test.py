import json
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.preset_catalog import build_preset_catalog
from src.agent.dataset_tool_scope import authorized_scope


def test_method_catalog_signature_and_inline_verification(monkeypatch):
    from src.agent.scientific_method_tools import verify_literature_references

    from src.tools.registry import is_readonly_call

    catalog = build_preset_catalog()
    expected = {'prepare_audit_dataset', 'audit_prepared_dataset', 'verify_literature_references',
                'run_numeric_integrity', 'collect_numeric_integrity'}
    for name in expected:
        catalog.resolve(name, expected)
    assert is_readonly_call('verify_literature_references', {})
    assert is_readonly_call('audit_prepared_dataset', {})
    assert is_readonly_call('collect_numeric_integrity', {})
    assert not is_readonly_call('run_numeric_integrity', {})
    assert not is_readonly_call('prepare_audit_dataset', {})
    monkeypatch.setenv('CITRUS_SCIENTIFIC_METHODS_ONLY', '1')
    with patch('src.core.capability_policy.allows', return_value=True), patch(
        'src.research.reference_verification_service.verify_references',
        return_value={'status': 'complete', 'records': []},
    ) as verify:
        result = verify_literature_references.invoke({'references': [{'doi': '10.1000/abc'}]})
    verify.assert_called_once_with([{'doi': '10.1000/abc'}])
    assert json.loads(result)['status'] == 'complete'


def test_explicit_coordinates_or_sheet_are_never_replaced_by_default():
    import pytest
    with pytest.raises(ValueError):
        authorized_scope(None, '只审查 data!A1:C52')
    with pytest.raises(ValueError):
        authorized_scope({'sheet': 'data', 'bounds': 'A1:C53'}, '只审查 data!A1:C52')
    exact = authorized_scope({'sheet': 'data', 'bounds': 'A1:C52'}, '只审查 data!A1:C52')
    assert exact.sheet == 'data' and exact.bounds == 'A1:C52'
    assert authorized_scope({'sheet': 'data'}, '仅审查工作表 data').bounds is None
    assert authorized_scope(None, '审查一下这个数据表') is None
    assert authorized_scope(None, 'Report only missing cells and exact duplicate rows, with source locations.') is None
    with pytest.raises(ValueError):
        authorized_scope(None, 'Audit only rows 3 to 8')


def test_delivery_outputs_one_verified_asset_and_one_render():
    from src.core.public_delivery import project_artifact_delivery
    from src.research.artifact_delivery import build_delivery, missing_answer_links

    identity = SimpleNamespace(run_id='a' * 32, session_id='sess-name', owner_user_id=7)
    artifact_id, render_id, checksum = 'b' * 32, 'c' * 32, 'd' * 64
    path = f'/api/v2/session/{identity.session_id}/artifacts/{artifact_id}'
    output = {'artifact_id': artifact_id, 'sha256': checksum, 'format': 'svg'}
    verified = {'link': path, 'artifact_id': artifact_id, 'request_matches': True, 'sha256': checksum,
                'filename': 'one.svg', 'mime': 'image/svg+xml', 'size_bytes': 42, 'valid': True,
                'reason_code': 'verified'}
    row = {'name': 'run_research_figure_script', 'status': 'complete', 'tool_call_id': 'one',
           'artifact_validations': [verified]}
    message = SimpleNamespace(artifact={'figure_id': 'e' * 32, 'exports': [output], 'qa_receipt': {'status': 'pass'}})
    manifest = build_delivery(identity=identity, request_id='test', rows=[row], messages={'one': message},
        verified=[verified, verified], project_lookup=lambda *_: {
            'exports_json': {'svg': output}, 'document_json': {'render_id': render_id}})
    assert project_artifact_delivery(manifest)['user_links'] == [path]
    assert manifest['render_id'] == render_id and manifest['delivery_status'] == 'complete'
    assert missing_answer_links(manifest, 'Download ' + path) == []
    failed = build_delivery(identity=identity, request_id='test', rows=[row], messages={'one': message},
        verified=[dict(verified, valid=False)], requirements=[{'formats': ['svg', 'pdf']}],
        gaps=[{'reason_code': 'required_deliverable_missing'}], project_lookup=lambda *_: {})
    assert failed['delivery_status'] == 'failed' and not failed['user_links']
