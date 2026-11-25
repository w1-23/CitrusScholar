"""Tests for the fixed Nature CLI authority and exact accepted-output boundary."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError

from src.agent import nature_skill_tools as tools
from src.agent.identity import ExecutionIdentity
from src.code_execution.protocol import ExecutionRequest, FileSnapshot
from src.research import nature_skill_contract as contract
from src.research import nature_skill_worker as worker


class NatureSkillTest(TestCase):
    def setUp(self):
        self.identity = ExecutionIdentity(1, "session-fixture", "turn-fixture", "run-fixture")
        self.release = Path("/trusted-release")
        self.pin = contract.PinnedTemplate(b"vendor bytes", "e" * 64, "f" * 40, "d" * 64, self.release)
        self.ref = {"file_id": "a" * 32, "sha256": "b" * 64}
        self.raw = {"backend": "python", "input": self.ref,
                    "plot": {"template": "marginal", "x_col": "Dose α", "y_col": "--demo"}}
        self.spec = contract.RunArgs.model_validate(self.raw)

    def test_cli_has_no_paths_demo_or_position_inference(self):
        stem, outputs = contract.output_names("marginal", "nature_" + "4" * 64)
        self.assertEqual(outputs, tuple("/outputs/" + stem + suffix for suffix in
                                       (".svg", ".pdf", ".tiff", ".qa.json")))
        argv = contract.argv(self.spec, stem)
        self.assertIn("--y-col=--demo", argv)
        self.assertIn("--x-col=Dose α", argv)
        self.assertIn("--group-col=", argv)
        self.assertNotIn("--demo", argv)
        self.assertNotIn("--drop-incomplete", argv)
        for patch in ({"template": "roc", "fpr_col": "FPR"},
                      {"template": "roc", "fpr_col": "FPR", "tpr_cols": ["A,B"]},
                      {"template": "roc", "fpr_col": "FPR", "tpr_cols": ["FPR"]},
                      {"template": "marginal", "x_col": "same", "y_col": "same"},
                      {"template": "marginal", "x_col": "X", "y_col": "Y", "demo": True}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                contract.RunArgs.model_validate({**self.raw, "plot": patch})

    def test_r_explains_limitation_without_queue_or_fallback(self):
        with patch.object(tools, "_bound", return_value=(self.identity, self.release, self.pin)), \
                patch.object(worker, "schedule_plot") as schedule:
            _, artifact = tools.run_nature_plot.func(**{**self.raw, "backend": "r"})
        schedule.assert_not_called()
        self.assertEqual(artifact["status"], "unsupported")
        self.assertNotIn("_code_execution_id", artifact)

    def test_native_tool_only_submits_and_uncertainty_is_not_replayable(self):
        with patch.object(tools, "_bound", return_value=(self.identity, self.release, self.pin)), \
                patch.object(worker, "schedule_plot", return_value={"execution_id": "c" * 32, "status": "queued"}) as submit:
            message = tools.run_nature_plot.invoke({"type": "tool_call", "id": "nature-test-call",
                                                    "name": "run_nature_plot", "args": self.raw})
            self.assertEqual(submit.call_args.kwargs["spec"], self.spec)
        self.assertEqual(message.artifact["_code_execution_id"], "c" * 32)
        self.assertEqual(message.artifact["completion_scope"], "submission_only")
        self.assertEqual(message.artifact["_tool_outcome"]["artifacts"], [])
        with patch.object(tools, "_bound", return_value=(self.identity, self.release, self.pin)), \
                patch.object(worker, "schedule_plot", side_effect=worker.NatureSubmissionUncertain):
            _, unknown = tools.run_nature_plot.func(**self.raw)
        self.assertEqual(unknown["_tool_outcome"]["status"], "unknown")
        self.assertNotIn("_code_execution_id", unknown)

    def test_complete_real_counts_and_exact_mapping_required(self):
        qa = {"template": "marginal", "backend": "python", "demo": False,
              "input": "observations.csv", "output_prefix": "plot",
              "mapping": {"x": "Dose α", "y": "--demo", "group": ""},
              "rows_input": 8, "rows_plotted": 8, "excluded_rows": 0, "excluded_entities": 0}
        encode = lambda value: json.dumps(value).encode()
        self.assertEqual(contract.template_qa(encode(qa), self.spec, "plot")["rows_plotted"], 8)
        for change in ({"demo": True}, {"rows_plotted": 7}, {"rows_plotted": 7, "excluded_rows": 1},
                       {"mapping": {"x": "other", "y": "--demo", "group": ""}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                contract.template_qa(encode({**qa, **change}), self.spec, "plot")

    def test_cross_run_wrong_sha_and_extra_deliverable_rejected(self):
        task, workspace = "nature_" + "9" * 64, "c" * 32
        stem, paths = contract.output_names("marginal", task)
        request = ExecutionRequest(self.identity, task, workspace, 3, task, task,
            FileSnapshot(contract.SCRIPT, "e" * 32, 1, self.pin.sha256, len(self.pin.content)),
            inputs=(FileSnapshot(contract.CSV, self.ref["file_id"], 1, self.ref["sha256"], 123),),
            arguments=contract.argv(self.spec, stem), outputs=paths, limits=worker.LIMITS)
        manifest = {"task_id": task, "metadata": worker._meta(self.pin, self.spec)}
        service = SimpleNamespace(workspace=SimpleNamespace(open=lambda _: manifest))
        self.assertEqual(worker._bound_request(service, self.identity,
                          {"request": request.serialize()}, self.pin)[1], self.spec)
        for change in ({"identity": {**request.as_dict()["identity"], "run_id": "different"}},
                       {"outputs": [*paths[:-1], "/outputs/extra.json"]},
                       {"inputs": [{**request.as_dict()["inputs"][0], "sha256": "1" * 64}]}):
            with self.subTest(change=change), self.assertRaises(PermissionError):
                worker._bound_request(service, self.identity,
                                      {"request": json.dumps({**request.as_dict(), **change})}, self.pin)

    def test_input_scope_requires_live_public_attached_hash_and_csv(self):
        accepted = {"visibility": "public", "origin": "uploaded", "filename": "table.csv",
                    "size_bytes": 62, "sha256": self.ref["sha256"]}
        with patch.object(worker.files, "get_task_input_file", return_value=accepted) as lookup:
            self.assertEqual(worker._source(self.identity, self.spec.input), {**self.ref, "size_bytes": 62})
            lookup.assert_called_once_with(self.identity.owner_user_id, self.identity.run_id,
                                           self.ref["file_id"], self.ref["sha256"])
        for rejected in [None, {**accepted, "visibility": "internal"}, {**accepted, "sha256": "bad"},
                         {**accepted, "filename": "table.xlsx"}]:
            with self.subTest(rejected=rejected), patch.object(worker.files, "get_task_input_file", return_value=rejected), \
                    self.assertRaises((PermissionError, ValueError)):
                worker._source(self.identity, self.spec.input)

    def test_registered_tiff_requires_expected_mime_and_signature(self):
        from src.research.tools import _artifact_format_matches

        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "plot.tiff"
            for header in (b"II*\x00\x08\x00\x00\x00", b"MM\x00*\x00\x00\x00\x08"):
                target.write_bytes(header + b"data")
                self.assertTrue(_artifact_format_matches(target, target.name, "image/tiff"))
                self.assertFalse(_artifact_format_matches(target, target.name, "image/png"))
            target.write_bytes(b"not an image")
            self.assertFalse(_artifact_format_matches(target, target.name, "image/tiff"))
