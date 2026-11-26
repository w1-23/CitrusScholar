"""Actual worker, source snapshot and authenticated figure delivery receipts."""
import asyncio
import hashlib
import json
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from src.agent.host import _verified_length_delivery
from src.core import figures, tool_results, tracing
from src.core.tool_result_view import FIRST_DELIVERY_BYTES, content_wire_bytes, first_delivery
from src.figures import store
from src.figures.publication_reliability_test import OWNER, OTHER, REQUEST, SESSION, runtime
from src.figures.sandbox import LocalBackend
from src.graph.task_contract import contract_for_turn
from src.research import figure_tools, tools
from src.research.artifact_logical_identity_test import SVG
from src.research.artifacts import publish_artifact
from src.research.tools import inspect_uploaded_table
from src.session import files
from src.session import manager as sessions


def test_csv_shape_snapshot_and_actual_figure_receipts(runtime, monkeypatch):
    state = runtime / 'state'
    monkeypatch.setattr(sessions, 'STATE_DIR', state)
    monkeypatch.setattr(sessions, 'DB_PATH', state / 'history.sqlite3')
    monkeypatch.setattr(sessions.SessionManager, '_instance', None)
    monkeypatch.setattr(tool_results, '_schema_ready', False)
    session_store = sessions.SessionManager()
    monkeypatch.setattr(sessions, 'session_manager', session_store)
    asyncio.run(session_store.get_or_create_session(SESSION, OWNER))
    assert session_store.get_session_owner(SESSION) == OWNER
    copy_context().run(_exercise_real_figure)


def _exercise_real_figure():
    tracing.set_session_id(SESSION)
    tracing.set_user_id(OWNER)
    tracing.set_client_request_id(REQUEST)
    tracing.set_current_tool_name('run_research_figure_script')
    tracing.set_original_query('使用 Python 绘制散点图并导出 SVG。')
    figures.begin_request()
    names = ['record_id', *[f'feature_{i:03}' for i in range(113)], 'annotation']
    raw = (','.join(names) + '\n' + '\n'.join(','.join([f'row_{r}', *[str(i*r) for i in range(113)], 'label'])
        for r in range(1, 6)) + '\n').encode()
    material = files.register_file(session_id=SESSION, owner_user_id=OWNER, filename='fixture.csv',
        mime='text/csv', content=raw)
    digest = hashlib.sha256(raw).hexdigest()
    profile = inspect_uploaded_table.invoke({'file_ids_json': [material['file_id']]})
    parsed = json.loads(profile)
    assert (parsed['rows'], parsed['columns'], parsed['source_sha256']) == (5, 115, digest)
    assert [field['name'] for field in parsed['fields']] == names
    recorded = tool_results.record_with_reference(tool_call_id='fixture-profile', session_id=SESSION,
        request_id=REQUEST, tool_name='inspect_uploaded_table', status='complete', result_text=profile)
    assert recorded and recorded['storage']['complete']
    assert recorded['full_sha256'] == hashlib.sha256(profile.encode()).hexdigest()
    message = ToolMessage(content=profile, name='inspect_uploaded_table', tool_call_id='fixture-profile',
        additional_kwargs={'result_reference': recorded})
    compact = first_delivery(message, call={}, reader_available=True)
    overview = json.loads(compact.content)['table_overview']
    assert content_wire_bytes(compact.content) <= FIRST_DELIVERY_BYTES
    assert (overview['rows'], overview['columns'], overview['source_sha256']) == (5, 115, digest)
    assert overview['fields'][0]['samples'] and overview['fields_omitted'] > 0
    assert overview['inputs_binding']['data']['file_id'] == material['file_id']

    contract = {'version': 2, 'tables': [], 'figures': [{'name': 'actual', 'formats': ['svg']}]}
    source = ('import pandas as pd\n'
              'frame = pd.read_csv(data)\n'
              'fig, ax = plt.subplots()\n'
              'ax.scatter(frame["feature_001"], frame["feature_002"])\n'
              'ax.set_xlabel("Feature 1")\n'
              'ax.set_ylabel("Feature 2")\n'
              'emit.figure("actual", fig)\n')
    calls = []
    original = LocalBackend().run

    def run(job):
        calls.append(dict(job.expected_input_sha256))
        return original(job)

    runner = SimpleNamespace(run=run)
    _, result = figure_tools._run_research_figure_script(script=source, title='Synthetic scatter',
        inputs_json=overview['inputs_binding'], outputs_json=contract, _sandbox_runner=runner)
    assert calls == [{'input_0.csv': digest}], result
    assert result['deliverable_stage'] == 'figure_published', result
    figure = result['figures'][0]
    assert figure['project_registered'] and figure['inline_registered'], figure
    entry = figure['exports'][0]
    owned = files.get_account_file(entry['file_id'], OWNER, touch=False)
    assert not files.get_account_file(entry['file_id'], OTHER, touch=False)
    payload = Path(owned['path']).read_bytes()
    assert entry['size'] == len(payload) > 0
    assert hashlib.sha256(payload).hexdigest() == entry['sha256']
    project = store.get_project(figure['figure_id'], OWNER)
    assert project['source_json']['input_snapshots'][0]['sha256'] == digest
    [card] = figures.snapshot().values()
    assert card['svg'].encode() == payload
    assert (card['download_url'], card['workbench_url']) == (entry['download_url'], figure['workspace_url'])

    _, analysis = figure_tools._run_research_figure_script(script="emit.report({'checked': 5})\n",
        outputs_json={'version': 2, 'tables': [], 'figures': []}, _sandbox_runner=runner)
    assert analysis['execution_stage'] == 'worker_receipt_verified', analysis
    assert analysis['deliverable_stage'] == 'analysis_only' and not analysis['_tool_outcome']['artifacts']
    _, missing = figure_tools._run_research_figure_script(script='pass\n',
        outputs_json=contract, _sandbox_runner=runner)
    assert missing['deliverable_stage'] == 'figure_unpublished' and not missing['exports'], missing
    assert missing['figures'][0]['reason_code'] == 'figure_render_missing', missing
    _, fake = figure_tools._error('PUBLISH', 'empty')
    assert not fake['_tool_outcome']['artifacts']
    with pytest.raises(ValueError, match='为空'):
        figure_tools._publish('empty.svg', b'', 'image/svg+xml')


def test_length_delivery_rechecks_real_current_bytes(runtime):
    entry = publish_artifact(owner=OWNER, session_id=SESSION, filename='synthetic.svg', content=SVG,
        mime='image/svg+xml', producer_tool='run_research_figure_script', request_id=REQUEST)
    owned = files.get_account_file(entry['file_id'], OWNER)
    artifact_url = entry['artifact_url']
    validations = tools.validate_artifact_links([artifact_url], session_id=SESSION, owner_user_id=OWNER, request_id=REQUEST)
    assert validations[0]['valid'] and validations[0]['request_matches'], validations
    answer_host = SimpleNamespace(identity=SimpleNamespace(owner_user_id=OWNER, session_id=SESSION),
        _result_request_id=lambda: REQUEST, records=[{'tool_call_id': 'real', 'name': 'run_research_figure_script',
            'status': 'complete', 'artifact_validations': validations}])
    requirements = contract_for_turn('绘制一张散点图，导出 SVG。', has_tables=False).deliverables
    answer, gaps = _verified_length_delivery(answer_host, requirements)
    assert artifact_url in answer and not gaps, (answer, gaps)
    with open(owned['path'], 'wb') as handle:
        handle.write(b'')
    answer, gaps = _verified_length_delivery(answer_host, requirements)
    assert artifact_url not in answer and gaps[0]['reason_code'] == 'required_deliverable_missing', (answer, gaps)
    answer_host.records = [{'tool_call_id': 'analysis', 'name': 'run_research_figure_script',
        'status': 'complete', 'reason': 'analysis_only'}]
    answer, gaps = _verified_length_delivery(answer_host, requirements)
    assert artifact_url not in answer and gaps[0]['reason_code'] == 'required_deliverable_missing', (answer, gaps)
