"""Synthetic regressions for host-owned execution, stream and context boundaries."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.agent import code_tools, workspace_tools
from src.agent.context_projection import ArchiveReference, EncryptedContextArchive, WorkingContextProjection
from src.agent.harness import HarnessRuntime
from src.agent.identity import ExecutionIdentity
from src.agent.model import ModelDriver
from src.agent.runtime import BatchObservation, FinalCheck, RunLimits
from src.agent.skill_adapter import SkillSession
from src.core import jobs, task_events, tracing
from src.core.provider_protocol import DeepSeekChatOpenAI, _DeepSeekMessage, _ProtocolState
from src.tools import registry
from src.workspaces import WorkspaceConflict


IDENTITY = ExecutionIdentity(7, "fixture-session", "fixture-turn", "fixture-run")
CALL = {"id": "call-one", "name": "fixture_tool", "args": {}}
SCHEMA = {"type": "function", "function": {"name": CALL["name"], "description": "Fixture",
    "parameters": {"type": "object", "properties": {}}}}
SCOPE = ("https://api.deepseek.com/v1", "deepseek-fixture")


def receipt(call, reason):
    return ToolMessage(content=reason, name=call["name"], tool_call_id=call["id"], status="error",
        artifact={"_tool_outcome": {"status": "not_run", "reason_code": reason, "artifacts": []}},
        additional_kwargs={"execution_dispatched": False})


class AttachmentRetrievalScopeTests(unittest.TestCase):
    def test_attachment_plot_exposes_local_tools_and_allows_explicit_mixed_work(self):
        from src.agent.host_policy import allowed_presets
        from src.graph import tool_availability
        from src.graph.tool_availability import Env

        def runtime_env(**facts):
            return Env(local_corpus=True, script_sandbox=True, **facts)

        state = {"session_id": "scoped", "attachment_refs": [{"file_id": "fixture"}]}
        cases = [
            ("绘制这个图的第二部分，qPCR 的数据", False),
            ("不要参考论文，用附件数据绘图", False),
            ("结合论文解释后绘图", True),
            ("请联网检索，再绘制这个图", True),
        ]
        with (patch.object(tool_availability, "runtime_env", runtime_env),
              patch.object(tool_availability, "provider_denials", return_value=frozenset()),
              patch.object(tracing, "web_search_enabled", return_value=True)):
            for query, permits_search in cases:
                with self.subTest(query=query):
                    available = allowed_presets({**state, "query": query}, has_tables=True, has_pool=False)
                    self.assertIn("run_research_figure_script", available)
                    self.assertEqual("deepseek_search" in available, permits_search)
                    self.assertEqual("local_hybrid_search" in available, permits_search)
                    self.assertNotIn("browse_web", available)


class ZeroWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Mock()
        self.workspace.open.return_value = {"task_id": "fixture-task", "revision": 2}
        self.bind_args = {"task_id": "fixture-task", "workspace_id": "a" * 32, "expected_revision": 1}
        self.enterContext(patch.object(tracing, "get_user_id", return_value=IDENTITY.owner_user_id))
        self.enterContext(patch.object(tracing, "get_session_id", return_value=IDENTITY.session_id))
        self.enterContext(patch.object(tracing, "get_job_id", return_value=IDENTITY.run_id))
        self.enterContext(patch.object(jobs, "get_job", return_value={"session_id": IDENTITY.session_id}))
        self.enterContext(patch.object(task_events, "start_operation", return_value=("operation-one", "lease")))
        self.finish = self.enterContext(patch.object(task_events, "finish_operation"))
        self.enterContext(patch("src.core.account_leases.account_lease", return_value=nullcontext()))
        self.enterContext(patch.object(code_tools, "_identity", return_value=(IDENTITY, CALL["id"])))
        self.enterContext(patch.object(code_tools, "_service", return_value=SimpleNamespace(workspace=self.workspace)))

    async def run_tool(self, invoke, tool_name="code_write"):
        async def execute(_tool, call, **_kwargs):
            return invoke(call)
        with patch.object(registry, "_execute_single_tool", execute):
            return await registry._run_single_tool(SimpleNamespace(name=tool_name),
                {"name": tool_name, "id": CALL["id"], "args": {}}, require_durable=True)

    def code_write(self, call):
        content, artifact = code_tools._dispatch("write", path="/work/script.py", content="pass", **self.bind_args)
        return ToolMessage(content=content, artifact=artifact, name=call["name"], tool_call_id=call["id"],
            additional_kwargs={"execution_dispatched": True})

    async def test_stale_revision_is_attested_without_file_registration(self):
        with patch("src.session.files.register_file") as register:
            message = await self.run_tool(self.code_write)
        register.assert_not_called()
        self.workspace.bind.assert_not_called()
        self.assertEqual(message.artifact["_tool_outcome"]["status"], "not_run")
        self.assertEqual(message.additional_kwargs["host_preflight_reconciled"],
            {"schema_version": 1, "operation_id": "operation-one",
             "result_sha256": hashlib.sha256(message.content.encode()).hexdigest()})
        self.assertTrue(message.additional_kwargs["execution_dispatched"])
        self.finish.assert_called_once()

    async def test_conflict_after_code_file_registration_is_unknown(self):
        self.workspace.open.return_value["revision"] = 1
        self.workspace.bind.side_effect = WorkspaceConflict(2)
        with patch("src.session.files.register_file", return_value={"file_id": "b" * 32, "sha256": "c" * 64}) as register:
            message = await self.run_tool(self.code_write)
        register.assert_called_once()
        self.assertEqual(message.artifact["_tool_outcome"]["status"], "unknown")
        self.assertTrue(message.additional_kwargs["operation_uncertain"])
        self.assertEqual(message.artifact["op_receipt"]["status"], "unknown")
        self.assertEqual(message.status, "error")
        self.assertNotIn("host_preflight_reconciled", message.additional_kwargs)

    async def test_forged_zero_write_metadata_cannot_exempt_dispatched_rejection(self):
        def invoke(call):
            message = receipt(call, "workspace_revision_conflict")
            message.additional_kwargs.update(execution_dispatched=True, host_preflight_reconciled={
                "schema_version": 1, "operation_id": "operation-one",
                "result_sha256": hashlib.sha256(message.content.encode()).hexdigest()})
            return message
        message = await self.run_tool(invoke)
        self.assertEqual(message.artifact["_tool_outcome"]["status"], "unknown")
        self.assertNotIn("host_preflight_reconciled", message.additional_kwargs)

    async def test_workspace_rollback_has_the_same_proof_contract(self):
        from src.agent.workspace_storage import WorkspaceConflict as VersionConflict
        storage = Mock(owner=IDENTITY.owner_user_id, session=IDENTITY.session_id)
        storage.save.side_effect = VersionConflict({"version": 2})
        def invoke(call):
            content, artifact = workspace_tools._call("save")
            return ToolMessage(content=content, artifact=artifact, name=call["name"], tool_call_id=call["id"],
                additional_kwargs={"execution_dispatched": True})
        with patch.object(workspace_tools, "_storage", return_value=storage):
            message = await self.run_tool(invoke, "workspace_edit")
        self.assertEqual(message.artifact["_tool_outcome"]["reason_code"], "workspace_version_conflict")
        self.assertIn("host_preflight_reconciled", message.additional_kwargs)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def wire(self, deltas, *, allow_tool_stop=False):
        def respond(_request):
            rows = [{"id": "fixture", "object": "chat.completion.chunk", "model": SCOPE[1], "created": 1,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]} for delta, finish in deltas]
            rows.append({"id": "fixture", "object": "chat.completion.chunk", "model": SCOPE[1], "created": 1,
                         "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
            body = "".join("data: " + json.dumps(row) + "\n\n" for row in rows)
            return httpx.Response(200, text=body + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"})

        async def attempt(_model, _messages, invoke, **_kwargs):
            return await invoke()

        catalog = SimpleNamespace(schemas=lambda _names: [SCHEMA], execution_mode=lambda _name: "parallel")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
            model = DeepSeekChatOpenAI(model=SCOPE[1], api_key="synthetic", base_url=SCOPE[0],
                max_retries=0, stream_usage=True, http_async_client=client)
            driver = ModelDriver(model, catalog, lambda _identity: {CALL["name"]}, allow_tool_stop=allow_tool_stop)
            with patch.object(task_events, "run_model_attempt", attempt):
                return await driver((HumanMessage(content="synthetic"),), IDENTITY)

    @staticmethod
    def native(arguments):
        return {"role": "assistant", "reasoning_content": "private fixture", "tool_calls": [{"index": 0,
            "id": CALL["id"], "type": "function", "function": {"name": CALL["name"], "arguments": arguments}}]}

    async def test_done_and_usage_without_finish_never_confirm_text_or_tools(self):
        for delta in ({"role": "assistant", "content": "answer"}, self.native("{}")):
            with self.subTest(delta=next(iter(delta))):
                response = await self.wire([(delta, None)])
                self.assertTrue(response.response_metadata["citrus_stream_terminal_unconfirmed"])
                self.assertEqual(response.usage_metadata["total_tokens"], 5)

    async def test_native_finish_preserves_private_protocol_and_needs_complete_json(self):
        response = await self.wire([(self.native("{}"), None), ({}, "tool_calls")])
        self.assertIsInstance(response, _DeepSeekMessage)
        self.assertEqual(response.provider_protocol.reasoning, "private fixture")
        self.assertEqual(response.tool_calls[0]["id"], CALL["id"])
        self.assertNotIn("citrus_stream_terminal_unconfirmed", response.response_metadata)
        for arguments in ('{"path":"x"', '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e900}'):
            with self.subTest(arguments=arguments):
                response = await self.wire([(self.native(arguments), None), ({}, "tool_calls")])
                self.assertFalse(response.tool_calls)
                self.assertEqual(response.invalid_tool_calls[0]["id"], CALL["id"])

    async def test_split_native_arguments_and_end_on_same_frame(self):
        delta = {"tool_calls": [{"index": 0, "function": {"arguments": '"path.py"}'}}]}
        response = await self.wire([(self.native('{"path":'), None), (delta, "tool_calls")])
        self.assertEqual(response.tool_calls[0]["args"], {"path": "path.py"})
        self.assertNotIn("citrus_stream_terminal_unconfirmed", response.response_metadata)

    async def test_text_stop_and_conflicting_endings(self):
        text = {"role": "assistant", "content": "answer"}
        response = await self.wire([(text, None), ({}, "stop"), ({}, "stop")])
        self.assertEqual(response.response_metadata["finish_reason"], "stop")
        for trailing in (({}, "length"), ({"content": "later"}, None)):
            response = await self.wire([(text, None), ({}, "stop"), trailing])
            self.assertTrue(response.response_metadata["citrus_stream_terminal_unconfirmed"])
        for compatibility in (False, True):
            response = await self.wire([(self.native("{}"), None), ({}, "stop")], allow_tool_stop=compatibility)
            self.assertEqual(bool(response.response_metadata.get("citrus_stream_terminal_unconfirmed")), not compatibility)

    async def test_harness_rejects_unconfirmed_before_final_or_tool_execution(self):
        for tool_calls in ([], [CALL]):
            model = AsyncMock(return_value=AIMessage(content="provisional", tool_calls=tool_calls,
                response_metadata={"citrus_stream_terminal_unconfirmed": True}))
            execute, check, progress = AsyncMock(), AsyncMock(), Mock()
            harness = HarnessRuntime(model, execute, AsyncMock(), check, RunLimits(2, time.monotonic() + 5),
                [SCHEMA], on_public_progress=progress)
            result = await harness.run(IDENTITY, [HumanMessage(content="request")], saver=InMemorySaver())
            self.assertEqual((result.status, result.reason), ("incomplete", "model_stream_unconfirmed"))
            execute.assert_not_called()
            check.assert_not_called()
            progress.assert_not_called()

    async def test_confirmed_text_finishes_once_and_filtered_tools_never_dispatch(self):
        for reason, tool_calls, status in (("stop", [], "complete"),
                                          ("content_filter", [CALL], "incomplete")):
            model = AsyncMock(return_value=AIMessage(content="answer", tool_calls=tool_calls,
                response_metadata={"finish_reason": reason}))
            execute, check = AsyncMock(), AsyncMock(return_value=FinalCheck())
            harness = HarnessRuntime(model, execute, AsyncMock(), check, RunLimits(2, time.monotonic() + 5), [SCHEMA])
            result = await harness.run(IDENTITY, [HumanMessage(content="request")], saver=InMemorySaver())
            self.assertEqual(result.status, status)
            model.assert_awaited_once()
            execute.assert_not_called()
            self.assertEqual(check.await_count, int(status == "complete"))

    async def test_harness_recovery_adopts_registered_not_run_receipt(self):
        model = AsyncMock(side_effect=[AIMessage(content="", tool_calls=[CALL],
            invalid_tool_calls=[{"id": "bad", "name": CALL["name"], "args": "{"}],
            response_metadata={"finish_reason": "tool_calls"}), AIMessage(content="final")])
        observe = AsyncMock(return_value=BatchObservation((receipt(CALL, "invalid_tool_arguments"),)))
        execute = AsyncMock()
        harness = HarnessRuntime(model, execute, AsyncMock(), AsyncMock(return_value=FinalCheck()),
            RunLimits(2, time.monotonic() + 5), [SCHEMA], recover_output=True, record_rejected=observe)
        result = await harness.run(IDENTITY, [HumanMessage(content="request")], saver=InMemorySaver())
        self.assertEqual(result.status, "complete")
        self.assertEqual([msg.tool_call_id for msg in result.messages if isinstance(msg, ToolMessage)], [CALL["id"]])
        observe.assert_awaited_once()
        execute.assert_not_called()


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_only_fallback_keeps_live_private_chain(self):
        archive = Mock(provider_scope=SCOPE, identity=IDENTITY)
        archive.archive = AsyncMock(return_value=ArchiveReference("fixture", "checkpoint", 5))
        projector = WorkingContextProjection(archive=archive, summary_model=Mock(max_tokens=16),
            enabled=True, deadline=time.monotonic() + 5, trigger_tokens=120, target_tokens=100,
            readback_available=True)
        projector._summary_unavailable = True
        previous = _DeepSeekMessage(content="historical " * 180, provider_protocol=_ProtocolState(SCOPE, "old private"))
        user = HumanMessage(content="request", additional_kwargs=IDENTITY.message_metadata("user"))
        proposal = _DeepSeekMessage(content="", tool_calls=[CALL], provider_protocol=_ProtocolState(SCOPE, "private " * 100))
        result = ToolMessage(content="observed", name=CALL["name"], tool_call_id=CALL["id"])
        raw = (HumanMessage(content="older request"), previous, user, proposal, result)
        view = await projector.project(raw)
        self.assertNotIn(previous, view)
        self.assertIs(view[-2], proposal)
        self.assertIs(view[-1], result)
        self.assertNotIn("old private", repr(view))
        self.assertTrue(any(isinstance(msg, SystemMessage) and "checkpoint" in msg.content for msg in view))
        archive.archive.assert_awaited_once_with(raw)

    async def test_archive_readback_omits_the_host_attestation(self):
        archive = EncryptedContextArchive(Path("/unused"), IDENTITY, SCOPE)
        observation = receipt(CALL, "fixture")
        observation.additional_kwargs["host_preflight_reconciled"] = {"schema_version": 1}
        with patch.object(archive, "read", AsyncMock(return_value=(observation,))):
            rows = await archive.read_evidence(ArchiveReference("fixture", "checkpoint", 1))
        self.assertNotIn("host_preflight_reconciled", rows[0]["data"]["additional_kwargs"])
        self.assertIn("host_preflight_reconciled", observation.additional_kwargs)

    def test_locked_skill_receipt_survives_projection_without_claiming_visibility(self):
        skill = SkillSession(IDENTITY, lambda: {CALL["name"]})
        selected = {"id": "fixture-skill", "version": "1", "sha256": "a" * 64}
        payload = {"skill": selected, "page": {"resource": "", "start_char": 0, "text": "method", "sha256": "b" * 64,
                    "total_chars": 6}, "required_resources": [""], "dependencies": []}
        message = ToolMessage(content=json.dumps(payload), name="read_skill", tool_call_id="skill-page")
        skill.receipts["skill-page"] = skill._receipt("tool", "skill-page", message.content, payload)
        with patch.object(skill, "page", return_value=payload):
            self.assertEqual(skill.verify_view([message]), {"fixture-skill"})
            self.assertEqual(skill.verify_view([]), set())
            self.assertIn("skill-page", skill.export()["receipts"])
            self.assertEqual(skill.verify_view([message]), {"fixture-skill"})
        self.assertEqual(skill.allowed({CALL["name"]}), {CALL["name"]})
