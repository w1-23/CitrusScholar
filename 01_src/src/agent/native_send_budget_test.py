"""Pinned graph, converted streamed provider requests and durable older readback."""
from importlib.metadata import version
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from typing_extensions import NotRequired

from .capabilities import CapabilityCatalog, CapabilityDefinition
from .checkpoint_store import framework_checkpoint_scope, open_framework_checkpoint
from .execution import ToolExecution
from .deep_agent_runtime import EXECUTOR_ID
from .harness import HarnessRuntime, HarnessState
from .host_tools import HostTools
from .identity import ExecutionIdentity
from .model import ModelDriver
from .recovery import _checkpoint_reader
from .runtime import FinalCheck, RunLimits
from src.config import settings
from src.core import jobs, progress_bus, request_context, task_events, tool_results, tracing
from src.core.provider_transport import AsyncDeepSeekTransport
from langchain_openai import ChatOpenAI
from src.research.result_tools import read_tool_result
from src.session import manager as sessions


READER = "read_tool_result"
EVIDENCE = "read_context_evidence"
TABLE = "inspect_uploaded_table"
PROVIDER = "deepseek-fixture"
BASE = "https://api.deepseek.com/v1"
OLD_TEXT = {f"old-{i}": f"evidence {i} " + "Z" * 30_300 for i in range(16)}


def _chat_stream(calls=(), *, answer="accepted", finish=None, raw_args=None, completion_tokens=10):
    if calls:
        delta = {"role": "assistant", "reasoning_content": "private tool reasoning",
                 "tool_calls": [{"index": i, "id": cid, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args) if raw_args is None else raw_args}}
                     for i, (cid, name, args) in enumerate(calls)]}
    else:
        delta = {"role": "assistant", "content": answer}
    chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": PROVIDER, "created": 1}
    rows = [{**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**chunk, "choices": [{"index": 0, "delta": {},
                                  "finish_reason": finish or ("tool_calls" if calls else "stop")}]},
            {**chunk, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens,
                                                "total_tokens": 100 + completion_tokens}}]
    return httpx.Response(200, text="".join(f"data: {json.dumps(row)}\n\n" for row in rows) + "data: [DONE]\n\n",
                          headers={"Content-Type": "text/event-stream"})


class NativeSendBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(sessions, "STATE_DIR", self.root))
        self.enterContext(patch.object(sessions, "DB_PATH", self.root / "history.sqlite3"))
        self.enterContext(patch.object(sessions.SessionManager, "_instance", None))
        self.enterContext(patch.object(tool_results, "_schema_ready", False))
        store = sessions.SessionManager()
        self.enterContext(patch.object(sessions, "session_manager", store))
        self.enterContext(patch.object(jobs, "_DB_PATH", Path(store.db_path)))
        self.sid = await store.get_or_create_session(None, 7)
        tracing.set_user_id(7)
        tracing.set_session_id(self.sid)
        self.enterContext(patch.object(settings, "CONTEXT_ACTIVE_MAX_INPUT_TOKENS", 128_000))
        self.provider = (BASE, PROVIDER)
        self.catalog = CapabilityCatalog()
        self.catalog.register(CapabilityDefinition(EVIDENCE, "Synthetic archival evidence",
            {"type": "object", "properties": {}}, lambda: None))
        self.catalog.register(CapabilityDefinition(TABLE, "Synthetic long table",
            {"type": "object", "properties": {}}, lambda: None))
        self.catalog.register(CapabilityDefinition(READER, read_tool_result.description,
            read_tool_result.get_input_schema().model_json_schema(), lambda: read_tool_result))

    def job(self, index):
        claim = jobs.claim_job(self.sid, f"server-{index}", owner_user_id=7,
                               client_request_id=f"client-{index}", payload={"query": index})
        self.assertTrue(claim.created)
        identity = ExecutionIdentity(7, self.sid, claim.job["request_id"], claim.job_id)
        tracing.set_job_id(identity.run_id)
        progress_bus.set_turn_scope(self.sid, claim.job["client_request_id"])
        return identity

    async def run_turn(self, model, identity, human_content, evidence, *, model_budget=None, edit_history=True, **kwargs):
        host = HostTools(identity, {}, allowed_names={EVIDENCE, TABLE, READER})
        driver = ModelDriver(self.catalog, max_output_tokens=16_384, budget=model_budget)
        human = HumanMessage(content=human_content, additional_kwargs=identity.message_metadata("user"))

        async def metered(request, handler, active, host_tools):
            allowed = {EVIDENCE, TABLE}
            if host.has_result_readback():
                allowed.add(READER)
            return await driver(request, handler, active, allowed=allowed, host_tools=host_tools,
                                historical_readback=host, edit_history=edit_history)

        async def execute(call, active):
            if call["name"] == READER:
                args = dict(call["args"])
                self.assertTrue(host._prepare_result_readback(args, call["id"]))
                content = read_tool_result.invoke(args)
                row = tool_results.fetch_for_current_request(args["tool_call_id"], strict=True)
                decoded = json.loads(content)
                page = decoded["page"]
                self.assertEqual(decoded["result_text"], evidence[row["provider_call_id"]][page["start_char"]:page["end_char"]])
            else:
                content = evidence[call["id"]]
            message = ToolMessage(content=content, name=call["name"], tool_call_id=call["id"],
                                  artifact={"_tool_outcome": {"status": "complete"}})
            return await host.publish(call, ToolExecution(message), active), {}

        async def check_final(*_):
            return FinalCheck()

        runtime = HarnessRuntime(model, execute, lambda *_: None, check_final, RunLimits(8, float("inf")),
            self.catalog.schemas({EVIDENCE, TABLE, READER}), model_call=metered, **kwargs)
        async with open_framework_checkpoint(self.root, identity, self.provider) as saver:
            result = await runtime.run(identity, [human], saver=saver,
                checkpoint_config=framework_checkpoint_scope(identity, self.provider),
                agent_options={"system_prompt": "fixture system prompt"})
        return result, host

    async def test_same_thread_second_fresh_job_edits_only_proven_old_wire_observations(self):
        self.assertEqual((version("deepagents"), version("langchain")), ("0.7.38", "1.5.0"))
        self.assertEqual(task_events.model_input_limit(4096), 128_000)
        wire, admissions = [], []
        first = self.job(1)
        advertised = {}
        unrelated = tool_results.record_with_reference(tool_call_id="unoffered", session_id=self.sid,
                         request_id="client-1", tool_name=EVIDENCE, result_text="not in the checkpoint")
        self.assertIsNotNone(unrelated)

        def transport(request):
            payload = json.loads(request.content)
            wire.append(payload)
            if len(wire) == 1:
                return _chat_stream([(cid, EVIDENCE, {}) for cid in OLD_TEXT])
            if len(wire) == 3:
                for message in payload["messages"]:
                    content = message.get("content") or ""
                    if message["role"] == "tool" and content.startswith('{"delivery":"older_result_omitted'):
                        advertised[message["tool_call_id"]] = json.loads(content)
                self.assertTrue(advertised, "oversized second request carried no verified old receipt")
                receipt = next(iter(advertised.values()))
                return _chat_stream([("current", EVIDENCE, {}), ("read-old", READER,
                    {"tool_call_id": receipt["read_with"]["tool_call_id"], "max_chars": 24})])
            return _chat_stream()

        real_attempt = task_events.run_model_attempt

        async def measured(model, messages, invoke, **kwargs):
            payload, estimate = request_context.model_request(model, messages)
            admissions.append((payload, estimate))
            return await real_attempt(model, messages, invoke, **kwargs)

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            with patch.object(task_events, "run_model_attempt", measured):
                result1, host1 = await self.run_turn(model, first, "first", OLD_TEXT)
                self.assertEqual(result1.status, "complete", result1.reason)
                self.assertEqual(len(wire), 2)
                self.assertTrue(jobs.update_job(first.run_id, status="completed"))
                second = self.job(2)
                self.assertNotEqual(first.run_id, second.run_id)
                self.assertNotEqual(first.user_turn_id, second.user_turn_id)
                self.assertEqual(framework_checkpoint_scope(first, self.provider),
                                 framework_checkpoint_scope(second, self.provider))
                self.assertIsNotNone(host1.messages["old-0"].additional_kwargs.get("host_publication"))
                result2, host2 = await self.run_turn(model, second, "q" * 31_000,
                    {**OLD_TEXT, "current": "current bytes"})
                self.assertEqual(result2.status, "complete", result2.reason)
                self.assertEqual(len(wire), 4)
                self.assertFalse(host2._prepare_result_readback({"tool_call_id": "unoffered"}, "forgery"))
                self.assertFalse(host2._prepare_result_readback({"tool_call_id": "old-0"}, "provider-alias"))
                prior = next(m for m in result1.messages if isinstance(m, ToolMessage) and m.tool_call_id == "old-0")
                for changed in ({"operation_uncertain": True}, {"context_trusted": False},
                                {"result_reference": {**prior.additional_kwargs["result_reference"], "full_sha256": "0" * 64}}):
                    self.assertIsNone(host2.historical_result_reference(prior.model_copy(update={
                        "additional_kwargs": {**prior.additional_kwargs, **changed}})))
                self.assertTrue(jobs.update_job(second.run_id, status="completed"))
                third = self.job(3)
                blocked, host3 = await self.run_turn(model, third, "q" * 520_000, {})
                self.assertEqual((blocked.status, blocked.reason), ("incomplete", "context_budget_exhausted"))
                self.assertEqual(len(wire), 4)
                self.assertEqual(host3.historical_result_offers, set())

        self.assertEqual(len(admissions), 5)
        for payload, (preflight, estimate) in zip(wire, admissions):
            self.assertEqual(payload["messages"], preflight["messages"])
            self.assertEqual(payload["tools"], preflight["tools"])
            self.assertEqual(request_context.estimate_request(payload), estimate)
            self.assertEqual(estimate["output_tokens"], 4096)
            self.assertLessEqual(estimate["estimated_input_tokens"], 128_000)
        self.assertGreater(admissions[-1][1]["estimated_input_tokens"], 128_000)
        tool_before = {msg["tool_call_id"]: msg for msg in wire[1]["messages"] if msg["role"] == "tool"}
        tool_after = {msg["tool_call_id"]: msg for msg in wire[2]["messages"] if msg["role"] == "tool"}
        self.assertEqual(tool_before.keys(), tool_after.keys())
        self.assertLess(len(advertised), len(OLD_TEXT))
        for cid, original in OLD_TEXT.items():
            self.assertEqual(tool_before[cid]["content"], original)
            if cid not in advertised:
                self.assertEqual(tool_after[cid]["content"], original)
                continue
            receipt = advertised[cid]
            key = tool_results.execution_receipt_key(first, cid)
            self.assertEqual(receipt["read_with"], {"tool": READER, "tool_call_id": key})
            self.assertEqual(receipt["tool_result_ref"]["full_sha256"], hashlib.sha256(original.encode()).hexdigest())
            self.assertEqual(tool_results.fetch_for_current_request(key)["result_text"], original)
        strict_old_wire = {**wire[2], "messages": [tool_before.get(m.get("tool_call_id"), m) for m in wire[2]["messages"]]}
        self.assertGreater(request_context.estimate_request(strict_old_wire)["estimated_input_tokens"], 128_000)
        names = [{s["function"]["name"] for s in payload["tools"]} for payload in wire]
        self.assertNotIn(READER, names[0])
        self.assertNotIn(READER, names[1])
        self.assertIn(READER, names[2])
        self.assertTrue(all(payload["model"] == PROVIDER and payload["stream"] for payload in wire))
        self.assertTrue(all(not (row & {"write_file", "edit_file", "delete", "execute", "task"}) for row in names))
        current_pair = [m for m in wire[3]["messages"] if m.get("tool_call_id") == "current"]
        self.assertEqual([m["content"] for m in current_pair], ["current bytes"])
        current_assistant = [m for m in wire[3]["messages"] if any(c["id"] == "current" for c in m.get("tool_calls", []))]
        self.assertEqual([m["reasoning_content"] for m in current_assistant], ["private tool reasoning"])
        async with open_framework_checkpoint(self.root, third, self.provider, read_only=True) as saver:
            checkpoint = await _checkpoint_reader(saver).aget_state(
                {"configurable": framework_checkpoint_scope(third, self.provider)})
        canonical = checkpoint.values["messages"]
        self.assertEqual({m.tool_call_id: m.content for m in canonical if isinstance(m, ToolMessage)
                          and m.tool_call_id in OLD_TEXT}, OLD_TEXT)
        self.assertNotIn("working_context", checkpoint.values)

    async def test_continuation_reoffers_verified_table_metadata_without_compacting(self):
        first = self.job(1)
        raw = json.dumps({"rows": 7, "columns": 115, "fields": [
            {"name": f"field_{i}", "min": "-1.0000000000001", "samples": ["汉字" * 60]}
            for i in range(115)], "raw_preview": "original table" * 400})
        evidence = {"old-table": raw}
        wire = []
        key = tool_results.execution_receipt_key(first, "old-table")

        def transport(request):
            payload = json.loads(request.content)
            wire.append(payload)
            if len(wire) == 1:
                return _chat_stream([("old-table", TABLE, {})])
            if len(wire) == 3:
                old = next(message for message in payload["messages"] if message.get("tool_call_id") == "old-table")
                table = json.loads(old["content"])
                self.assertEqual(table["delivery"], "stored_table_overview")
                self.assertEqual(table["table_overview"]["columns"], 115)
                self.assertEqual(table["tool_result_ref"]["full_sha256"], hashlib.sha256(raw.encode()).hexdigest())
                self.assertEqual(table["tool_result_ref"]["tool_call_id"], key)
                self.assertIn(READER, {item["function"]["name"] for item in payload["tools"]})
                self.assertLess(request_context.estimate_request(payload)["estimated_input_tokens"], 128_000)
                return _chat_stream([("read-old-table", READER, {"tool_call_id": key, "max_chars": 24})])
            return _chat_stream()

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            result1, _ = await self.run_turn(model, first, "inspect", evidence)
            self.assertEqual(result1.status, "complete", result1.reason)
            self.assertTrue(jobs.update_job(first.run_id, status="completed"))
            store = sessions.session_manager
            self.assertTrue(await store.save_messages(self.sid, result1.messages))
            restored, row_ids = await store.get_messages_with_ids(self.sid)
            from src.core.context_manager import ContextManager, assemble_supervisor_messages
            view, _ = await ContextManager()._build_send_view(self.sid, restored, row_ids, "follow-up", checkpoint={})
            messages, _ = assemble_supervisor_messages("system", {"messages": view}, HumanMessage("follow-up"))
            legacy = next(message for message in messages if isinstance(message, ToolMessage))
            self.assertNotIn("host_publication", legacy.additional_kwargs)
            self.assertNotIn("context_projection", legacy.additional_kwargs)
            self.assertEqual(legacy.content, next(message for message in result1.messages
                if isinstance(message, ToolMessage)).content)
            second = self.job(2)
            result2, host = await self.run_turn(model, second, "read prior table", evidence, edit_history=False)
            self.assertEqual(result2.status, "complete", result2.reason)
            self.assertEqual(host.historical_result_offers, {key})
            self.assertEqual(len(wire), 4)
            self.assertEqual(host.historical_result_reference(legacy, existing_delivery=True)["tool_call_id"], key)
            changed = json.loads(legacy.content)
            changed["tool_result_ref"]["full_sha256"] = "0" * 64
            self.assertIsNone(host.historical_result_reference(legacy.model_copy(update={
                "content": json.dumps(changed)}), existing_delivery=True))
            tracing.set_user_id(9)
            try:
                self.assertIsNone(host.historical_result_reference(legacy, existing_delivery=True))
            finally:
                tracing.set_user_id(7)

    async def test_duplicate_older_readback_changes_only_final_wire_copy(self):
        raw = json.dumps({"rows": 7, "columns": 115, "fields": [{"name": f"col_{i}"} for i in range(115)],
                          "raw_preview": "原始表格带有非 ASCII 数据" * 650}, ensure_ascii=False)
        evidence = {"table": raw}
        first = self.job(1)
        target = tool_results.execution_receipt_key(first, "table")
        args = {"tool_call_id": target, "start_char": 1200, "max_chars": 5000}
        wire = []

        def transport(request):
            wire.append((request.content, json.loads(request.content)))
            return {
                1: lambda: _chat_stream([("table", TABLE, {})]),
                2: lambda: _chat_stream([("old-page", READER, args)]),
                4: lambda: _chat_stream([("current-page", READER, args)]),
            }.get(len(wire), _chat_stream)()

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            first_result, _ = await self.run_turn(model, first, "inspect and read", evidence)
            self.assertEqual((first_result.status, len(wire)), ("complete", 3))
            self.assertTrue(jobs.update_job(first.run_id, status="completed"))
            second = self.job(2)
            second_result, host = await self.run_turn(model, second, "check exact range again", evidence)
            self.assertEqual((second_result.status, len(wire)), ("complete", 5))
            self.assertEqual(host.historical_result_offers, {target})
            self.assertEqual([record["tool_call_id"] for record in host.records], ["current-page"])

        def by_id(payload):
            return {item["tool_call_id"]: item for item in payload["messages"] if item["role"] == "tool"}

        old = by_id(wire[2][1])["old-page"]
        self.assertEqual(by_id(wire[3][1])["old-page"], old)
        final = by_id(wire[4][1])
        current_page = final["current-page"]
        self.assertEqual(current_page["content"], old["content"])
        pointer = json.loads(final["old-page"]["content"])
        self.assertEqual(pointer["delivery"], "older_verified_readback_page_omitted")
        self.assertEqual(pointer["duplicate_of_tool_call_id"], "current-page")
        self.assertEqual(pointer["tool_result_ref"], {"tool_call_id": target,
            "full_sha256": hashlib.sha256(raw.encode()).hexdigest()})
        self.assertEqual(set(final), {"table", "old-page", "current-page"})
        self.assertNotIn("result_text", pointer)
        self.assertGreater(len(old["content"].encode()) - len(final["old-page"]["content"].encode()), 5000)
        self.assertEqual(tool_results.fetch_for_current_request(target, strict=True)["result_text"], raw)
        for identity, cid in ((first, "old-page"), (second, "current-page")):
            saved = tool_results.fetch_for_current_request(tool_results.execution_receipt_key(identity, cid), strict=True)
            self.assertEqual(saved["result_text"], old["content"])
            self.assertTrue(tool_results.storage_integrity(saved)["complete"])
        async with open_framework_checkpoint(self.root, second, self.provider, read_only=True) as saver:
            checkpoint = await _checkpoint_reader(saver).aget_state(
                {"configurable": framework_checkpoint_scope(second, self.provider)})
        saved_pages = {msg.tool_call_id: msg for msg in checkpoint.values["messages"]
                       if isinstance(msg, ToolMessage) and msg.name == READER}
        self.assertEqual(set(saved_pages), {"old-page", "current-page"})
        self.assertTrue(all(msg.content == old["content"] for msg in saved_pages.values()))
        self.assertTrue(all(not msg.response_metadata.get("context_editing") for msg in saved_pages.values()))

    async def test_official_stream_produces_one_charged_attempt_with_packet_and_usage(self):
        identity = self.job(1)
        task_events.init_budget(identity.run_id, time.time() + 30, max_calls=1, max_tokens=60_000)
        sent = []

        def transport(request):
            sent.append(json.loads(request.content))
            return _chat_stream()

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            result, _ = await self.run_turn(model, identity, "once", {})
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(sent), 1)
        with task_events.transaction() as conn:
            rows = conn.execute("SELECT * FROM task_model_attempts WHERE job_id=?", (identity.run_id,)).fetchall()
            budget = conn.execute("SELECT calls,tokens FROM task_budgets WHERE job_id=?", (identity.run_id,)).fetchone()
        self.assertEqual(tuple(budget), (1, 110))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "complete")
        self.assertEqual(json.loads(rows[0]["usage"])["total_tokens"], 110)
        observation = json.loads(rows[0]["observation"])
        self.assertIsInstance(observation["provider_first_packet_ms"], int)
        self.assertEqual(observation["stream_state"], "terminal_observed")
        self.assertEqual(observation["max_output_tokens"], 4096)

    async def test_oversized_current_tool_observations_are_not_edited(self):
        identity = self.job(1)
        evidence = {f"current-{i}": f"current evidence {i} " + "R" * 30_300 for i in range(18)}
        wire = []

        def transport(request):
            wire.append(json.loads(request.content))
            self.assertEqual(len(wire), 1, "current observations must fail before another provider send")
            return _chat_stream([(cid, EVIDENCE, {}) for cid in evidence])

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            result, host = await self.run_turn(model, identity, "current results", evidence)
        self.assertEqual((result.status, result.reason), ("incomplete", "context_budget_exhausted:tool_observations"))
        self.assertEqual(len(wire), 1)
        self.assertFalse(host.historical_result_offers)
        self.assertEqual({m.tool_call_id: m.content for m in result.messages if isinstance(m, ToolMessage)}, evidence)

    async def test_length_never_raises_the_configured_output_cap(self):
        identity = self.job(1)
        wire = []

        def transport(request):
            wire.append(json.loads(request.content))
            self.assertEqual(len(wire), 1, "a length stop must not regenerate with extra output tokens")
            return _chat_stream([("truncated", EVIDENCE, {})], finish="length")

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            result, host = await self.run_turn(model, identity, "output", {}, recover_output=True)
        self.assertEqual((result.status, result.reason), ("incomplete", "model_length"))
        self.assertEqual([request_context.estimate_request(payload)["output_tokens"] for payload in wire], [4096])
        self.assertFalse(host.records)
        self.assertFalse(result.pending_tool_calls)

    async def test_malformed_tool_stream_length_requires_measured_cap_usage(self):
        self.addCleanup(progress_bus.clear_request_queue)
        for index, (finish, used, expected) in enumerate((
            ('length', 6144, 'exhausted'), ('length', 21, 'unconfirmed'), ('content_filter', 6144, None)), 1):
            identity = self.job(index)
            task_events.init_budget(identity.run_id, time.time() + 60, max_calls=1, max_tokens=30_000)
            progress_bus.set_request_queue(task_events.DurableEventQueue(identity.run_id))
            wire = []
            base_url = BASE
            registry = {'opencode_go': {PROVIDER: {'max_output_tokens': 6144, 'context_window_tokens': 20000}}}
            transport = SimpleNamespace(provider_id='opencode_go', model=PROVIDER, configured_context_window=30000)
            with patch.object(settings, 'MAIN_MODEL_BUDGETS', registry):
                budget = request_context.resolve_main_model_budget(transport, settings)
            def receive(request):
                wire.append(json.loads(request.content))
                return _chat_stream([('truncated', EVIDENCE, {})], finish=finish, raw_args='{',
                                    completion_tokens=used)
            with (patch.object(type(settings), 'RESOLVED_PROVIDER_ID', new_callable=PropertyMock, return_value='opencode_go'),
                  patch.object(type(settings), 'RESOLVED_MAIN_BASE_URL', new_callable=PropertyMock, return_value=base_url)):
                client_transport = AsyncDeepSeekTransport(base_url, httpx.MockTransport(receive))
                async with httpx.AsyncClient(transport=client_transport, trust_env=False) as client:
                    model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key='fixture', base_url=base_url,
                        max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
                    result, host = await self.run_turn(model, identity, 'fixture', {}, model_budget=budget, recover_output=True)
            self.assertEqual((result.status, result.reason), ('incomplete', 'model_' + finish))
            self.assertFalse(host.records)
            self.assertFalse(result.pending_tool_calls)
            self.assertEqual(len(wire), 1)
            self.assertEqual(wire[0]['max_tokens'] if 'max_tokens' in wire[0] else wire[0]['max_completion_tokens'], 6144)
            with task_events.transaction() as conn:
                row = conn.execute('SELECT observation,usage FROM task_model_attempts WHERE job_id=?',
                                   (identity.run_id,)).fetchone()
            obs = json.loads(row['observation'])
            self.assertEqual((obs['provider'], obs['finish_reason'], obs['finish_reason_status'], obs['output_limit_status']),
                             ('opencode_go', finish, 'reported', expected))
            self.assertEqual((obs['max_output_tokens'], obs['prepared_output_tokens'], obs['model_window'], obs['input_limit']),
                             (6144, 6144, 20000, budget.input_limit()))
            self.assertEqual(json.loads(row['usage'])['output_tokens'], used)
            self.assertIsInstance(obs['provider_first_packet_ms'], int)
            self.assertIsInstance(obs['attempt_elapsed_ms'], int)
            snapshot = task_events.latest_context_snapshot(identity.run_id)
            self.assertEqual(snapshot['actual_output_tokens'], 6144)
            self.assertEqual(snapshot['input_limit'], budget.input_limit())
            self.assertEqual(snapshot['attempt_elapsed_ms'], obs['attempt_elapsed_ms'])
            self.assertEqual(snapshot['task_budget'], {'calls': 1, 'max_calls': 1,
                                                       'tokens': 100 + used, 'max_tokens': 30_000})

    async def test_legacy_derived_checkpoint_is_ignored_without_rewriting_canonical_tape(self):
        class LegacyState(HarnessState):
            working_context: NotRequired[dict]

        first = self.job(1)
        raw = HumanMessage(content="original prior request", additional_kwargs=first.message_metadata("user"))
        old_answer = AIMessage(content="original prior answer", additional_kwargs=first.message_metadata("assistant"))
        config = {"configurable": framework_checkpoint_scope(first, self.provider)}
        async with open_framework_checkpoint(self.root, first, self.provider) as saver:
            legacy = StateGraph(LegacyState)
            legacy.add_node("legacy_finish", lambda _: {"stop_status": "complete"})
            legacy.add_edge(START, "legacy_finish")
            legacy.add_edge("legacy_finish", END)
            legacy_graph = legacy.compile(checkpointer=saver)
            await legacy_graph.ainvoke({"messages": [raw, old_answer], "active_run_id": first.run_id,
                "executor_id": EXECUTOR_ID, "working_context": {"messages": [HumanMessage("old private summary")]}},
                config, durability="sync")
            self.assertIn("working_context", (await legacy_graph.aget_state(config)).values)
        self.assertTrue(jobs.update_job(first.run_id, status="completed"))

        wire = []

        def transport(request):
            wire.append(json.loads(request.content))
            return _chat_stream()

        second = self.job(2)
        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(transport)), trust_env=False) as client:
            model = ChatOpenAI(use_responses_api=False, model=PROVIDER, api_key="fixture", base_url=BASE,
                max_retries=0, max_tokens=4096, stream_usage=True, http_async_client=client)
            result, _ = await self.run_turn(model, second, "new request", {})
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(wire), 1)
        contents = [m["content"] for m in wire[0]["messages"]]
        self.assertNotIn("old private summary", contents)
        self.assertIn("original prior request", contents)
        self.assertIn("original prior answer", contents)
        self.assertIn("new request", contents)
        async with open_framework_checkpoint(self.root, second, self.provider, read_only=True) as saver:
            checkpoint = await _checkpoint_reader(saver).aget_state(config)
        self.assertNotIn("working_context", checkpoint.values)
        self.assertEqual(checkpoint.values["messages"][:2], [raw, old_answer])
