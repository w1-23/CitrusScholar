"""Read-only archive compatibility and fail-closed old executor admission."""
import asyncio
import hashlib

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph

from src.agent.checkpoint_store import (WORKING_CONTEXT_ARCHIVE_NAMESPACE, checkpoint_scope,
    native_checkpoint_path, native_checkpoint_scope, open_checkpoint, open_native_checkpoint)
from src.agent.context_projection import ArchiveReference, EncryptedContextArchiveReader
from src.agent.deep_agent_runtime import LEGACY_NATIVE_EXECUTOR_ID
from src.agent.harness import HarnessState
from src.agent.identity import ExecutionIdentity
from src.agent.recovery import load_recovery_state, validate_legacy_native_head
from src.agent.runtime import AdmissionStopped
from src.core import jobs, task_events
from src.core.provider_protocol import _DeepSeekMessage, _ProtocolState

SCOPE = ("https://api.deepseek.com/v1", "deepseek-fixture")


def test_archive_opens_without_writing_or_publishing_protocol_state(tmp_path):
    async def run():
        identity = ExecutionIdentity(1, "s", "t", "j")
        private = _ProtocolState(SCOPE, "a synthetic private value")
        call = {"id": "legacy-call", "name": "read_data", "args": {}}
        proposal = _DeepSeekMessage(content="", tool_calls=[call], provider_protocol=private)
        proposal.tool_calls[0].update(_host_logical_key="host:" + "a" * 32, _attempt=1)
        tool = ToolMessage(content="原始证据", name="read_data", tool_call_id="legacy-call",
                           additional_kwargs={"host_preflight_reconciled": {"schema_version": 1}})
        raw = [HumanMessage("original"), proposal, tool]
        config = {"configurable": {**checkpoint_scope(identity, SCOPE),
                                    "checkpoint_ns": WORKING_CONTEXT_ARCHIVE_NAMESPACE}}
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": raw}
        checkpoint["channel_versions"] = {"messages": 1}
        async with open_checkpoint(tmp_path, identity, SCOPE) as saver:
            saved = await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {"messages": 1})
        reference = ArchiveReference(config["configurable"]["thread_id"], saved["configurable"]["checkpoint_id"], 3)
        reader = EncryptedContextArchiveReader(tmp_path, identity, SCOPE)
        snapshot = {p: hashlib.sha256(p.read_bytes()).digest() for p in tmp_path.rglob("*") if p.is_file()}
        restored = await reader.read(reference)
        assert restored[1].provider_protocol == private
        assert restored[1].tool_calls == [call | {"type": "tool_call"}]
        public = await reader.read_evidence(reference)
        assert "a synthetic private value" not in str(public)
        assert "host_preflight_reconciled" not in str(public)
        assert "原始证据" in str(public)
        assert {p: hashlib.sha256(p.read_bytes()).digest() for p in tmp_path.rglob("*") if p.is_file()} == snapshot
        with pytest.raises(AdmissionStopped, match="scope_mismatch"):
            await EncryptedContextArchiveReader(tmp_path, ExecutionIdentity(2, "s", "t", "j"), SCOPE).read(reference)

    asyncio.run(run())


@pytest.mark.parametrize("hazard", [None, "unpaired", "unknown", "journal_interrupted", "not_terminal"])
def test_terminal_orphan_admits_only_paired_and_settled_old_thread(tmp_path, monkeypatch, hazard):
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "jobs.sqlite3")

    async def run():
        previous_job = jobs.claim_job("session", "previous", owner_user_id=2, client_request_id="previous", payload={})
        previous = ExecutionIdentity(2, "session", "previous", previous_job.job_id)
        next_turn = ExecutionIdentity(2, "session", "new", "new-job")
        async with open_native_checkpoint(tmp_path, previous, SCOPE) as saver:
            old_graph = StateGraph(HarnessState)
            old_graph.add_node("model", lambda state: {})
            old_graph.add_edge(START, "model")
            old_graph.add_edge("model", END)
            compiled = old_graph.compile(checkpointer=saver, interrupt_before=["model"])
            original = {"user_id": 2, "session_id": "session", "request_id": "previous", "job_id": previous.run_id}
            messages = [HumanMessage("prior request")]
            if hazard == "unpaired":
                messages.append(AIMessage("", tool_calls=[{"name": "fake_effect", "id": "call", "args": {}}]))
            await compiled.ainvoke({"messages": messages, "active_run_id": previous.run_id,
                    "executor_id": LEGACY_NATIVE_EXECUTOR_ID,
                    "unknown_operations": ["a" * 32] if hazard == "unknown" else [],
                    "host_state": {"original_request_state": original}},
                    {"configurable": native_checkpoint_scope(previous, SCOPE)}, durability="sync")
        if hazard == "journal_interrupted":
            op, lease = task_events.start_operation(previous.run_id, 2, "fake_effect", {}, recovery_mode="never",
                                                          input_refs=[], tool_call_id="call", logical_key="host:" + "a" * 32)
            task_events.abandon_operation(op, lease)
        if hazard != "not_terminal":
            with task_events.transaction() as conn:
                conn.execute("UPDATE task_jobs SET status='failed',terminal_snapshot='{}' WHERE job_id=?", (previous.run_id,))
        db = native_checkpoint_path(tmp_path, previous, SCOPE)
        before = hashlib.sha256(db.read_bytes()).digest()
        if hazard:
            with pytest.raises(AdmissionStopped, match="requires_reconciliation"):
                await validate_legacy_native_head(tmp_path, next_turn, SCOPE)
        else:
            await validate_legacy_native_head(tmp_path, next_turn, SCOPE)
        with pytest.raises(task_events.TaskConflict, match="executor_checkpoint_incompatible"):
            await load_recovery_state(tmp_path, previous, SCOPE)
        assert hashlib.sha256(db.read_bytes()).digest() == before

    asyncio.run(run())


def test_legacy_projection_receipt_stays_off_model_working_prompt():
    from src.core.context_manager import prompt_working_state
    working = {"goal": "g", "dataset_refs": [{"file_id": "f"}],
               "projection_cache": {"schema": "working_summary_cache/2", "digest": "0" * 64}}
    assert prompt_working_state(working)["goal"] == "g"
    assert "projection_cache" not in prompt_working_state(working)
    assert prompt_working_state(None) == {}
