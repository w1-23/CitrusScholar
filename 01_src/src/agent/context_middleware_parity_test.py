"""Behavioral contracts against the actual pinned App SIF middleware."""
from importlib.metadata import version

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware import SummarizationMiddleware as LangChainSummarization
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime

from .deep_agent_runtime import HostOnlyBackend


def _call(call_id, name="workspace_write"):
    return {"id": call_id, "name": name, "args": {}, "type": "tool_call"}


def _assistant(call_id, name="workspace_write"):
    return AIMessage("", tool_calls=[_call(call_id, name)], id=f"assistant-{call_id}")


def test_stock_patching_keeps_complete_calls_but_fabricates_unresolved_observations():
    assert (version("deepagents"), version("langchain")) == ("0.7.38", "1.5.0")
    middleware = PatchToolCallsMiddleware()
    assistant = _assistant("operation")
    paired = ToolMessage("committed", id="tool", tool_call_id="operation", name="workspace_write",
                         artifact={"op_receipt": {"status": "complete", "operation_id": "issued"}})
    assert middleware.before_agent({"messages": [assistant, paired]}, Runtime()) is None

    # An unclosed write has to be reconciled against storage, not closed by the SDK.
    raw = [assistant]
    patch = middleware.before_agent({"messages": raw}, Runtime())
    patched = add_messages(raw, patch["messages"])
    assert len(patched) == 2 and patched[0] == assistant
    assert patched[1].tool_call_id == "operation" and patched[1].status == "error"
    assert patched[1].artifact is None and not patched[1].additional_kwargs
    # Matching a bare ID anywhere cannot prove a contiguous pair.
    assert middleware.before_agent({"messages": [paired, assistant]}, Runtime()) is None


def test_langchain_summarizes_a_closed_pair_but_replaces_canonical_prefix():
    model = FakeListChatModel(responses=["summary without evidence or receipt"])
    middleware = LangChainSummarization(model, trigger=("messages", 4), keep=("messages", 1))
    evidence = ToolMessage("exact [W1] evidence and operation receipt", tool_call_id="old",
                           name="workspace_write", id="receipt")
    current = HumanMessage("current user", id="current")
    raw = [HumanMessage("old user", id="old"), _assistant("old"), evidence, current]
    proposed = middleware.before_model({"messages": raw}, Runtime())
    result = add_messages(raw, proposed["messages"])
    assert result[-1] == current and len(result) == 2
    assert "summary without evidence" in result[0].content
    assert "[W1]" not in result[0].content and evidence not in result


def test_sdk_summary_continues_after_offload_denial_and_omits_exact_evidence():
    import asyncio

    async def scenario():
        model = FakeListChatModel(responses=["model-generated navigation, not evidence"])
        middleware = SummarizationMiddleware(model, backend=HostOnlyBackend(None),
                                             trigger=("messages", 4), keep=("messages", 1))
        evidence = ToolMessage("exact [W1] evidence and operation receipt", tool_call_id="old",
                              name="workspace_write", id="receipt")
        current = HumanMessage("current user", id="current")
        raw = [HumanMessage("old user", id="old"), _assistant("old"), evidence, current]
        sent = []

        async def handle(request):
            sent.extend(request.messages)
            return ModelResponse(result=[AIMessage("fixture answer")])

        with pytest.warns(UserWarning, match="Offloading conversation history"):
            response = await middleware.awrap_model_call(ModelRequest(
                model=model, messages=raw, state={"messages": raw}), handle)
        assert sent[-1] == current and evidence not in sent
        assert "model-generated navigation" in sent[0].content and not sent[0].additional_kwargs.get("context_trusted")
        assert response.command.update["_summarization_event"]["file_path"] is None
        assert raw[2] is evidence  # different from LangChain's state rewrite

    asyncio.run(scenario())


def test_stock_eviction_can_read_bytes_but_bypasses_receipt_file_policy(tmp_path):
    import asyncio

    async def scenario():
        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = FilesystemMiddleware(backend=backend,
            permissions=[FilesystemPermission(operations=["write", "edit", "delete"],
                                              paths=["/**"], mode="deny")],
            tool_token_limit_before_evict=1, human_message_token_limit_before_evict=None)
        text = "result points to scientific evidence [W1] " * 150
        response = ToolMessage(text, tool_call_id="tool-1", name="workspace_write", id="result",
            additional_kwargs={"context_trusted": True}, artifact={"op_receipt": {"id": "real-receipt"}})

        async def handle(_):
            return response

        evicted = await middleware.awrap_tool_call(ToolCallRequest(
            tool_call=_call("tool-1"), tool=None, state={}, runtime=Runtime()), handle)
        retained = list(tmp_path.rglob("tool-1"))
        assert len(retained) == 1 and retained[0].read_text() == text
        assert evicted.tool_call_id == response.tool_call_id
        assert evicted.content != text and "/large_tool_results/tool-1" in evicted.content
        assert evicted.artifact == response.artifact
        assert "full_sha256" not in evicted.content and "read_tool_result" not in evicted.content

    asyncio.run(scenario())


def test_stock_human_eviction_precedes_provider_admission(tmp_path):
    import asyncio

    async def scenario():
        model = FakeListChatModel(responses=["ok"])
        middleware = FilesystemMiddleware(backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
            tool_token_limit_before_evict=None, human_message_token_limit_before_evict=1)
        current = HumanMessage("current exact request " * 600, id="current")
        observed = []

        async def capture(request):
            observed.extend(request.messages)
            return ModelResponse(result=[AIMessage("ok")])

        response = await middleware.awrap_model_call(ModelRequest(model=model, messages=[current],
                                                          state={"messages": [current]}), capture)
        assert observed[0].content != current.content
        saved = response.command.update["messages"][0]
        path = saved.additional_kwargs["lc_evicted_to"]
        assert saved.content == current.content and saved.id == current.id
        assert (tmp_path / path.lstrip("/")).read_text() == current.content
        assert not current.additional_kwargs  # canonical content is not the issue; admission sees only the stub

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["exact", "opt_out", "missing_current", "unclosed_batch", "foreign_run",
                                 "untrusted", "incomplete", "reused", "wrong_source", "wrong_digest",
                                 "changed_selector", "changed_cursor", "different_bytes", "denied_old",
                                 "denied_current", "unoffered_source"])
def test_repeated_page_edit_requires_exact_bound_current_proof(case):
    import asyncio
    import hashlib
    import json
    from copy import deepcopy
    from unittest.mock import patch

    from src.agent.deep_agent_runtime import prepare_context_for_send
    from src.agent.identity import ExecutionIdentity
    from src.core import request_context, task_events
    from src.core.tool_results import execution_receipt_key

    older, active = ExecutionIdentity(7, "s", "old-turn", "old-job"), ExecutionIdentity(7, "s", "now", "active-job")
    model = FakeListChatModel(responses=["must not call a model"])
    key, digest = execution_receipt_key(older, "source"), "a" * 64
    source = {"tool_call_id": key, "full_sha256": digest, "original_chars": 9000, "stored_chars": 9000}
    preview = ToolMessage(json.dumps({"delivery": "stored_result_reference", "tool_result_ref": source,
            "read_with": {"tool": "read_tool_result", "tool_call_id": key}, "complete": False}),
        name="inspect_uploaded_table", tool_call_id="source", additional_kwargs={**older.message_metadata("tool"),
            "result_reference": {**source, "provider_call_id": "source"}})
    body = {"status": "ok", "tool_call_id": key, "tool_result_ref": source, "selected_fields": ["/fields"],
            "storage": {"complete": True}, "_tool_outcome": {"status": "complete"}, "missing_fields": [],
            "content_origin": "untrusted_historical_tool_result", "result_text": "é" * 4000,
            "page": {"start_char": 20, "end_char": 4020, "total_chars": 9000,
                     "offset_basis": "selected_fields_json", "next_cursor": "cursor"}}
    other = deepcopy(body)
    if case == "wrong_source":
        other["tool_result_ref"]["tool_call_id"] = other["tool_call_id"] = "tr_" + "3" * 64
    if case == "wrong_digest":
        other["tool_result_ref"]["full_sha256"] = "b" * 64
    if case == "changed_selector":
        other["selected_fields"] = ["/raw_preview"]
    if case == "changed_cursor":
        other["page"]["next_cursor"] = "another-cursor"
    if case == "different_bytes":
        other["result_text"] = "ê" * 4000
    if case == "incomplete":
        # Even equal bodies cannot remove partial tool results.
        other["_tool_outcome"]["status"] = body["_tool_outcome"]["status"] = "partial"
    if case == "reused":
        other["status"] = body["status"] = "reused"

    def issuer(cid, name, identity, extra=()):
        return AIMessage("", tool_calls=[_call(cid, name), *extra],
                         additional_kwargs=identity.message_metadata("assistant"))

    old_page = ToolMessage(json.dumps(body, ensure_ascii=False), name="read_tool_result", tool_call_id="old-page",
                           additional_kwargs=older.message_metadata("tool"))
    current_meta = (older if case == "foreign_run" else active).message_metadata("tool")
    if case == "untrusted":
        current_meta["context_trusted"] = False
    current_page = ToolMessage(json.dumps(other, ensure_ascii=False), name="read_tool_result",
                               tool_call_id="current-page", additional_kwargs=current_meta)
    messages = [issuer("source", "inspect_uploaded_table", older), preview,
                issuer("old-page", "read_tool_result", older), old_page,
                HumanMessage("recheck", additional_kwargs=active.message_metadata("user"))]
    if case != "missing_current":
        extra = [_call("unclosed", "workspace_write")] if case == "unclosed_batch" else []
        messages.extend([issuer("current-page", "read_tool_result", active, extra), current_page])
    checked = []

    def receipt_for(message, *, existing_delivery=False, current_readback=False):
        checked.append((message.tool_call_id, existing_delivery, current_readback))
        if (case, message.tool_call_id) in {("denied_old", "old-page"), ("denied_current", "current-page"),
                                          ("unoffered_source", "source")}:
            return None
        return {"tool_call_id": key, "full_sha256": digest}

    def estimate(_, view):
        return None, {"estimated_input_tokens": sum(len(m.content) for m in view), "output_tokens": 4096}

    with (patch.object(request_context, "model_request", estimate),
          patch.object(task_events, "model_input_limit", return_value=128_000)):
        edited, offers = asyncio.run(prepare_context_for_send(model, model, messages, active, receipt_for,
                                                             edit=case != "opt_out"))
    assert offers == (() if case == "unoffered_source" else (key,))
    if case == "exact":
        assert json.loads(edited[3].content)["duplicate_of_tool_call_id"] == "current-page"
        assert edited[3].response_metadata["context_editing"]["strategy"] == "clear_tool_uses"
        assert {(cid, current) for cid, _, current in checked} == {
            ("source", False), ("old-page", False), ("current-page", True)}
    else:
        assert edited == messages
    assert messages[3] is old_page and "context_editing" not in old_page.response_metadata
    if case != "missing_current":
        assert edited[-1] is current_page
    assert hashlib.sha256(old_page.content.encode()).hexdigest() == hashlib.sha256(messages[3].content.encode()).hexdigest()
