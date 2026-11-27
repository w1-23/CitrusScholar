"""Original evidence survives delivery, SQLite reopen, and scoped paging."""
import hashlib
import json
import random
import string
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from src.agent.execution import ToolExecution
from src.agent.host_tools import HostTools
from src.agent.identity import ExecutionIdentity
from src.agent.framework_test_support import invoke_model_once
from src.agent.model import ModelDriver
from src.agent.runtime import AdmissionStopped
from src.core import jobs, progress_bus, task_events, tool_results, tracing
from src.core.context_manager import ContextManager
from src.core.provider_transport import AsyncDeepSeekTransport
from langchain_openai import ChatOpenAI
from src.core.provider_protocol import _DeepSeekMessage, _ProtocolState
from src.core.request_context import model_request
from src.core.tool_result_view import FIRST_DELIVERY_BYTES, first_delivery
from src.research.result_tools import read_tool_result
from src.session import manager as sessions


def oversized_controls():
    body = {"status": "complete", "next_action": "read", "files": ["a" * 2400],
            "exports": ["b" * 2400], "rows": ""}
    size = len(json.dumps(body, separators=(",", ":")))
    body["rows"] = "R" * (104_846 - size)
    return json.dumps(body, separators=(",", ":"))


class FixtureCatalog:
    def schemas(self, allowed):
        assert allowed == {"analyze"}
        return [{"type": "function", "function": {"name": "analyze", "description": "Synthetic analysis",
                 "parameters": {"type": "object", "properties": {}}}}]

    def execution_mode(self, name):
        assert name == "analyze"
        return "interaction"


class ToolResultDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(sessions, "STATE_DIR", root))
        self.enterContext(patch.object(sessions, "DB_PATH", root / "history.sqlite3"))
        self.enterContext(patch.object(sessions.SessionManager, "_instance", None))
        self.enterContext(patch.object(tool_results, "_schema_ready", False))
        self.session_store = sessions.SessionManager()
        self.enterContext(patch.object(sessions, "session_manager", self.session_store))
        self.sid = await self.session_store.get_or_create_session(None, 7)
        self.previous_identity = (tracing.get_user_id(), tracing.get_session_id())
        self.addCleanup(tracing.set_user_id, self.previous_identity[0])
        self.addCleanup(tracing.set_session_id, self.previous_identity[1])
        tracing.set_user_id(7)
        tracing.set_session_id(self.sid)

    def delivery(self, call_id, content, *, tool_name="analyze", args=None):
        reference = tool_results.record_with_reference(tool_call_id=call_id, session_id=self.sid,
            request_id="request", tool_name=tool_name, status="complete", result_text=content)
        self.assertIsNotNone(reference)
        self.assertTrue(tool_results.pin_references(self.sid, [call_id]))
        original = ToolMessage(content=content, name=tool_name, tool_call_id=call_id,
                               additional_kwargs={"result_reference": reference},
                               artifact={"op_receipt": {"status": "complete"}})
        return first_delivery(original, call={"args": args or {}}, reader_available=True)

    def serialized(self, text):
        return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))

    async def test_combined_controls_keep_bounded_verified_reference(self):
        original = oversized_controls()
        self.assertEqual(len(original), 104_846)
        result = self.delivery("aggregate-controls", original)
        self.assertLessEqual(self.serialized(result.content), FIRST_DELIVERY_BYTES)
        view = json.loads(result.content)
        self.assertEqual(view["tool_result_ref"]["full_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(view["control_observation"]["status"], "complete")
        self.assertEqual(view["control_observation"]["next_action"], "read")
        self.assertEqual(view["read_with"]["tool"], "read_tool_result")
        self.assertEqual(tool_results.fetch_for_current_request(result.tool_call_id)["result_text"], original)

    async def test_large_figure_receipt_delivers_exact_reuse_and_actual_outcome(self):
        script_ref = {"kind": "research_script", "id": "a" * 32, "version": 2, "sha256": "b" * 64}
        dataset_ref = {**script_ref, "kind": "dataset_snapshot", "id": "c" * 32}
        artifact_url = "/api/artifact/example/real.svg"
        for status, stage, artifacts in (("partial", "figure_published", [artifact_url]),
                                         ("failed", "figure_unpublished", [])):
            with self.subTest(status=status):
                receipt = {"report": "long QA" * 10_000,
                    "deliverable_stage": stage, "execution_stage": "worker_receipt_verified",
                    "script_ref": script_ref, "run_id": "d" * 32, "dataset_refs": [dataset_ref],
                    "_tool_outcome": {"status": status, "reason_code": "script_outputs_incomplete",
                                      "artifacts": artifacts},
                    "figures": [{"name": "f", "status": status, "delivery_stage": "rendered",
                                 "reason_code": "figure_verification_failed", "qa": "detail" * 9000}]}
                content = json.dumps(receipt)
                result = self.delivery("figure-" + status, content, tool_name="run_research_figure_script")
                view = json.loads(result.content)["control_observation"]
                self.assertLessEqual(self.serialized(result.content), FIRST_DELIVERY_BYTES)
                self.assertEqual(view["script_ref"], script_ref)
                self.assertEqual(view["dataset_refs"], [dataset_ref])
                self.assertEqual(view["run_id"], receipt["run_id"])
                self.assertEqual(view["deliverable_stage"], stage)
                self.assertEqual(view["_tool_outcome"], receipt["_tool_outcome"])
                self.assertEqual(view["figure_stages"][0]["reason_code"], "figure_verification_failed")
                self.assertEqual(tool_results.fetch_for_current_request(result.tool_call_id)["result_text"], content)

    async def test_workspace_list_discovers_usable_refs_without_source_readback(self):
        from contextlib import nullcontext
        from src import config
        from src.research import script_assets, script_presets
        from src.research.workspace_tools import research_workspace

        with (patch.object(config, "PROJECT_ROOT", Path(self.session_store.db_path).parent),
              patch.object(script_assets, "_lease", return_value=nullcontext())):
            source = "# synthetic private source marker\nprint(2)\n"
            private = script_assets.save_script(7, self.sid, name="private.py", source=source)
            scripts = [private["script_ref"]]
            for index in range(1, 15):
                saved = script_assets.save_script(7, self.sid, name=f"fixture_{index}.py", source=f"print({index})")
                scripts.insert(0, saved["script_ref"])
            args = {"operation": "list"}
            content = research_workspace.invoke(args)
            self.assertNotIn("render_script", content)
            self.assertNotIn("private source marker", content)
            delivered = self.delivery("workspace-list", content, tool_name="research_workspace", args=args)
            self.assertLessEqual(self.serialized(delivered.content), FIRST_DELIVERY_BYTES)
            view = json.loads(delivered.content)["workspace_overview"]
            presets = script_presets.list_presets()
            self.assertEqual([item["script_ref"] for item in view["presets"]],
                             [item["script_ref"] for item in presets])
            self.assertEqual(view["presets"][1]["parameter_names"], list(presets[1]["parameters"]))
            self.assertEqual(view["scripts"][0]["script_ref"], scripts[0])
            self.assertGreater(view["scripts_omitted"], 0)
            detailed = json.loads(research_workspace.invoke({"operation": "read", "script_ref": scripts[-1]}))
            self.assertEqual(detailed["source"], source)
            builtin = json.loads(research_workspace.invoke({"operation": "read", "script_ref": presets[1]["script_ref"]}))
            self.assertIn("render_script", builtin["outputs"]["figures"][0])
            tracing.set_user_id(8)
            self.addCleanup(tracing.set_user_id, 7)
            other = json.loads(research_workspace.invoke({"operation": "read", "script_ref": scripts[-1]}))
            self.assertNotIn("source", other)
            self.assertEqual(other["_tool_outcome"]["status"], "failed")

    async def test_escaped_surrogate_in_field_name_keeps_verified_reference(self):
        original = json.dumps({"\ud800": "x" * 5000, "status": "complete"})
        result = self.delivery("unpaired-surrogate", original)
        self.assertLessEqual(self.serialized(result.content), FIRST_DELIVERY_BYTES)
        view = json.loads(result.content)
        self.assertEqual(view["control_observation"]["status"], "complete")
        self.assertEqual(view["tool_result_ref"]["full_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(tool_results.fetch_for_current_request(result.tool_call_id)["result_text"], original)

    async def test_oversized_invalid_reference_never_returns_full_payload(self):
        content = json.dumps({"rows": "字😀\n\\" * 140_000}, ensure_ascii=False)
        self.assertGreater(len(content.encode()), 1_000_000)
        reference = tool_results.record_with_reference(tool_call_id="large", session_id=self.sid,
            request_id="request", tool_name="analyze", status="complete", result_text=content)
        self.assertIsNotNone(reference)
        variants = [
            (None, True, 7, "result_storage_unavailable"),
            ({**reference, "storage": {"complete": False}}, True, 7, "result_storage_unavailable"),
            ({**reference, "full_sha256": "0" * 64}, True, 7, "result_storage_unavailable"),
            (reference, True, 8, "result_storage_unavailable"),
            (reference, False, 7, "result_readback_unavailable"),
            (reference, True, 7, "stored"),
        ]
        for proof, reader, owner, expected in variants:
            with self.subTest(reader=reader, owner=owner, expected=expected):
                tracing.set_user_id(owner)
                msg = ToolMessage(content=content, name="analyze", tool_call_id="large",
                                  additional_kwargs={"result_reference": proof})
                actual = first_delivery(msg, call={}, reader_available=reader)
                self.assertLessEqual(self.serialized(actual.content), FIRST_DELIVERY_BYTES)
                view = json.loads(actual.content)
                self.assertEqual((actual.tool_call_id, actual.name), (msg.tool_call_id, msg.name))
                if expected == "stored":
                    self.assertIn(view["delivery"], {"stored_result_reference", "stored_result_with_partial_preview"})
                    self.assertEqual(view["tool_result_ref"]["full_sha256"], reference["full_sha256"])
                else:
                    self.assertEqual(view["delivery"], expected)
                    self.assertNotIn("tool_result_ref", view)
                    if expected == "result_storage_unavailable":
                        self.assertNotIn("result_reference", actual.additional_kwargs)
        inline = ToolMessage(content="x" * 2000, name="analyze", tool_call_id="inline")
        self.assertIs(first_delivery(inline, call={}, reader_available=False), inline)

    async def test_readback_is_exact_within_page_budget_and_never_unbounded(self):
        content = json.dumps({"text": "字😀" * 1000}, ensure_ascii=False)
        msg = ToolMessage(content=content, name="read_tool_result", tool_call_id="page")
        self.assertIs(first_delivery(msg, call={}, reader_available=True), msg)
        long = msg.model_copy(update={"content": "字😀" * 12_000})
        stopped = first_delivery(long, call={}, reader_available=True)
        self.assertEqual(json.loads(stopped.content)["delivery"], "result_page_too_large")
        self.assertLessEqual(self.serialized(stopped.content), FIRST_DELIVERY_BYTES)

    async def test_ten_and_thirty_outputs_are_bounded_in_real_adapter_payload(self):
        body = '{"status":"complete","next_action":"read","counts":{"rows":1200},"data":"' + "F" * 10240 + '"}'
        calls = [{"name": "analyze", "args": {}, "id": f"call-{i}"} for i in range(30)]
        messages = [self.delivery(call["id"], body) for call in calls]
        identity = ExecutionIdentity(7, self.sid, "request", "run")
        model = ChatOpenAI(model="fixture", api_key="fixture", max_retries=0, max_tokens=4096,
                          base_url="http://127.0.0.1:9/v1")
        driver = ModelDriver(FixtureCatalog())
        for count in (10, 30):
            with self.subTest(count=count):
                view = [HumanMessage(content="Analyze synthetic data"),
                        AIMessage(content="", tool_calls=calls[:count]), *messages[:count]]
                payload, estimate = model_request(model.bind_tools(driver._schemas_for({"analyze"})), view)
                self.assertTrue(payload["tools"][0]["function"]["strict"])
                self.assertEqual(estimate["tool_message_count"], count)
                self.assertEqual(estimate["output_tokens"], 4096)
                self.assertLess(estimate["estimated_input_tokens"], 48000)
                tool_views = [m for m in payload["messages"] if m["role"] == "tool"]
                self.assertLessEqual(sum(self.serialized(m["content"]) for m in tool_views),
                                     count * FIRST_DELIVERY_BYTES)
                for n, m in enumerate(tool_views):
                    data = json.loads(m["content"])
                    self.assertEqual(m["tool_call_id"], calls[n]["id"])
                    self.assertEqual(data["control_observation"]["next_action"], "read")
                    self.assertEqual(data["control_observation"]["counts"]["rows"], "1200")
                    self.assertEqual(data["tool_result_ref"]["full_sha256"], hashlib.sha256(body.encode()).hexdigest())

    async def test_reopened_legacy_checkpoint_preserves_history_and_exact_paged_evidence(self):
        content = '{"p":0.012345678901234567,"text":' + json.dumps("研究数据" * 4000, ensure_ascii=False) + '}'
        first = self.delivery("persisted-call", content)
        self.assertLessEqual(self.serialized(first.content), FIRST_DELIVERY_BYTES)
        self.assertTrue(tool_results.pin_references(self.sid, [first.tool_call_id]))
        expected_history = [HumanMessage(content="old question"), AIMessage(content="old answer"),
                            HumanMessage(content="new question")]
        self.assertTrue(await self.session_store.save_messages(self.sid, expected_history))
        _, ids = await self.session_store.get_messages_with_ids(self.sid)
        with sessions._connect_db(self.session_store.db_path) as conn:
            conn.execute("UPDATE sessions SET checkpoint_msg_id=?, checkpoint_summary=? WHERE session_id=?",
                         (ids[1], "UNVERIFIED legacy summary", self.sid))
            conn.commit()
        sessions.SessionManager._instance = None
        tool_results._schema_ready = False
        reopened = sessions.SessionManager()
        with patch.object(sessions, "session_manager", reopened):
            history, reopened_ids = await reopened.get_messages_with_ids(self.sid)
            view, compacted = await ContextManager(session_manager=reopened)._build_send_view(
                self.sid, history, reopened_ids, "new question")
            self.assertEqual(reopened_ids, ids)
            self.assertEqual([m.content for m in view], [m.content for m in expected_history])
            self.assertEqual(reopened.get_checkpoint(self.sid)["message_watermark"], ids[-1])
            self.assertFalse(compacted)
            selected = json.loads(read_tool_result.invoke({"tool_call_id": first.tool_call_id, "fields": ["/p"]}))
            self.assertIn("0.012345678901234567", selected["result_text"])
            parts, offset = [], 0
            while offset is not None:
                page = json.loads(read_tool_result.invoke({"tool_call_id": first.tool_call_id,
                                                          "start_char": offset, "max_chars": 4096}))
                self.assertEqual(page["status"], "ok")
                self.assertEqual(page["page"]["start_char"], offset)
                parts.append(page["result_text"])
                offset = page["page"]["next_start_char"]
            self.assertEqual("".join(parts), content)
            self.assertEqual(page["storage"]["full_sha256"], hashlib.sha256(content.encode()).hexdigest())
            tracing.set_user_id(8)
            self.assertEqual(json.loads(read_tool_result.invoke({"tool_call_id": first.tool_call_id}))["status"], "unavailable")
            tracing.set_user_id(7)
            tracing.set_session_id(await reopened.get_or_create_session(None, 7))
            self.assertEqual(json.loads(read_tool_result.invoke({"tool_call_id": first.tool_call_id}))["status"], "unavailable")

    async def test_workbook_snapshot_reuse_is_bound_to_source_version_and_scope(self):
        from io import BytesIO
        from openpyxl import Workbook
        from citrus_audit import inspection
        from src.research import tools

        def source(value):
            workbook = Workbook()
            workbook.active.append([value, "value"])
            stream = BytesIO()
            workbook.save(stream)
            data = stream.getvalue()
            ref = {"kind": "account_file", "file_id": "same-id", "sha256": hashlib.sha256(data).hexdigest()}
            return {"name": "same.xlsx", "source_ref": ref, "data": data}

        args = {"inspection": "layout"}
        with (patch.object(tools, "_audit_source_tables", return_value=[source("first")]) as inputs,
              patch.object(inspection, "inspect_workbook", wraps=inspection.inspect_workbook) as parser):
            first = tools.inspect_uploaded_table.invoke(args)
            snapshot = json.loads(first)["snapshot_ref"]
            self.assertTrue(tool_results.record(tool_call_id="workbook-origin", session_id=self.sid,
                request_id="previous-turn", tool_name="inspect_uploaded_table", status="complete", result_text=first))
            replay = json.loads(tools.inspect_uploaded_table.invoke(args))
            self.assertEqual(replay["reused_tool_call_id"], "workbook-origin")
            self.assertEqual(replay["snapshot_ref"], snapshot)
            self.assertEqual(parser.call_count, 1)

            inputs.return_value = [source("second")]
            changed = json.loads(tools.inspect_uploaded_table.invoke(args))
            self.assertNotEqual(changed["snapshot_ref"], snapshot)
            self.assertNotIn("reused_tool_call_id", changed)
            self.assertEqual(parser.call_count, 2)

        tracing.set_user_id(8)
        self.assertIsNone(tool_results.fetch_workbook_snapshot(snapshot["key"]))

    async def test_optional_readback_fields_preserve_available_values_and_null(self):
        original = '{"qa":{"status":"pass","nullable":null,"score":0.12345678901234567890}}'
        delivered = self.delivery("qa-fields", original)
        fields = ["/qa/status", "/qa/nullable", "/qa/score", "/error", "/qa/details"]
        rendered = read_tool_result.invoke({"tool_call_id": delivered.tool_call_id, "fields": fields})
        result = json.loads(rendered)
        self.assertEqual(result["storage"]["state"], "complete")
        self.assertEqual(result["missing_fields"], ["/error", "/qa/details"])
        self.assertEqual(result["_tool_outcome"]["status"], "complete")
        self.assertEqual(result["selected_values"]["/qa/status"], "pass")
        self.assertIsNone(result["selected_values"]["/qa/nullable"])
        self.assertIn("0.12345678901234567890", result["result_text"])
        self.assertIsNone(result["page"]["next_start_char"])
        self.assertIsNone(result["page"]["next_args"])

    async def test_readback_rejections_and_corruption_do_not_count_as_execution_failures(self):
        from src.tools.outcome import safe_tool_step

        raw = "字😀" * 12000
        result = self.delivery("large-unicode", raw)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        args = {"tool_call_id": result.tool_call_id}
        cases = [({**args, "max_chars": 20_001}, "result_read_range"),
                 ({**args, "max_chars": 20_000}, "result_page_too_large"),
                 ({**args, "start_char": len(raw) + 1}, "result_read_range"),
                 ({**args, "fields": ["/unknown"]}, "result_json_invalid"),
                 ({"tool_call_id": "missing"}, "result_unavailable")]
        for inputs, reason in cases:
            with self.subTest(reason=reason):
                content = read_tool_result.invoke(inputs)
                self.assertNotIn("result_text", json.loads(content))
                step = safe_tool_step("read_tool_result", content)
                self.assertEqual((step["status"], step["reason_code"], step["retryable"]),
                                 ("not_run", reason, False))
        denied = {**args}
        tracing.set_user_id(8)
        denial = json.loads(read_tool_result.invoke(denied))
        self.assertEqual(denial["reason"], "result_access_denied")
        self.assertNotIn("tool_result_ref", denial)
        tracing.set_user_id(7)
        small = json.loads(read_tool_result.invoke({**args, "start_char": 11, "max_chars": 32}))
        self.assertEqual(small["tool_result_ref"]["full_sha256"], digest)
        self.assertEqual(small["result_text"], raw[11:43])
        self.assertEqual(small["page"]["next_start_char"], 43)
        with tool_results._connect() as conn:
            conn.execute("UPDATE tool_results SET result_text=? WHERE tool_call_id=?",
                         ("X" + raw[1:], result.tool_call_id))
            conn.commit()
        broken = json.loads(read_tool_result.invoke(args))
        self.assertEqual(broken["reason"], "result_integrity_failed")
        self.assertNotIn("result_text", broken)
        with tool_results._connect() as conn:
            conn.execute("UPDATE tool_results SET result_text=?, truncated=1 WHERE tool_call_id=?",
                         (raw[:30], result.tool_call_id))
            conn.commit()
        clipped = json.loads(read_tool_result.invoke(args))
        self.assertEqual(clipped["storage"]["state"], "truncated")
        self.assertFalse(clipped["storage"]["complete"])
        self.assertEqual(clipped["_tool_outcome"]["status"], "partial")
        self.assertEqual(clipped["result_text"], raw[:30])
        fields = json.loads(read_tool_result.invoke({**args, "fields": ["/unknown"]}))
        self.assertEqual(fields["reason"], "result_retention_truncated")
        self.assertNotIn("selected_values", fields)

    async def test_exact_readback_retries_reuse_the_authenticated_same_turn_page(self):
        self.enterContext(patch.object(jobs, "_DB_PATH", Path(self.session_store.db_path)))
        self.addCleanup(tracing.set_job_id, tracing.get_job_id())
        self.addCleanup(progress_bus.clear_turn_scope)
        claim = jobs.claim_job(self.sid, "request", owner_user_id=7, client_request_id="request", payload={})
        tracing.set_job_id(claim.job_id)
        progress_bus.set_turn_scope(self.sid, "request")
        identity = ExecutionIdentity(7, self.sid, "request", claim.job_id)
        host = HostTools(identity, {}, allowed_names={"read_tool_result"})
        raw = json.dumps({"field": "éλ字" * 3000}, ensure_ascii=False)
        host.messages["source"] = self.delivery("source", raw)

        async def page(call_id, **selection):
            args = {"tool_call_id": "source", "max_chars": 50, **selection}
            self.assertTrue(host._prepare_result_readback(args, call_id))
            call = {"id": call_id, "name": "read_tool_result", "args": args}
            message = ToolMessage(content=read_tool_result.invoke(args), name=call["name"], tool_call_id=call_id)
            delivered = await host.publish(call, ToolExecution(message), identity)
            return json.loads(delivered.content), delivered.additional_kwargs["result_reference"]

        original, reference = await page("first", start_char=25)
        self.assertEqual(original["result_text"], raw[25:75])
        next_args = original["page"]["next_args"]
        cursor = original["page"]["next_cursor"]
        self.assertEqual(next_args, {"tool_call_id": "source", "cursor": cursor, "max_chars": 50})
        second, _ = await page("second", **next_args)
        self.assertEqual(second["result_text"], raw[75:125])
        repeated, _ = await page("repeat", start_char=25)
        self.assertEqual(repeated["status"], "reused")
        self.assertNotIn("result_text", repeated)
        self.assertEqual(repeated["reused_readback"], {
            "tool_call_id": reference["tool_call_id"], "full_sha256": reference["full_sha256"],
            "original_page_bytes": len(raw[25:75].encode())})
        self.assertEqual(repeated["page"], original["page"])
        retried_cursor, _ = await page("cursor-retry", cursor=cursor)
        self.assertEqual((retried_cursor["status"], retried_cursor["page"]), ("reused", second["page"]))
        self.assertFalse(host._prepare_result_readback({"tool_call_id": "source", "cursor": "forged"}, "forged"))

        selected, _ = await page("selected", fields=["field"])
        next_selected = selected["page"]["next_args"]
        self.assertFalse(host._prepare_result_readback({**next_selected, "fields": ["/other"]}, "wrong-fields"))
        self.assertFalse(host._prepare_result_readback({**next_selected, "start_char": 51}, "wrong-offset"))
        continued, _ = await page("selected-next", **next_selected)
        self.assertEqual(continued["selected_fields"], ["/field"])
        self.assertEqual(continued["page"]["start_char"], selected["page"]["end_char"])
        projected = json.dumps({"/field": "éλ字" * 3000}, ensure_ascii=False)
        self.assertEqual(selected["result_text"] + continued["result_text"], projected[:100])
        next_job = jobs.claim_job(self.sid, "next", owner_user_id=7, client_request_id="different", payload={})
        tracing.set_job_id(next_job.job_id)
        fresh = json.loads(read_tool_result.invoke({"tool_call_id": "source", "start_char": 25, "max_chars": 50}))
        self.assertEqual(fresh["result_text"], raw[25:75])
        tracing.set_user_id(8)
        forbidden = json.loads(read_tool_result.invoke({"tool_call_id": "source"}))
        self.assertEqual(forbidden["reason"], "result_access_denied")

    async def test_published_pages_survive_soft_budget_and_have_hard_limit_resume_range(self):
        from src.agent.host import _finish
        from src.agent.runtime import RunResult
        from src.config import settings
        from src.core.execution_ledger import ExecutionLedger

        self.enterContext(patch.object(jobs, "_DB_PATH", Path(self.session_store.db_path)))
        claim = jobs.claim_job(self.sid, "request", owner_user_id=7, client_request_id="request", payload={})
        identity = ExecutionIdentity(7, self.sid, "request", claim.job_id)
        progress_bus.set_turn_scope(self.sid, "request")
        progress_bus.set_request_queue(task_events.DurableEventQueue(claim.job_id))
        self.addCleanup(progress_bus.clear_turn_scope)
        self.addCleanup(progress_bus.clear_request_queue)
        self.addCleanup(tracing.set_job_id, tracing.get_job_id())
        tracing.set_job_id(claim.job_id)
        host = HostTools(identity, {}, allowed_names={"read_tool_result"})

        class FixtureArchive:
            provider_scope = ("https://api.deepseek.com/v1", "deepseek-fixture")

        random_data = random.Random(9)
        raw = "".join(random_data.choices(string.ascii_letters + string.digits, k=210_001))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        source = self.delivery("source", raw)
        messages = [HumanMessage(content="Read synthetic evidence")]
        for index in range(30):
            start = index * 7000
            args = {"tool_call_id": source.tool_call_id, "start_char": start, "max_chars": 7000}
            call = {"id": f"page-{index}", "name": "read_tool_result", "args": args}
            message = ToolMessage(content=read_tool_result.invoke(args), name=call["name"], tool_call_id=call["id"])
            page = await host.publish(call, ToolExecution(message), identity)
            data = json.loads(page.content)
            self.assertEqual(data["tool_result_ref"]["full_sha256"], digest)
            self.assertEqual(data["result_text"], raw[start:start + 7000])
            private = _ProtocolState(FixtureArchive.provider_scope, "synthetic private protocol")
            messages.extend((_DeepSeekMessage(content="", tool_calls=[call], provider_protocol=private), page))
        started = time.monotonic()
        view = tuple(messages)
        calls = []

        def unexpected_dispatch(request):
            calls.append(request)
            raise AssertionError("hard context cap must block transport")

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(FixtureArchive.provider_scope[0], httpx.MockTransport(unexpected_dispatch))) as transport:
            model = ChatOpenAI(use_responses_api=False, model="deepseek-fixture", api_key="fixture",
                base_url=FixtureArchive.provider_scope[0], max_retries=0, max_tokens=4096,
                http_async_client=transport)
            with patch.object(settings, "CONTEXT_ACTIVE_MAX_INPUT_TOKENS", 5000):
                with self.assertRaisesRegex(AdmissionStopped, "context_budget_exhausted:tool_observations") as halted:
                    await invoke_model_once(model, FixtureCatalog(), {"analyze"}, view, identity)
        self.assertEqual(calls, [])
        ledger = ExecutionLedger(session_id=self.sid, request_id="request")
        result = await _finish({"query": "Read synthetic evidence"}, identity, ledger, host,
            RunResult("incomplete", halted.exception.reason, tuple(messages), 30), 1, started)
        self.assertEqual(result["turn_outcome"]["reason_code"], "budget_blocked")
        self.assertEqual({record["status"] for record in result["tool_status"]}, {"complete"})
        with task_events.transaction() as conn:
            rows = conn.execute("SELECT data FROM task_events WHERE job_id=? AND event='context_degraded'",
                                (claim.job_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        event = json.loads(rows[0][0])
        self.assertEqual(event["status"], "budget_blocked")
        cursor = event["last_readback_page"]
        self.assertEqual((cursor["tool_call_id"], cursor["full_sha256"]), (source.tool_call_id, digest))
        self.assertEqual(cursor["next_start_char"], 210_000)
        smaller = json.loads(read_tool_result.invoke({"tool_call_id": source.tool_call_id, **cursor["retry"]}))
        self.assertEqual(smaller["result_text"], raw[203_000:206_500])
        self.assertEqual(smaller["tool_result_ref"]["full_sha256"], digest)

    async def test_operation_receipt_blocks_same_call_after_publication_and_reopen(self):
        from types import SimpleNamespace
        import numpy as np
        from src.agent.batch_journal import require_unclaimed_call

        self.enterContext(patch.object(jobs, "_DB_PATH", Path(self.session_store.db_path)))
        claim = jobs.claim_job(self.sid, "request", owner_user_id=7, client_request_id="request", payload={})
        self.addCleanup(tracing.set_job_id, tracing.get_job_id())
        tracing.set_job_id(claim.job_id)
        self.addCleanup(progress_bus.clear_turn_scope)
        progress_bus.set_turn_scope(self.sid, "request")
        identity = ExecutionIdentity(7, self.sid, "request", claim.job_id)
        host = HostTools(identity, {}, allowed_names={"fixture_write"})
        call = {"name": "fixture_write", "args": {}, "id": "fixture"}
        dispatched = []

        async def dispatch(tool, call, *, on_dispatch, **kwargs):
            on_dispatch()
            dispatched.append(call["id"])
            return ToolMessage(content="fixture output", name=tool.name, tool_call_id=call["id"],
                artifact={"value": np.array([1.0, np.nan]), "_tool_outcome": {"status": "complete"}})

        with patch("src.tools.registry._execute_single_tool", dispatch):
            require_unclaimed_call(identity, call)
            observed = await host.publish(call, await host.invoke(SimpleNamespace(name=call["name"]), call, identity), identity)
        self.assertEqual(dispatched, ["fixture"])
        self.assertEqual(observed.artifact["value"], [1.0, None])
        with task_events.transaction() as conn:
            row = conn.execute("SELECT status,host_observation FROM task_operations WHERE tool_call_id='fixture'").fetchone()
            self.assertEqual(row["status"], "complete")
            self.assertTrue(row["host_observation"])
        restored = HostTools(identity, {}, allowed_names={"fixture_write"})
        restored.restore_observation(observed)
        self.assertEqual(host.messages, restored.messages)
        with self.assertRaisesRegex(AdmissionStopped, "recovery_requires_reconciliation"):
            require_unclaimed_call(identity, call)
        with task_events.transaction() as conn:
            conn.execute("UPDATE task_operations SET status='unknown',host_observation='' WHERE tool_call_id='fixture'")
        with self.assertRaisesRegex(AdmissionStopped, "recovery_requires_reconciliation"):
            require_unclaimed_call(identity, call)
        self.assertEqual(dispatched, ["fixture"])

    async def test_sdk_wire_pair_preserves_private_state_and_meters_fixture_usage(self):
        self.enterContext(patch.object(jobs, "_DB_PATH", Path(self.session_store.db_path)))
        claim = jobs.claim_job(self.sid, "request", owner_user_id=7, client_request_id="request", payload={})
        self.addCleanup(tracing.set_job_id, tracing.get_job_id())
        tracing.set_job_id(claim.job_id)
        task_events.init_budget(claim.job_id, time.time() + 12, max_calls=4, max_tokens=1_000_000)
        identity = ExecutionIdentity(7, self.sid, "request", claim.job_id)
        base, model_name = "https://api.deepseek.com/v1", "deepseek-fixture"
        wire_requests = []

        def respond(request):
            wire_requests.append(json.loads(request.content))
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Synthetic result"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 109, "completion_tokens": 7, "total_tokens": 116}},
            ]
            stream = "".join("data: " + json.dumps({"id": "fixture-reply", "object": "chat.completion.chunk",
                "model": model_name, "created": 1, **chunk}) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(base, httpx.MockTransport(respond)), trust_env=False) as transport:
            model = ChatOpenAI(use_responses_api=False, model=model_name, api_key="fixture", base_url=base,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=transport)
            call = {"name": "analyze", "args": {}, "id": "native-call"}
            raw = oversized_controls()
            observation = ToolMessage(content=raw, tool_call_id=call["id"], name=call["name"],
                                      artifact={"op_receipt": {"status": "complete"}})
            self.addCleanup(progress_bus.clear_turn_scope)
            progress_bus.set_turn_scope(self.sid, "request")
            host = HostTools(identity, {}, allowed_names={"analyze", "read_tool_result"})
            content = await host.publish(call, ToolExecution(observation), identity)
            self.assertEqual(content.additional_kwargs["host_publication"]["tool_call_id"], call["id"])
            self.assertEqual(tool_results.fetch_for_current_request(call["id"])["result_text"], raw)
            self.assertEqual(json.loads(content.content)["tool_result_ref"]["full_sha256"],
                             hashlib.sha256(raw.encode()).hexdigest())
            native = _DeepSeekMessage(content="", tool_calls=[call],
                                      provider_protocol=_ProtocolState((base, model_name), "synthetic private protocol"))
            messages = (HumanMessage(content="Read the attached data"), native, content)
            response = await invoke_model_once(model, FixtureCatalog(), {"analyze"}, messages, identity)
            self.assertEqual(response.usage_metadata["total_tokens"], 116)
            self.assertEqual(len(wire_requests), 1)
            wire = wire_requests[0]
            self.assertEqual([item["role"] for item in wire["messages"]], ["user", "assistant", "tool"])
            self.assertEqual(wire["messages"][1]["reasoning_content"], "synthetic private protocol")
            self.assertEqual(wire["messages"][1]["tool_calls"][0]["id"], wire["messages"][2]["tool_call_id"])
            self.assertTrue(wire["tools"][0]["function"]["strict"])
            self.assertLessEqual(self.serialized(wire["messages"][2]["content"]), FIRST_DELIVERY_BYTES)
            oversized = messages + (HumanMessage(content="x" * 700_000),)
            with self.assertRaisesRegex(task_events.TaskConflict, "context_budget_exhausted"):
                await invoke_model_once(model, FixtureCatalog(), {"analyze"}, oversized, identity)
            self.assertEqual(len(wire_requests), 1)
            budget = task_events.projection(claim.job_id)["budget"]
            self.assertEqual((budget["calls"], budget["tokens"]), (1, 116))


class TaskDeliveryContractTests(unittest.TestCase):
    def test_successful_inspect_does_not_fulfill_requested_files_or_count(self):
        from src.core.turn_outcome import artifact_goal_evidence
        from src.graph.task_contract import contract_for_explicit_outputs

        requirements = contract_for_explicit_outputs("请生成两张科研图，导出 PDF。").deliverables
        self.assertEqual(len(requirements), 2)
        asset = {"valid": True, "request_matches": True, "filename": "plot.svg",
                 "producer_tool": "figure_workspace", "artifact_id": "figure-one", "link": "/plot"}
        records = [{"status": "complete", "tool_call_id": "inspect"},
                   {"status": "complete", "tool_call_id": "produce", "artifact_validations": [asset]}]
        goals, gaps = artifact_goal_evidence(requirements, records, {"/plot"})
        self.assertEqual(goals, [])
        self.assertEqual(len(gaps), 2)
        asset["filename"] = "plot.pdf"
        goals, gaps = artifact_goal_evidence(requirements, records, {"/plot"})
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0].evidence_call_ids, ["produce"])
        self.assertEqual(len(gaps), 1)
        asset["request_matches"] = False
        self.assertEqual(artifact_goal_evidence(requirements, records, {"/plot"})[0], [])

    def test_artifact_contract_tampering_is_rejected(self):
        from src.core.turn_outcome import (CompletionEnvelope, CompletionGoal, FinalReview,
            reviewed_completion_valid, reviewed_turn_outcome)
        from src.graph.task_contract import contract_for_explicit_outputs

        requirements = contract_for_explicit_outputs("请生成一张科研图并导出 PDF。").deliverables
        asset = {"valid": True, "request_matches": True, "filename": "plot.pdf",
                 "producer_tool": "figure_workspace", "artifact_id": "figure-one", "link": "/plot"}
        records = [{"status": "complete", "tool_call_id": "produce",
                    "artifact_validations": [asset]}]
        goal = CompletionGoal(goal=str(requirements[0]["label"]), kind="deliverable",
                              evidence_call_ids=["produce"], artifact_links=["/plot"])
        statement = CompletionEnvelope(schema_version=1, answer="done /plot", status="completed",
            goals=[goal], remaining=[], incidental_partials=[], proposal_repairs=[],
            review=FinalReview(**{key: "fixture" for key in
                ("facts", "sources", "numbers", "conditions", "delivery")}))
        outcome = reviewed_turn_outcome(statement, records,
            identity={"owner_user_id": 7, "session_id": "s", "job_id": "j"},
            request_version=0, request_hash="h",
            artifact_contract={"schema_version": 1, "request_version": 0,
                "requirements": requirements, "verified_links": ["/plot"]})
        self.assertTrue(reviewed_completion_valid(outcome, records, answer="done /plot"))
        outcome.completion["artifact_contract"]["verified_links"] = []
        self.assertFalse(reviewed_completion_valid(outcome, records, answer="done /plot"))

    def test_repair_only_matches_the_original_bind_target(self):
        from src.agent.host import _same_repair_target

        original = {"name": "code_bind_input", "args": {"workspace_id": "ws", "task_id": "task",
                    "file_id": "file", "sha256": "digest", "path": "/input/table.csv", "expected_revision": 3}}
        correction = {**original, "args": {**original["args"], "expected_revision": 4}}
        self.assertTrue(_same_repair_target(original, correction))
        self.assertFalse(_same_repair_target(original, {**correction, "args": {**correction["args"], "file_id": "another"}}))
        self.assertFalse(_same_repair_target(original, {"name": "read", "args": correction["args"]}))
        rooted = {**correction, "args": {**correction["args"], "path": "/input/table.csv"}}
        self.assertTrue(_same_repair_target(
            {**original, "args": {**original["args"], "path": "table.csv"}}, rooted))

    def test_relative_write_repair_matches_the_same_workspace_file(self):
        from src.agent.host import _same_repair_target

        original = {"name": "code_write", "args": {"workspace_id": "ws", "task_id": "task",
                    "path": "figure.py", "expected_revision": 2, "content": "x"}}
        correction = {"name": "code_write", "args": {"workspace_id": "ws", "task_id": "task",
                      "path": "/workspace/figure.py", "expected_revision": 2, "content": "x"}}
        self.assertTrue(_same_repair_target(original, correction))
        self.assertFalse(_same_repair_target(original, {"name": "code_write",
            "args": {**correction["args"], "workspace_id": "other"}}))

    def test_relative_code_paths_are_rooted_in_their_namespace(self):
        from src.agent.code_tools import BindInputArgs, ExecuteArgs, WriteArgs

        write = WriteArgs(task_id="task", workspace_id="0" * 32, expected_revision=1,
                          path="figure.py", content="print(1)")
        self.assertEqual(write.path, "/workspace/figure.py")
        bind = BindInputArgs(task_id="task", workspace_id="0" * 32, expected_revision=1,
                             path="./table.csv", file_id="1" * 32, sha256="a" * 64)
        self.assertEqual(bind.path, "/input/table.csv")
        execute = ExecuteArgs(task_id="task", workspace_id="0" * 32, expected_revision=1,
                              script_path="run.py", inputs=["table.csv"], outputs=["plot.png"])
        self.assertEqual(execute.script_path, "/workspace/run.py")
        self.assertEqual(execute.inputs, ["/input/table.csv"])
        self.assertEqual(execute.outputs, ["/outputs/plot.png"])
        with self.assertRaises(ValueError):
            WriteArgs(task_id="task", workspace_id="0" * 32, expected_revision=1,
                      path="../escape.py", content="x")



def test_script_receipt_projects_bounded_counts_instead_of_preview_inference():
    from src.core.tool_result_view import _script_controls

    content = {"script_report": '{"rows_read":12,"rows_plotted":12,"rows_excluded":0,"n_per_group":{"A":6,"B":6},"arbitrary":"not projected"}'}
    assert _script_controls(content)["render_data_summary"] == {
        "rows_read": 12, "rows_plotted": 12, "rows_excluded": 0, "n_per_group": {"A": 6, "B": 6},
    }
    assert "render_data_summary" not in _script_controls({"script_report": '{"rows_read":true,"n_per_group":{"bad":-1}}'})
