"""Regression tests for figure input and source preflight failures."""
from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import ToolMessage

from src.core import jobs, task_events, tracing
from src.research import script_runtime
from src.tools import registry


OWNER = 7
SESSION = "fixture-session"
JOB = "fixture-job"


class ScriptRuntimeRejectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(tracing, "get_user_id", return_value=OWNER))
        self.enterContext(patch.object(tracing, "get_session_id", return_value=SESSION))
        self.enterContext(patch.object(tracing, "get_job_id", return_value=JOB))
        self.enterContext(patch.object(tracing, "get_client_request_id", return_value="request"))
        self.enterContext(patch("src.core.account_leases.account_lease", return_value=nullcontext()))

    def test_input_preflight_failure_does_not_save_source_or_reserve_run(self):
        executor = Mock()
        with (patch.object(script_runtime, "_validate_new_script_inputs", side_effect=ValueError("bad input")),
              patch("src.research.script_assets.save_script") as save,
              patch("src.research.script_assets.begin_budgeted_run") as begin):
            content, payload = script_runtime.execute_script_request(
                executor, script="print('x')\n", inputs_json='{"data":{"file_id":"' + "a" * 32 + '"}}')
        executor.assert_not_called()
        save.assert_not_called()
        begin.assert_not_called()
        self.assertEqual(json.loads(content), payload)
        self.assertEqual(payload["_tool_outcome"]["status"], "not_run")
        self.assertEqual(payload["failure_phase"], "preflight")
        self.assertFalse(payload["script_persisted"])
        self.assertFalse(payload["run_reserved"])

    def test_missing_bound_file_is_rejected_before_source_persistence(self):
        executor = Mock()
        resolved = {"data": {"file_id": "a" * 32}}
        with (patch("src.figures.projects.normalize_script_bindings", return_value=resolved),
              patch("src.session.files.get_account_file", return_value=None),
              patch("src.research.script_assets.save_script") as save,
              patch("src.research.script_assets.begin_budgeted_run") as begin):
            content, payload = script_runtime.execute_script_request(
                executor, script="print('x')\n", inputs_json='{"data":{"file_id":"' + "a" * 32 + '"}}')
        executor.assert_not_called()
        save.assert_not_called()
        begin.assert_not_called()
        self.assertEqual(json.loads(content), payload)
        self.assertEqual(payload["_tool_outcome"]["status"], "not_run")
        self.assertEqual(payload["failure_phase"], "preflight")
        self.assertIn("输入文件不存在", payload["error"])

    def test_contract_rejection_after_source_save_is_source_persisted_not_run(self):
        executor = Mock()
        saved = {"script_ref": {"kind": "research_script", "id": "b" * 32,
                                "version": 1, "sha256": "c" * 64}, "source": "print('x')\n"}
        with (patch.object(script_runtime, "_validate_new_script_inputs"),
              patch("src.research.script_assets.save_script", return_value=saved) as save,
              patch("src.research.script_assets.begin_budgeted_run") as begin,
              patch("src.figures.script_outputs.normalize_output_contract",
                    side_effect=ValueError("bad contract"))):
            content, payload = script_runtime.execute_script_request(executor, script="print('x')\n")
        executor.assert_not_called()
        save.assert_called_once()
        begin.assert_not_called()
        self.assertEqual(json.loads(content), payload)
        self.assertEqual(payload["_tool_outcome"]["status"], "not_run")
        self.assertEqual(payload["failure_phase"], "source_persisted")
        self.assertTrue(payload["script_persisted"])
        self.assertFalse(payload["run_reserved"])
        self.assertEqual(payload["script_ref"], saved["script_ref"])

    def test_resolved_script_reference_binding_failure_has_no_worker_or_run(self):
        executor = Mock()
        for source, contract, code in (
                ("frame = data['tidy']", {"version": 2, "figures": [{"name": "plot"}]}, "main_script_input_not_bound"),
                ("emit.figure('plot', fig)", {}, "main_script_emit_not_available")):
            with self.subTest(code=code):
                ref = {"kind": "research_script", "id": "b" * 32, "version": 1, "sha256": "c" * 64}
                with (patch.object(script_runtime, "_validate_new_script_inputs"),
                      patch("src.research.script_assets.get_script", return_value={"script_ref": ref, "source": source}) as get,
                      patch("src.research.script_assets.save_script") as save,
                      patch("src.research.script_assets.begin_budgeted_run") as begin,
                      patch("src.research.script_reconciliation.observe_preflight_rejection") as proof):
                    _, payload = script_runtime.execute_script_request(executor, script_ref_json=ref,
                        inputs_json={"tidy": {"file_id": "a" * 32}}, outputs_json=contract)
                get.assert_called_once_with(OWNER, SESSION, ref, touch=True)
                save.assert_not_called()
                begin.assert_not_called()
                executor.assert_not_called()
                proof.assert_called_once_with(payload)
                self.assertEqual(payload["error_code"], code)
                self.assertEqual(payload["script_ref"], ref)
                self.assertFalse(payload["run_reserved"])
                self.assertEqual(payload["_tool_outcome"]["status"], "not_run")

    def test_succeeded_run_cannot_be_used_as_repair_source(self):
        executor = Mock()
        with (patch("src.research.script_assets.get_run",
                    return_value={"status": "succeeded", "result": {}}),
              patch("src.research.script_assets.save_script") as save):
            content, payload = script_runtime.execute_script_request(
                executor, script="print('x')\n", repair_from_run_id="d" * 32)
        executor.assert_not_called()
        save.assert_not_called()
        self.assertEqual(json.loads(content), payload)
        self.assertEqual(payload["_tool_outcome"]["status"], "not_run")
        self.assertEqual(payload["failure_phase"], "preflight")
        self.assertIn("already succeeded", payload["error"])
        self.assertIn("new run", payload["error"])


class ScriptRuntimeRegistryProofTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(tracing, "get_user_id", return_value=OWNER))
        self.enterContext(patch.object(tracing, "get_session_id", return_value=SESSION))
        self.enterContext(patch.object(tracing, "get_job_id", return_value=JOB))
        self.enterContext(patch.object(tracing, "get_client_request_id", return_value="request"))
        self.enterContext(patch.object(jobs, "get_job", return_value={"session_id": SESSION, "owner_user_id": OWNER}))
        self.enterContext(patch.object(task_events, "start_operation",
                                       return_value=("operation-one", "lease")))
        self.enterContext(patch.object(task_events, "finish_operation"))
        self.enterContext(patch("src.core.account_leases.account_lease", return_value=nullcontext()))

    async def test_input_preflight_proof_prevents_unknown_upgrade(self):
        async def execute(_tool, call, **_kwargs):
            content, payload = script_runtime.execute_script_request(
                Mock(), script="print('x')\n", inputs_json='{"data":{"file_id":"' + "a" * 32 + '"}}')
            return ToolMessage(content=content, artifact=payload, name=call["name"],
                               tool_call_id=call["id"], additional_kwargs={"execution_dispatched": True})

        tool = SimpleNamespace(name="run_research_figure_script")
        call = {"name": tool.name, "id": "call-one", "args": {}}
        with (patch.object(script_runtime, "_validate_new_script_inputs",
                           side_effect=ValueError("bad input")),
              patch("src.research.script_assets.save_script") as save,
              patch.object(registry, "_execute_single_tool", execute)):
            message = await registry.run_tool_operation(tool, call, require_durable=True)
        save.assert_not_called()
        self.assertEqual(message.artifact["_tool_outcome"], {
            "status": "not_run", "reason_code": "script_execution_rejected", "artifacts": []})
        self.assertIn("host_preflight_reconciled", message.additional_kwargs)
        self.assertNotIn("operation_uncertain", message.additional_kwargs)
