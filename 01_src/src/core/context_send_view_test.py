"""Send view keeps authorized raw history and independently scoped host state."""
import asyncio
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from src.core import checkpoint_restore
from src.core.context_manager import ContextManager


class FakeSession:
    def __init__(self, summary, working_state=None):
        self.summary = summary
        self.working_state = working_state or {}

    def get_checkpoint(self, session_id):
        return {"msg_id": 2, "summary": self.summary, "revision": 1,
                "checkpoint_watermark": 2, "message_watermark": 4,
                "working_state": self.working_state}


class ExplodingBudget:
    def check(self, *args, **kwargs):
        raise AssertionError("load-time model compaction must not run")


def document():
    return {
        "schema": checkpoint_restore.SCHEMA,
        "generator": checkpoint_restore.GENERATOR,
        "navigation_authoritative": False,
        "owner_user_id": 5,
        "session_id": "s",
        "navigation": {"goal": "goal", "decisions": ["one"], "unfinished": []},
        "host_task_status": "completed",
        "host_task_state": {"turn_outcome": {"status": "completed"}},
        "source_inventory": [{"source_id": "1", "role": "human", "total_chars": 3,
                              "preview_sha256": "a"},
                             {"source_id": "2", "role": "ai", "total_chars": 3,
                              "preview_sha256": "b"}],
        "inventory_sha256": "digest",
        "tool_result_call_ids": [],
        "exact_excerpts": [{"source_id": "2", "start_char": 0, "end_char": 3,
                            "quote": "UNIQUE-EXCERPT", "quote_sha256": "hash"}],
    }


class SendViewTests(unittest.TestCase):
    def setUp(self):
        self.raw = [HumanMessage(content="u1"), AIMessage(content="a1"),
                    HumanMessage(content="u2"), AIMessage(content="a2")]
        self.ids = [1, 2, 3, 4]

    def view(self, summary, *, valid=None, working_state=None):
        manager = ContextManager(session_manager=FakeSession(summary, working_state),
                                 budget=ExplodingBudget())
        reason = "source_integrity_failed" if valid is False else None
        context = patch.object(checkpoint_restore, "checkpoint_rejection", return_value=reason)
        with context as validator:
            messages, compacted = asyncio.run(manager._build_send_view("s", self.raw, self.ids, "u2"))
            if valid is not None:
                validator.assert_called_once_with(json.loads(summary), manager._session, "s", 2,
                                                  rejected_sources=set())
            return messages, compacted

    def test_verified_checkpoint_is_checked_but_never_replaces_raw_history(self):
        messages, compacted = self.view(json.dumps(document()), valid=True)
        self.assertEqual([m.content for m in messages], ["u1", "a1", "u2", "a2"])
        self.assertFalse(compacted)

    def test_legacy_prose_is_not_injected_and_raw_history_is_kept(self):
        messages, compacted = self.view("legacy model summary")
        self.assertFalse(compacted)
        self.assertEqual([m.content for m in messages], ["u1", "a1", "u2", "a2"])
        self.assertNotIn("<conversation_checkpoint>",
                         "\n".join(str(m.content) for m in messages))

    def test_failed_validation_keeps_every_raw_message(self):
        messages, compacted = self.view(json.dumps(document()), valid=False)
        self.assertFalse(compacted)
        self.assertEqual([m.content for m in messages], ["u1", "a1", "u2", "a2"])
        self.assertNotIn("<conversation_checkpoint>",
                         "\n".join(str(m.content) for m in messages))

    def test_working_state_is_kept_without_authorizing_prose(self):
        messages, _ = self.view("legacy model summary",
                                working_state={"verified": 1, "projection_cache": {"private": "not prompt"}})
        self.assertEqual(json.loads(messages[0].content.splitlines()[1]), {"verified": 1})
        self.assertTrue(messages[0].content.startswith("<context_working_state>"))
        self.assertEqual([m.content for m in messages[1:]], ["u1", "a1", "u2", "a2"])


def test_experiment_turns_keep_scope_and_task_snapshot_after_sqlite_readback(tmp_path):
    from src.core.capability_policy import CapabilityPolicy, history_rows
    from src.graph.session_persistence import persist_agent_turn
    from src.session import manager

    async def scenario():
        store = object.__new__(manager.SessionManager)
        store.db_path = str(tmp_path / "history.sqlite3")
        store._init_db_sync()
        session = await store.get_or_create_session("shared-fixture", owner_user_id=5)
        assert await store.save_messages(session, [HumanMessage("legacy question"), AIMessage("local evidence")])
        isolated = CapabilityPolicy(local_rag=False, experiment_group="without-rag")
        state = {"session_id": session, "query": "experiment question", "answer": "network result",
                 "turn_trace": [HumanMessage("experiment question")],
                 "capability_snapshot": isolated.snapshot()}
        with patch.object(manager, "session_manager", store):
            saved = await persist_agent_turn(state)
        assert saved["_trace"]["history_saved"]
        reopened = object.__new__(manager.SessionManager)
        reopened.db_path = store.db_path
        messages, row_ids = await reopened.get_messages_with_ids(session)
        assert [message.content for message in messages] == [
            "legacy question", "local evidence", "experiment question", "network result"]
        isolated_rows, _ = history_rows(messages, row_ids, isolated)
        assert [message.content for message in isolated_rows] == ["experiment question", "network result"]
        assert isolated_rows[-1].response_metadata["citrus_task_snapshot"]["capability_snapshot"] == isolated.snapshot()
        ordinary_rows, _ = history_rows(messages, row_ids, CapabilityPolicy(summarization=False))
        assert [message.content for message in ordinary_rows] == ["legacy question", "local evidence"]

    asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
