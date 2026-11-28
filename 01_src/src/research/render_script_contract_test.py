"""Fixed-renderer emit contract and deterministic failure prompts.

The v2 separated renderer receives only frozen ``data`` tables. It has no
``emit`` API. These tests pin the static preflight (AST based, never a
substring search), the runtime fallback for dynamically constructed calls,
and the valid legacy, v2 main-script and frozen-table replay paths.

The real paths run the in-container capture code with normal Matplotlib and
synthetic fixtures. No sandbox policy, permission or image is changed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from src.figures.core import capture_outputs
from src.figures.sandbox import LocalBackend, SandboxJob
from src.figures.script_outputs import (RenderScriptEmitUnavailable,
                                        classify_render_script_error,
                                        normalize_output_contract,
                                        render_script_emit_line, output_slots)
import pytest

from src.research import figure_tools
from src.research.script_presets import get_preset, list_presets

GOOD_RENDERER = (
    "frame = data['tidy']\n"
    "fig, ax = plt.subplots()\n"
    "ax.scatter(frame['x'], frame['y'])\n"
)


def _call_tool(**kwargs):
    """Call the preset tool's raw function, not the LangChain wrapper."""
    tool = figure_tools.run_research_figure_script
    function = getattr(tool, "func", tool)
    return function(**kwargs)


def _tool_help() -> str:
    tool = figure_tools.run_research_figure_script
    return (getattr(tool, "description", "")
            or getattr(getattr(tool, "func", None), "__doc__", ""))


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [2.0, 1.5, 3.5]})


def _contract(render_source: str, *, name: str = "relations") -> dict:
    return {
        "version": 2,
        "tables": [{"name": "tidy", "parents": ["data"]}],
        "figures": [{
            "name": name,
            "title": "变量关系",
            "formats": ["svg"],
            "parents": ["tidy"],
            "render_script": render_source,
        }],
    }


class StaticRenderScriptContractTests(unittest.TestCase):
    def test_worker_distinguishes_syntax_error_from_denied_import(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from src.figures import worker

        with tempfile.TemporaryDirectory() as scratch:
            job_path = Path(scratch) / "job.json"
            fixtures = [
                ("figure = (", worker.EXIT_SCRIPT_ERROR, "script_syntax"),
                ("import socket", worker.EXIT_FORBIDDEN, "forbidden_import"),
            ]
            for source, code, reason in fixtures:
                with self.subTest(reason=reason):
                    job_path.write_text(json.dumps({"script": source}), encoding="utf-8")
                    output = StringIO()
                    with redirect_stdout(output), redirect_stderr(StringIO()):
                        self.assertEqual(worker.run(job_path), code)
                    payload = json.loads(output.getvalue())
                    self.assertEqual(payload["diagnostic"]["stage"], "analysis")
                    self.assertEqual(payload["diagnostic"]["error_code"], reason)
                    if reason == "script_syntax":
                        self.assertGreaterEqual(payload["diagnostic"]["line"], 1)
                        self.assertGreaterEqual(payload["diagnostic"]["column"], 1)
                        self.assertNotIn("FIGURE_SCRIPT_FORBIDDEN", payload["message"])

    def test_frozen_renderer_refuses_a_path_before_execution(self):
        result = capture_outputs("", output_contract=_contract(GOOD_RENDERER), output_key="relations",
                                 frozen_tables={"tidy": Path("private-fixture-path.csv")})
        self.assertEqual(result.error_stage, "render")
        self.assertEqual(result.error_type, "input_type_mismatch")
        self.assertEqual(result.error_details["parameter"], "data['tidy']")
        self.assertEqual(result.error_details["expected"], "DataFrame")
        self.assertNotIn("private-fixture-path.csv", result.error)
        self.assertEqual(result.figures, {})

    def test_provable_emit_reference_is_rejected_with_stable_code(self):
        source = ("frame = data['tidy']\n"
                  "emit.table('tidy', frame, parents=['data'])\n")
        with self.assertRaises(RenderScriptEmitUnavailable) as caught:
            normalize_output_contract(_contract(source))
        self.assertEqual(caught.exception.reason_code,
                         "render_script_emit_not_available")
        self.assertEqual(caught.exception.line, 2)
        message = str(caught.exception)
        self.assertIn("第 2 行", message)
        self.assertIn("主 script", message)
        self.assertIn("emit.table", message)
        self.assertEqual(render_script_emit_line(source), 2)

    def test_comments_strings_and_local_bindings_are_not_rejected(self):
        commented = ("# emit.table('tidy', frame)\n"
                     "note = \"emit.figure(...)\"\n" + GOOD_RENDERER)
        self.assertIsNone(render_script_emit_line(commented))
        self.assertEqual(normalize_output_contract(_contract(commented))["version"], 2)
        local = ("emit = print\n"
                 "emit('debug')\n" + GOOD_RENDERER)
        self.assertIsNone(render_script_emit_line(local))
        normalize_output_contract(_contract(local))

    def test_zero_figure_and_legacy_v2_are_not_rejected(self):
        tables_only = {"version": 2,
                       "tables": [{"name": "tidy", "parents": ["data"]}],
                       "figures": []}
        self.assertEqual(normalize_output_contract(tables_only)["figures"], [])
        legacy = {"version": 2, "tables": [], "figures": [
            {"name": "f", "title": "t", "formats": ["svg"]}]}
        self.assertEqual(normalize_output_contract(legacy)["figures"][0]["name"], "f")


class PreflightFailureTests(unittest.TestCase):
    def test_rejection_does_not_select_or_start_a_worker(self):
        contract = _contract("emit.figure('relations', object())\n")
        with mock.patch.object(figure_tools, "_select_script_runner") as runner:
            content, payload = _call_tool(script="pass\n", outputs_json=contract)
        runner.assert_not_called()
        self.assertEqual(payload["_tool_outcome"], {
            "status": "not_run",
            "reason_code": "script_execution_rejected",
            "artifacts": [],
        })
        self.assertEqual(payload["error_code"], "render_script_emit_not_available")
        self.assertIn("RENDER_SCRIPT_EMIT_NOT_AVAILABLE", content)
        self.assertEqual(json.loads(content)["error_code"],
                         "render_script_emit_not_available")

    def test_repeated_preflight_rejection_is_deterministic_and_side_effect_free(self):
        contract = _contract("emit.table('tidy', frame)\n")
        with mock.patch.object(figure_tools, "_select_script_runner") as runner:
            first = _call_tool(script="pass\n", outputs_json=contract)
            second = _call_tool(script="pass\n", outputs_json=contract)
        runner.assert_not_called()
        self.assertEqual(first, second)


class RealCaptureContractTests(unittest.TestCase):
    def test_v2_main_script_and_separated_renderer_execute(self):
        contract = normalize_output_contract(_contract(GOOD_RENDERER))
        result = capture_outputs(
            "emit.table('tidy', data, parents=['data'])\n",
            output_contract=contract, namespace={"data": _frame()})
        self.assertEqual(result.error, "")
        self.assertIn("relations", result.figures)
        self.assertEqual(
            result.frozen_render_receipts["relations"]["version"], 1)

    def test_vector_heatmap_worker_exports_three_formats_from_one_frozen_matrix(self):
        import xml.etree.ElementTree as ET

        # A continuous colorbar also rasterizes by default in Matplotlib.
        script = ("matrix = data['tidy'].to_numpy()\n"
                  "fig, ax = plt.subplots()\n"
                  "mesh = ax.pcolormesh(matrix, cmap='viridis', shading='flat', rasterized=False)\n"
                  "ax.invert_yaxis()\n"
                  "colorbar = fig.colorbar(mesh, ax=ax, label='Measured value')\n"
                  "colorbar.solids.set_rasterized(False)\n"
                  "ax.set(xlabel='Variable', ylabel='Sample')\n")
        contract = _contract(script, name="heatmap")
        contract["figures"][0]["formats"] = ["svg", "png", "pdf"]
        contract = normalize_output_contract(contract)
        with tempfile.TemporaryDirectory() as root:
            from src.figures.dataset_codec import encode_frame
            frozen = Path(root) / "matrix.json"
            frozen.write_text(json.dumps(encode_frame(pd.DataFrame({"x": [1, 3, 5], "y": [2, 4, 6]}))),
                              encoding="utf-8")
            result = LocalBackend().run(SandboxJob(job_id="vector-heatmap-fixture",
                script="emit.table('tidy', load_dataset(data), parents=['data'])",
                inputs={"data": "matrix.json"}, sources={"data": str(frozen)},
                outputs=output_slots(contract), output_contract=contract))
        self.assertTrue(result.ok, result.message)
        files = {Path(filename).suffix: result.outputs[key] for key, filename in output_slots(contract).items()
                 if key in result.outputs}
        self.assertTrue(files['.png'].startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertTrue(files['.pdf'].startswith(b'%PDF-'))
        root = ET.fromstring(files['.svg'])
        tag = '{http://www.w3.org/2000/svg}'
        self.assertListEqual(list(root.iter(tag + 'image')), [])
        from matplotlib import colormaps
        from matplotlib.colors import to_hex
        expected = [to_hex(colormaps['viridis'](index / 5)) for index in range(6)]
        # The sanitizer strips IDs. A matrix group still has exactly six
        # individually editable paths in row-major order with the original colors.
        self.assertTrue(any([cell.get('fill') for cell in group.findall(tag + 'path')] == expected
                            for group in root.iter(tag + 'g')))

    def test_frozen_table_replay_executes_without_the_main_script(self):
        contract = normalize_output_contract(_contract(GOOD_RENDERER))
        result = capture_outputs(
            "raise AssertionError('main script must not run on replay')\n",
            output_contract=contract, output_key="relations",
            frozen_tables={"tidy": _frame()})
        self.assertEqual(result.error, "")
        self.assertIn("relations", result.figures)

    def test_worker_surfaces_a_bounded_diagnostic_for_a_path_emitted_as_a_table(self):
        contract = normalize_output_contract(_contract(GOOD_RENDERER))
        with tempfile.TemporaryDirectory() as root:
            csv = Path(root) / "fixture.csv"
            csv.write_bytes(b"x,y\n1,2\n")
            result = LocalBackend().run(SandboxJob(job_id="typed-render-fixture",
                script="emit.table('tidy', data, parents=['data'])",
                inputs={"data": "fixture.csv"}, sources={"data": str(csv)},
                outputs=output_slots(contract), output_contract=contract))
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostic, {
            "stage": "render", "error_code": "input_type_mismatch",
            "parameter": "data['tidy']", "expected": "DataFrame", "actual": "PosixPath" if os.name == "posix" else "WindowsPath",
            "inputs": {"data": "DataFrame mapping"},
            "line": 1,
        }, result.message)

    def test_legacy_single_figure_reads_a_frozen_dataset(self):
        from src.figures.dataset_codec import encode_frame

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "dataset.json"
            source.write_text(json.dumps(encode_frame(_frame())), encoding="utf-8")
            result = LocalBackend().run(SandboxJob(job_id="legacy-dataset-fixture",
                script="frame = load_dataset(tidy)\nfig, ax = plt.subplots()\nax.plot(frame['x'], frame['y'])\n",
                inputs={"tidy": "dataset.json"}, sources={"tidy": str(source)},
                outputs={"svg": "figure.svg"}))
        self.assertTrue(result.ok, result.message)
        self.assertIn(b"<svg", result.outputs["svg"])

    def test_legacy_v2_emit_figure_still_works(self):
        contract = normalize_output_contract({
            "version": 2, "tables": [], "figures": [
                {"name": "f", "title": "t", "formats": ["svg"]}]})
        result = capture_outputs(
            "fig, ax = plt.subplots()\n"
            "ax.plot([1, 2], [3, 4])\n"
            "emit.figure('f', fig)\n",
            output_contract=contract)
        self.assertEqual(result.error, "")
        self.assertIn("f", result.figures)

    def test_dynamic_emit_is_classified_at_the_runtime_boundary(self):
        source = ("eval(\"emit.table('tidy', 1)\")\n"
                  "fig, ax = plt.subplots()\n")
        self.assertIsNone(render_script_emit_line(source))
        contract = normalize_output_contract(_contract(source))
        result = capture_outputs(
            "emit.table('tidy', data, parents=['data'])\n",
            output_contract=contract, namespace={"data": _frame()})
        self.assertEqual(result.error_type, "render_script_emit_not_available")
        self.assertIn("RENDER_SCRIPT_EMIT_NOT_AVAILABLE", result.error)
        self.assertEqual(result.figures, {})
        self.assertIsNotNone(classify_render_script_error(
            "NameError: name 'emit' is not defined"))
        self.assertIsNone(classify_render_script_error(
            "NameError: name 'x' is not defined"))

    def test_real_worker_classifies_dynamic_emit(self):
        contract = normalize_output_contract(_contract(
            "eval(\"emit.table('tidy', 1)\")\n"
            "fig, ax = plt.subplots()\n"))
        main = ("import pandas as pd\n"
                "emit.table('tidy', pd.DataFrame({'x': [1.0, 2.0]}))\n")
        with tempfile.TemporaryDirectory() as root:
            work = Path(root) / "job"
            inbox = work / "in"
            outbox = work / "out"
            inbox.mkdir(parents=True)
            outbox.mkdir()
            job = {"job_id": "emit_dynamic", "script": main, "inputs": {},
                   "outputs": output_slots(contract), "output_contract": contract,
                   "qa_profile": {}, "render_mode": "initial", "output_key": ""}
            job_path = inbox / "job.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            environment = dict(os.environ)
            environment["MPLBACKEND"] = "Agg"
            environment["MPLCONFIGDIR"] = str(Path(root) / "mplconfig")
            Path(environment["MPLCONFIGDIR"]).mkdir(exist_ok=True)
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "src.figures.worker",
                 "--job", str(job_path)],
                capture_output=True, text=True, timeout=300,
                env=environment, cwd=str(work))
        self.assertEqual(completed.returncode, 10, completed.stderr[-2000:])
        message = json.loads(completed.stdout)["message"]
        self.assertIn("RENDER_SCRIPT_EMIT_NOT_AVAILABLE", message)
        self.assertIn("data['表名']", message)


class PresetRendererContractTests(unittest.TestCase):
    def test_builtin_renderers_hold_the_fixed_emit_contract(self):
        for preset in list_presets():
            full = get_preset(preset["script_ref"]["id"])
            for figure in full["outputs"]["figures"]:
                source = figure.get("render_script")
                if source:
                    self.assertIsNone(render_script_emit_line(source), figure["name"])
                    normalize_output_contract(full["outputs"])

    def test_provider_advertises_native_json_and_accepts_legacy_strings_at_ingress(self):
        from inspect import signature
        from src.agent.preset_catalog import canonical_tool_schema
        tool = figure_tools.run_research_figure_script
        schema = canonical_tool_schema(tool)['function']['parameters']['properties']
        self.assertEqual(set(schema), set(signature(tool.func).parameters))
        self.assertEqual(schema['outputs_json']['type'], 'object')
        self.assertEqual(schema['inputs_json']['type'], 'object')
        self.assertEqual(schema['qa_profile_json']['type'], 'object')
        self.assertEqual(schema['formats_json']['type'], 'array')
        for field in ('outputs_json', 'inputs_json', 'qa_profile_json', 'formats_json'):
            self.assertNotIn('default', schema[field])
            self.assertNotIn('anyOf', schema[field])
        outputs = schema['outputs_json']
        self.assertFalse(outputs['additionalProperties'])
        self.assertEqual(outputs['required'], ['version'])
        self.assertEqual(outputs['properties']['version']['enum'], [2])
        table = outputs['properties']['tables']['items']
        figure = outputs['properties']['figures']['items']
        self.assertFalse(table['additionalProperties'])
        self.assertFalse(figure['additionalProperties'])
        self.assertEqual(set(table['properties']), {'name', 'parents'})
        self.assertEqual(set(figure['properties']), {'name', 'title', 'formats', 'parents', 'render_script'})
        contract = _contract(GOOD_RENDERER)
        misplaced = _contract(GOOD_RENDERER)
        misplaced['figures'][0]['render_script_ref'] = 'ref'
        with self.assertRaisesRegex(ValueError, r'figures\[0\].*inline source'):
            normalize_output_contract(misplaced)
        self.assertEqual(tool.args_schema(outputs_json=contract).outputs_json, contract)
        self.assertEqual(tool.args_schema(outputs_json=json.dumps(contract)).outputs_json, json.dumps(contract))
        self.assertEqual(normalize_output_contract(figure_tools._json_arg(json.dumps(contract), 'outputs_json')),
                         normalize_output_contract(contract))

    def test_help_and_presets_describe_read_only_inspection_and_environments(self):
        help_text = _tool_help()
        self.assertIn("inspect_uploaded_table", help_text)
        self.assertIn("Docker", help_text)
        self.assertIn("WSL-Docker", help_text)
        self.assertIn("Apptainer", help_text)
        for preset in list_presets():
            self.assertIn("inspect_uploaded_table", preset["description"])
            self.assertIn("Docker", preset["description"])
            self.assertIn("Apptainer", preset["description"])


@pytest.mark.parametrize('source, contract, expected', [
    ("frame = data['tidy']", {'version': 2, 'figures': [{'name': 'plot'}]}, 'main_script_input_not_bound'),
    ("frame = load_dataset(data)", {'version': 2}, 'main_script_input_not_bound'),
    ("emit.figure('plot', fig)", {}, 'main_script_emit_not_available'),
])
def test_provably_missing_main_stage_binding_rejected_before_worker(source, contract, expected):
    with mock.patch.object(figure_tools, '_select_script_runner') as runner:
        _, result = _call_tool(script=source, inputs_json={'tidy': {'file_id': 'a' * 32}}, outputs_json=contract)
    runner.assert_not_called()
    assert result['error_code'] == expected
    assert result['_tool_outcome']['status'] == 'not_run'


@pytest.mark.parametrize('source, contract', [
    ("data = tidy\nframe = data['tidy']", {}),
    ("from something import *\nframe = data['tidy']", {}),
    ("globals()['data'] = tidy\nframe = data['tidy']", {}),
    ("if check:\n    frame = data['tidy']", {}),
    ("def helper(data):\n    return data['tidy']", {}),
    ("emit.figure('plot', fig)", {'version': 2, 'figures': [{'name': 'plot'}]}),
    ("from package import emit\nemit.figure('plot', fig)", {}),
    ("frame = load_dataset(tidy)", {}),
    ("frame = data['tidy']", {'input_contract': {'raw': ['tidy']}}),
])
def test_main_preflight_leaves_bound_or_uncertain_sources_to_worker(source, contract):
    from src.figures.script_outputs import validate_main_script_bindings
    validate_main_script_bindings(source, contract, {'tidy': {'file_id': 'a' * 32}})


if __name__ == "__main__":
    unittest.main()


def test_tool_preserves_declared_formats_and_applies_explicit_override():
    contract = _contract(GOOD_RENDERER)
    contract["figures"][0]["formats"] = ["svg", "png", "pdf"]
    with (mock.patch("src.figures.projects.snapshot_script_inputs", return_value=([], [])),
          mock.patch("src.figures.projects.script_replay_inputs", return_value=({}, {}, {})),
          mock.patch("src.research.script_execution.publish_script_outputs") as publish):
        for requested, expected in (("", ["svg", "png", "pdf"]), ('["pdf", "PNG"]', ["pdf", "png"])):
            figure_tools._run_research_figure_script(script="pass", outputs_json=contract,
                formats_json=requested, _sandbox_runner=object())
            assert publish.call_args.kwargs["contract"]["figures"][0]["formats"] == expected
    assert contract["figures"][0]["formats"] == ["svg", "png", "pdf"]
