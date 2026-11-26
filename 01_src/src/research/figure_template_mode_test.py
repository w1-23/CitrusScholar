"""F-04: ``mode="template"`` is the one drawing entry, not a second chain.

The test drives the *public tool* with the local sandbox backend, so it exercises
compile → preflight → frozen input snapshot → worker → contract validation →
current-QA → figure project → typography receipt in one pass. A template request
must behave exactly like a script request everywhere except that its source is
compiled from typed parameters.
"""
import asyncio
import hashlib
import json
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import Mock

from src.core import figures, tool_results, tracing
from src.figures import store
from src.figures.publication_reliability_test import OWNER, REQUEST, SESSION, runtime  # noqa: F401
from src.figures.sandbox import LocalBackend
from src.research import figure_tools
from src.session import files
from src.session import manager as sessions

_ROW = "gene,log2fc,padj\n" + "\n".join(
    f"g{index},{effect},{pvalue}" for index, (effect, pvalue) in enumerate(
        [(-2.4, 1e-4), (-1.6, 0.004), (0.2, 0.4), (1.8, 0.002), (2.6, 1e-5), (0.0, 0.9)]) ) + "\n"
CSV = _ROW.encode()


def test_template_mode_uses_the_same_snapshot_worker_contract_qa_and_workbench(runtime, monkeypatch):  # noqa: F811
    state = runtime / "state"
    monkeypatch.setattr(sessions, "STATE_DIR", state)
    monkeypatch.setattr(sessions, "DB_PATH", state / "history.sqlite3")
    monkeypatch.setattr(sessions.SessionManager, "_instance", None)
    monkeypatch.setattr(tool_results, "_schema_ready", False)
    manager = sessions.SessionManager()
    monkeypatch.setattr(sessions, "session_manager", manager)
    asyncio.run(manager.get_or_create_session(SESSION, OWNER))
    copy_context().run(lambda: _exercise(monkeypatch))


def _exercise(monkeypatch):
    from citrus_figures import template_registry

    tracing.set_session_id(SESSION)
    tracing.set_user_id(OWNER)
    tracing.set_client_request_id(REQUEST)
    tracing.set_current_tool_name("run_research_figure_script")
    tracing.set_original_query("用固定模板 volcano 绘制火山图并导出 PDF。")
    figures.begin_request()
    material = files.register_file(session_id=SESSION, owner_user_id=OWNER, filename="volcano.csv",
                                   mime="text/csv", content=CSV)
    digest = hashlib.sha256(CSV).hexdigest()
    compiled = template_registry.compile_request(
        "volcano", {"gene_col": "gene", "effect_col": "log2fc", "p_col": "padj", "top_labels": 2},
        binding="data", title="Volcano", formats=("svg", "pdf"))
    backend = LocalBackend()
    original = backend.run
    calls = []

    def run(job):
        calls.append(dict(job.expected_input_sha256))
        return original(job)

    content, result = figure_tools._run_research_figure_script(
        script=compiled.main_script, outputs_json=compiled.output_contract,
        inputs_json={"data": {"file_id": material["file_id"]}},
        formats_json='["svg", "pdf"]', title="Volcano",
        _sandbox_runner=SimpleNamespace(run=run))

    assert isinstance(content, str) and content.startswith("{")
    # The same frozen snapshot the script path uses: the worker saw one input
    # with exactly the registered bytes, and nothing else.
    assert calls == [{"input_0.csv": digest}], result
    assert result["deliverable_stage"] == "figure_published", result
    assert result["_tool_outcome"]["status"] == "complete", result
    # Template provenance, not a second execution model.
    assert result["mode"] == "template"
    assert result["template"]["template_id"] == "volcano"
    assert result["template"]["binding"] == "data"
    assert result["template"]["version"] == template_registry.TEMPLATE_VERSION
    # One timeout policy, stated as worker wall-clock time.
    assert result["timeout"]["unit"] == "worker_wall_clock_seconds"
    assert result["timeout"]["kind"] == "template"
    assert result["timeout"]["template"] == "volcano"
    timing = result['timings_ms']
    assert timing == json.loads(content)['timings_ms']
    assert set(timing) == {'input_snapshot_and_replay', 'worker_execution_and_render',
                           'manifest_and_qa', 'publication', 'project_initialization'}
    assert all(type(value) is int and value >= 0 for value in timing.values())
    assert timing['worker_execution_and_render'] == result['duration_ms']
    # Typography is recorded before publication.
    assert result["typography"]["style_policy"] == "template_registry"
    assert result["typography"]["embedding"]["pdf"] != "not_provided"
    assert result["typography"]["embedding"]["svg"] == "family_reference_only"

    figure = result["figures"][0]
    assert figure["project_registered"] and figure["inline_registered"], figure
    formats = {item["format"] for item in figure["exports"]}
    assert {"svg", "pdf"} <= formats, formats
    project = store.get_project(figure["figure_id"], OWNER)
    source = project["source_json"]
    assert source["kind"] == "script"  # replay stays on the one worker byte path
    assert source["template"]["template_id"] == "volcano"
    assert source["style"]["policy"] == "template_registry"
    assert source["input_snapshots"][0]["sha256"] == digest
    assert source["output_contract"]["tables"] == [{"name": "frame", "parents": ["data"]}]
    assert source["output_contract"]["figures"][0]["parents"] == ["frame"]


def test_template_parameter_error_is_not_run_before_any_dispatch(monkeypatch):
    runner = Mock()
    monkeypatch.setattr(figure_tools, "_select_script_runner", runner)
    content, payload = figure_tools.run_research_figure_script.func(
        mode="template", template_id="volcano",
        template_parameters_json=json.dumps({"gene_col": "gene"}),
        inputs_json=json.dumps({"data": {"file_id": "a" * 32}}))
    runner.assert_not_called()
    assert json.loads(content) == payload
    assert payload["_tool_outcome"] == {"status": "not_run",
                                        "reason_code": "template_request_rejected",
                                        "artifacts": []}
    assert payload["error_code"] == "PARAMETERS"


def test_template_mode_rejects_script_source_and_contract_overrides(monkeypatch):
    runner = Mock()
    monkeypatch.setattr(figure_tools, "_select_script_runner", runner)
    binding = json.dumps({"data": {"file_id": "a" * 32}})
    parameters = json.dumps({"gene_col": "gene", "effect_col": "log2fc", "p_col": "padj"})
    for options in ({"script": "print('x')\n"},
                    {"script_ref_json": {"kind": "research_script", "id": "b" * 32,
                                        "version": 1, "sha256": "c" * 64}},
                    {"outputs_json": {"version": 2, "tables": [], "figures": []}},
                    {"template_id": ""}):
        _, payload = figure_tools.run_research_figure_script.func(
            mode="template", template_id=options.pop("template_id", "volcano"),
            template_parameters_json=parameters, inputs_json=binding, **options)
        assert payload["_tool_outcome"]["status"] == "not_run", options
        assert payload["_tool_outcome"]["reason_code"] == "template_request_rejected"
    runner.assert_not_called()



def test_figure_support_uses_sealed_members_and_rejects_changed_bytes(tmp_path):
    import pytest
    from src.figures.engine_bundle import _sealed_support_files

    relative = "packages/citrus_figures/__init__.py"
    module = tmp_path / relative
    module.parent.mkdir(parents=True)
    module.write_bytes(b"# sealed package")
    signed = module.read_bytes()
    pin = {"bytes": len(signed), "sha256": hashlib.sha256(signed).hexdigest()}
    inventory = tmp_path / "LINUX-SOURCE-MANIFEST.json"
    commit = "a" * 40
    inventory.write_text(json.dumps({"schema": "citrusscholar.linux-source/1",
        "integrated_branch_commit": commit, "members": {relative: pin},
        "inventory_scope": "All archived regular members except this manifest"}))
    for path in (module, inventory):
        path.chmod(0o400)
    tmp_path.chmod(0o500)
    try:
        bundle = _sealed_support_files(tmp_path, {"citrus_figures"}, commit)
        assert bundle["citrus_figures/__init__.py"] == signed
        assert json.loads(bundle["support-manifest.json"])["source_commit"] == commit
        with pytest.raises(ValueError, match="identity mismatch"):
            _sealed_support_files(tmp_path, {"citrus_figures"}, "b" * 40)
        module.chmod(0o600)
        module.write_bytes(b"# altered bytes!")
        module.chmod(0o400)
        with pytest.raises(ValueError, match="integrity mismatch"):
            _sealed_support_files(tmp_path, {"citrus_figures"}, commit)
    finally:
        tmp_path.chmod(0o700)
