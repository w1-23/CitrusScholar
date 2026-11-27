"""Reconstruction keeps public history out of the private protocol namespace."""
import asyncio
from contextlib import closing
import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agent.checkpoint_store import (
    CheckpointProtectionError, framework_checkpoint_scope, open_framework_checkpoint,
)
from src.agent.framework_test_support import ScriptedModel
from src.agent.harness import HarnessRuntime
from src.agent.identity import ExecutionIdentity
from src.agent.runtime import FinalCheck, RunLimits
from src.core.capability_policy import CapabilityPolicy
from src.graph.session_persistence import persist_agent_turn
from src.session import manager


PROVIDER = ("https://example.test/v1", "fixture")
SNAPSHOT = "citrus_task_snapshot"


def test_persisted_history_projects_into_new_and_reopened_protected_thread(tmp_path, monkeypatch):
    async def scenario():
        store = object.__new__(manager.SessionManager)
        store.db_path = str(tmp_path / "history.sqlite3")
        store._init_db_sync()
        monkeypatch.setattr(manager, "session_manager", store)
        session = await store.get_or_create_session("history-checkpoint", owner_user_id=7)
        policy = CapabilityPolicy(local_rag=False, experiment_group="projection-fixture")
        root = tmp_path / "protected"
        seen = []
        request = {}

        async def model(messages, identity, **_):
            seen.append({str(message.text): message.response_metadata for message in messages})
            return AIMessage(f"answer {identity.user_turn_id}")

        async def accept(*_):
            return FinalCheck()

        runtime = HarnessRuntime(ScriptedModel(script=model), None, lambda *_: None, accept,
                                 RunLimits(), [], export_host=lambda: {"original_request_state": dict(request)})

        async def save_turn(identity, protocol, *, legacy=False):
            answer = f"answer {identity.user_turn_id}"
            snapshot = {"schema": 1, "provider_protocol": protocol, "summary_enabled": True}
            saved = await persist_agent_turn({"session_id": session, "request_id": identity.user_turn_id,
                "query": identity.user_turn_id, "answer": answer, "capability_snapshot": policy.snapshot(),
                "capability_runtime": snapshot, "turn_trace": [HumanMessage(identity.user_turn_id)],
                "references_data": {"display": "bubble_only", "bubbles": [{"id": "fixture"}]}})
            assert saved["_trace"]["history_saved"]
            if legacy:
                with closing(sqlite3.connect(store.db_path)) as conn, conn:
                    conn.execute("UPDATE messages SET msg_type='AIMessageChunk' WHERE content=?", (answer,))

        def stored_rows():
            with closing(sqlite3.connect(store.db_path)) as conn:
                return conn.execute("SELECT id,msg_type,context_metadata_json FROM messages ORDER BY id").fetchall()

        async def run_turn(identity, protocol, previous_answer):
            before = stored_rows()
            history, ids = await store.get_messages_with_ids(session)
            display, display_ids, _ = await store.get_display_history(session)
            assert ids == display_ids == [row[0] for row in before]
            assert [m.content for m in history] == [m.content for m in display]
            shown = next(m for m in display if m.content == previous_answer).response_metadata
            assert shown[SNAPSHOT]["capability_runtime"]["provider_protocol"] == protocol
            assert shown[SNAPSHOT]["capability_snapshot"] == policy.snapshot()
            assert shown["citrus_citation_bubbles"] == [{"id": "fixture"}]
            request["request_id"] = identity.user_turn_id
            config = framework_checkpoint_scope(identity, PROVIDER)
            human = HumanMessage(identity.user_turn_id, additional_kwargs=identity.message_metadata("user"))
            async with open_framework_checkpoint(root, identity, PROVIDER) as saver:
                result = await runtime.run(identity, [*history, human], saver=saver, checkpoint_config=config)
                assert result.status == "complete"
                head = await saver.aget_tuple({"configurable": config})
                assert head.checkpoint["channel_values"]["active_run_id"] == identity.run_id
            assert stored_rows() == before
            projected = seen[-1][previous_answer]
            assert projected[SNAPSHOT]["capability_runtime"] == {"schema": 1, "summary_enabled": True}
            assert projected[SNAPSHOT]["capability_snapshot"] == policy.snapshot()
            assert projected["citrus_capability_history"] == policy.history_key
            assert projected["citrus_citation_bubbles"] == shown["citrus_citation_bubbles"]

        old = ExecutionIdentity(7, session, "legacy", "job-legacy")
        first = ExecutionIdentity(7, session, "first", "job-first")
        imported = ExecutionIdentity(7, session, "imported", "job-imported")
        following = ExecutionIdentity(7, session, "following", "job-following")
        await save_turn(old, "deepseek", legacy=True)
        await run_turn(first, "deepseek", "answer legacy")
        await save_turn(first, "openai")
        await save_turn(imported, "openai")
        assert framework_checkpoint_scope(first, PROVIDER) == framework_checkpoint_scope(following, PROVIDER)
        await run_turn(following, "openai", "answer imported")
        assert "answer legacy" in seen[-1] and "answer first" in seen[-1]
        assert len(seen) == 2
        assert [json.loads(row[2])[SNAPSHOT]["capability_runtime"]["provider_protocol"]
                for row in stored_rows() if row[1] in ("ai", "AIMessageChunk")] == ["deepseek", "openai", "openai"]

    asyncio.run(scenario())


def test_saver_still_rejects_provider_protocol_in_public_message_fields(tmp_path):
    async def scenario():
        store = object.__new__(manager.SessionManager)
        store.db_path = str(tmp_path / "history.sqlite3")
        store._init_db_sync()
        session = await store.get_or_create_session("history-checkpoint", owner_user_id=7)
        identity = ExecutionIdentity(7, session, "turn", "job")
        malformed = {SNAPSHOT: {"capability_runtime": {"provider_protocol": {"reasoning": "fixture"}}}}
        assert await store.save_messages(session, [HumanMessage("question"),
                                                   AIMessage("public", response_metadata=malformed)])
        history, _ = await store.get_messages_with_ids(session)
        assert history[-1].response_metadata == malformed
        async with open_framework_checkpoint(tmp_path, identity, PROVIDER) as saver:
            for message in (history[-1], AIMessage("public", response_metadata={"provider_protocol": "openai"}),
                            AIMessage("public", response_metadata={SNAPSHOT: {"capability_runtime": {
                                "provider_protocol": "openai"}}})):
                with pytest.raises(CheckpointProtectionError, match="^private_state_in_public_fields$"):
                    saver.serde.dumps_typed(message)

    asyncio.run(scenario())
