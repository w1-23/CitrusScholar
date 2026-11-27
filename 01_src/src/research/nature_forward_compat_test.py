"""F-04: the deprecated Nature tools forward; they never schedule a second chain.

The evidence this file pins:

* the tool returns the *unified* execution id and publishes nothing itself;
* ``schedule_plot`` calls the template bridge instead of a Code Worker, so no
  second submission record exists;
* the forwarded typed parameters are accepted by the shared template registry;
* ``collect_plot`` reads that same unified run and re-verifies the links;
* R stays explicitly unsupported rather than silently becoming Python.
"""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from citrus_figures import template_registry

from src.agent import nature_skill_tools as tools
from src.agent.identity import ExecutionIdentity
from src.research import nature_skill_contract as contract
from src.research import nature_skill_worker as worker

RAW = {"backend": "python", "input": {"file_id": "a" * 32, "sha256": "b" * 64},
       "plot": {"template": "marginal", "x_col": "Dose", "y_col": "Response"}}
URI = "/api/v2/session/session-fixture/artifacts/" + "c" * 32


def _artifact():
    return {"run_id": "9" * 32,
            "_tool_outcome": {"status": "complete", "artifacts": [URI]},
            "figures": [{"figure_id": "1" * 32, "workspace_url": "/figures?project=" + "1" * 32,
                         "exports": [{"format": "svg", "filename": "figure.svg",
                                      "artifact_url": URI}]}]}


class NatureForwardTest(TestCase):
    def setUp(self):
        self.identity = ExecutionIdentity(1, "session-fixture", "turn-fixture", "run-fixture")
        self.pin = contract.PinnedTemplate(b"vendor bytes", "e" * 64, "f" * 40, "d" * 64, Path("/release"))
        self.release = Path("/release")
        self.source = {"file_id": "a" * 32, "sha256": "b" * 64, "size_bytes": 10}

    def test_deprecated_tool_reports_the_unified_execution_id_only(self):
        with patch.object(tools, "_bound", return_value=(self.identity, self.release, self.pin)), \
                patch.object(worker, "authorize_fusion_code"), \
                patch.object(worker, "_source", return_value=self.source), \
                patch.object(worker, "_service") as service, \
                patch("src.figures.template_execution.execute_template",
                      return_value=("{}", _artifact())) as forward:
            _content, artifact = tools.run_nature_plot.func(**RAW)
        forward.assert_called_once()
        forwarded = forward.call_args.kwargs
        self.assertEqual(forwarded["template_id"], "marginal")
        self.assertEqual(forwarded["file_id"], "a" * 32)
        self.assertEqual(forwarded["parameters"],
                         contract.template_request(contract.RunArgs.model_validate(RAW))[1])
        service.assert_not_called()
        self.assertEqual(artifact["execution_id"], "9" * 32)
        self.assertEqual(artifact["forwarded_to"], "run_research_figure_script")
        self.assertNotIn("_code_execution_id", artifact)
        self.assertEqual(artifact["completion_scope"], "complete")
        self.assertEqual(artifact["figure_id"], "1" * 32)
        self.assertEqual(artifact["_tool_outcome"]["artifacts"], [URI])
        self.assertTrue(artifact["deprecated"])

    def test_forwarded_parameters_are_the_registry_contract(self):
        template_id, parameters = contract.template_request(contract.RunArgs.model_validate(RAW))
        model = template_registry.parse(template_id, parameters)
        self.assertEqual((model.template, model.x_col, model.y_col, model.group_col),
                         ("marginal", "Dose", "Response", ""))
        for raw in ({**RAW, "plot": {"template": "volcano", "gene_col": "g",
                                     "effect_col": "e", "p_col": "p"}},
                    {**RAW, "plot": {"template": "roc", "fpr_col": "f",
                                     "tpr_cols": ["a", "b"]}},
                    {**RAW, "plot": {"template": "paired", "id_col": "i",
                                     "condition_col": "c", "value_col": "v"}},
                    {**RAW, "plot": {"template": "dotplot", "row_col": "r",
                                     "column_col": "c", "size_col": "s", "color_col": "k"}}):
            template_id, parameters = contract.template_request(contract.RunArgs.model_validate(raw))
            self.assertEqual(template_registry.parse(template_id, parameters).template, template_id)

    def test_schedule_plot_never_schedules_a_code_execution(self):
        with patch.object(worker, "authorize_fusion_code"), \
                patch.object(worker, "_source", return_value=self.source), \
                patch.object(worker, "_service") as service, \
                patch("src.figures.template_execution.execute_template",
                      return_value=("{}", _artifact())):
            row = worker.schedule_plot(self.identity, release=self.release,
                                       pin=self.pin, spec=contract.RunArgs.model_validate(RAW))
        service.assert_not_called()
        self.assertEqual(row["execution_id"], "9" * 32)
        self.assertTrue(row["forwarded"])
        self.assertEqual(row["forwarded_to"], "run_research_figure_script")

    def test_a_forward_without_a_run_id_is_not_an_execution(self):
        broken = {"_tool_outcome": {"status": "not_run", "reason_code": "figures_sandbox_unavailable"}}
        with patch.object(worker, "authorize_fusion_code"), \
                patch.object(worker, "_source", return_value=self.source), \
                patch("src.figures.template_execution.execute_template",
                      return_value=("{}", broken)):
            with self.assertRaises(LookupError) as caught:
                worker.schedule_plot(self.identity, release=self.release,
                                     pin=self.pin, spec=contract.RunArgs.model_validate(RAW))
        self.assertEqual(str(caught.exception), "figures_sandbox_unavailable")

    def test_collect_reads_the_same_unified_run_and_publishes_nothing(self):
        run = {"status": "succeeded", "result": {
            "template": {"template_id": "marginal"},
            "figures": [{"figure_id": "1" * 32, "workspace_url": "/figures?project=" + "1" * 32,
                         "exports": [{"format": "svg", "filename": "figure.svg",
                                      "artifact_url": URI}]}]}}
        receipts = []
        with patch.object(worker, "authorize_fusion_code"), \
                patch("src.research.script_assets.get_run", return_value=run) as read, \
                patch("src.research.tools.validate_artifact_links",
                      return_value=[{"valid": True}]) as validated:
            data, status, reason = worker.collect_plot(
                self.identity, release=self.release, pin=self.pin,
                execution_id="9" * 32, receipts=receipts)
        read.assert_called_once_with(self.identity.owner_user_id, self.identity.session_id, "9" * 32)
        validated.assert_called_once()
        self.assertEqual((status, reason), ("complete", "nature_plot_originals_verified"))
        self.assertEqual(data["execution_id"], "9" * 32)
        self.assertEqual(data["verified_original_links"], [URI])
        self.assertEqual(receipts, [{"url": URI}])
        self.assertEqual(data["provenance"]["unified_run_id"], "9" * 32)

    def test_collect_refuses_an_unknown_or_unverified_execution(self):
        with patch.object(worker, "authorize_fusion_code"), \
                patch("src.research.script_assets.get_run", side_effect=LookupError("missing")):
            data, status, reason = worker.collect_plot(
                self.identity, release=self.release, pin=self.pin,
                execution_id="9" * 32, receipts=[])
        self.assertEqual((status, reason), ("not_run", "nature_plot_collection_unavailable"))
        self.assertNotIn("original_exports", data)

        run = {"status": "succeeded", "result": {"figures": [
            {"exports": [{"format": "svg", "filename": "figure.svg", "artifact_url": URI}]}]}}
        with patch.object(worker, "authorize_fusion_code"), \
                patch("src.research.script_assets.get_run", return_value=run), \
                patch("src.research.tools.validate_artifact_links", return_value=[{"valid": False}]):
            _data, status, reason = worker.collect_plot(
                self.identity, release=self.release, pin=self.pin,
                execution_id="9" * 32, receipts=[])
        self.assertEqual((status, reason), ("not_run", "nature_plot_collection_unavailable"))

    def test_r_backend_is_unsupported_and_never_replaced_by_python(self):
        spec = contract.RunArgs.model_validate({**RAW, "backend": "r"})
        with patch.object(worker, "authorize_fusion_code"), \
                patch.object(worker, "nature_skill_r") as r:
            r.require_template.return_value = None
            r.available.return_value = True
            with self.assertRaises(LookupError) as caught:
                worker.schedule_plot(self.identity, release=self.release, pin=self.pin, spec=spec)
        self.assertEqual(str(caught.exception), worker.R_NOT_UNIFIED)
        with patch.object(tools, "_bound", return_value=(self.identity, self.release, self.pin)), \
                patch.object(worker, "authorize_fusion_code"), \
                patch.object(worker, "nature_skill_r") as r:
            r.require_template.return_value = None
            r.available.return_value = True
            _content, artifact = tools.run_nature_plot.func(**{**RAW, "backend": "r"})
        self.assertEqual(artifact["_tool_outcome"]["reason_code"], "nature_plot_r_backend_not_unified")
        self.assertEqual(artifact["status"], "unsupported")

    def test_policy_budget_replaced_the_legacy_ninety_seconds(self):
        from citrus_figures import timeouts

        self.assertEqual(worker.LIMITS.timeout_sec, timeouts.resolve_timeout(kind="template"))
        self.assertIn(90, worker.HISTORICAL_TIMEOUTS)  # old receipts stay readable
        self.assertNotEqual(worker.LIMITS.timeout_sec, 90)
