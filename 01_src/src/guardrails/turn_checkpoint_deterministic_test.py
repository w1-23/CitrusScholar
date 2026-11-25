"""Legacy checkpoint integrity and raw conversation loading without a writer."""
import asyncio
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core import tracing
from src.core.context_manager import ContextManager, finalize_load_result
from src.core.checkpoint_restore import (GENERATOR, SCHEMA, inventory_digest, source_inventory,
                                         structured_checkpoint, validate_checkpoint)
from src.guardrails import turn_checkpoint
from src.session import manager


class DeterministicCheckpointTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(manager, "STATE_DIR", root))
        self.enterContext(patch.object(manager, "DB_PATH", root / "history.db"))
        self.enterContext(patch.object(manager.SessionManager, "_instance", None))
        self.manager = manager.SessionManager()
        self.sid = "det-session"
        self.owner = 77
        asyncio.run(self.manager.get_or_create_session(self.sid, owner_user_id=self.owner))
        prior_session, prior_user = tracing.get_session_id(), tracing.get_user_id()
        self.addCleanup(tracing.set_session_id, prior_session)
        self.addCleanup(tracing.set_user_id, prior_user)
        tracing.set_session_id(self.sid)
        tracing.set_user_id(self.owner)
        asyncio.run(self.manager.save_messages(self.sid, [
            HumanMessage(content="What changed?",
                         additional_kwargs={"request_id": "req-1"}),
            AIMessage(content="A" * 300,
                      additional_kwargs={"request_id": "req-1"}),
        ], idempotency_key="crid:req-1"))
        self.watermark = self.manager.get_checkpoint(self.sid)["message_watermark"]
    def seed_legacy_snapshot(self, *, tool_hashes=None):
        """Build an old-format row as test data, never through a runtime writer."""
        cutoff = self.manager.get_checkpoint(self.sid)["message_watermark"]
        sources = self.manager.get_checkpoint_sources(self.sid, cutoff)
        inventory = source_inventory(sources)
        first = sources[0]
        quote = self.manager.get_context_source(self.sid, int(first["source_id"]), 0, 8)["text"]
        hashes = tool_hashes or {}
        document = {
            "schema": SCHEMA, "generator": GENERATOR, "navigation_authoritative": False,
            "owner_user_id": self.owner, "session_id": self.sid,
            "navigation": {"goal": "legacy navigation must not be used"},
            "host_task_status": "completed",
            "host_task_state": {"turn_outcome": {"status": "completed"}},
            "source_inventory": inventory, "inventory_sha256": inventory_digest(inventory),
            "tool_result_call_ids": list(hashes), "tool_result_sha256": hashes,
            "exact_excerpts": [{"source_id": first["source_id"], "start_char": 0,
                                "end_char": len(quote), "quote": quote,
                                "quote_sha256": hashlib.sha256(quote.encode()).hexdigest()}],
        }
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("UPDATE sessions SET checkpoint_msg_id=?, checkpoint_watermark=?, "
                         "checkpoint_summary=? WHERE session_id=?",
                         (cutoff, cutoff, json.dumps(document), self.sid))
            conn.commit()
        return document

    def test_stored_fixture_validates_in_read_only_mode(self):
        doc = self.seed_legacy_snapshot()
        self.assertEqual(doc["schema"], SCHEMA)
        self.assertEqual(doc["generator"], GENERATOR)
        self.assertIs(doc["navigation_authoritative"], False)
        self.assertEqual(doc["host_task_status"], "completed")
        self.assertEqual(doc["source_inventory"][-1]["source_id"], str(self.watermark))
        inventory = doc["source_inventory"]
        canonical = json.dumps(inventory, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        self.assertEqual(doc["inventory_sha256"],
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertTrue(doc["exact_excerpts"])
        for item in doc["exact_excerpts"]:
            self.assertEqual(item["quote_sha256"], hashlib.sha256(
                item["quote"].encode("utf-8")).hexdigest())
        self.assertTrue(validate_checkpoint(doc, self.manager, self.sid, self.watermark))

    def test_legacy_prose_is_never_authoritative(self):
        self.assertIsNone(structured_checkpoint("legacy model summary"))
        self.assertFalse(validate_checkpoint("legacy model summary", self.manager,
                                             self.sid, self.watermark))
        doc = self.seed_legacy_snapshot()
        legacy = dict(doc)
        legacy.pop("generator")
        self.assertFalse(validate_checkpoint(legacy, self.manager, self.sid, self.watermark))

    def test_tampered_documents_are_rejected(self):
        doc = self.seed_legacy_snapshot()
        cases = []
        wrong_generator = json.loads(json.dumps(doc))
        wrong_generator["generator"] = "model"
        cases.append(wrong_generator)
        wrong_owner = json.loads(json.dumps(doc))
        wrong_owner["owner_user_id"] = self.owner + 1
        cases.append(wrong_owner)
        wrong_inventory = json.loads(json.dumps(doc))
        wrong_inventory["source_inventory"][0]["preview_sha256"] = "0" * 64
        cases.append(wrong_inventory)
        wrong_digest = json.loads(json.dumps(doc))
        wrong_digest["inventory_sha256"] = "0" * 64
        cases.append(wrong_digest)
        wrong_quote = json.loads(json.dumps(doc))
        wrong_quote["exact_excerpts"][0]["quote"] = "tampered"
        cases.append(wrong_quote)
        for case in cases:
            self.assertFalse(validate_checkpoint(case, self.manager, self.sid,
                                                 self.watermark))

    def restore(self, *, user_id=None, memory=None, capability_policy=None):
        with patch.object(manager.SessionManager, "_instance", None):
            reopened = manager.SessionManager()
            loader = ContextManager(session_manager=reopened, memory_store=memory)
            return asyncio.run(loader.load(self.sid, "follow-up", "general_agent",
                                          user_id=user_id or self.owner, generate_hints=False,
                                          capability_policy=capability_policy))

    def test_old_schema_falls_back_and_even_verified_schema_never_replaces_raw_history(self):
        old = self.seed_legacy_snapshot()
        old["schema"] = "citrus-turn-checkpoint/1"
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("UPDATE sessions SET checkpoint_summary=? WHERE session_id=?",
                         (json.dumps(old), self.sid))
            conn.commit()
        ctx = self.restore()
        self.assertEqual((ctx.history_load_status, ctx.history_load_reason),
                         ("degraded", "legacy_checkpoint_untrusted"))
        self.assertEqual([m.content for m in ctx.history_messages], ["What changed?", "A" * 300])
        doc = self.seed_legacy_snapshot()
        with patch.object(self.manager.__class__, "set_checkpoint", side_effect=AssertionError("write")):
            restored = self.restore()
        self.assertEqual((restored.history_load_status, restored.history_load_reason), ("loaded", ""))
        self.assertEqual([m.content for m in restored.history_messages], ["What changed?", "A" * 300])
        self.assertFalse(restored.compacted)
        self.assertEqual(json.loads(self.manager.get_checkpoint(self.sid)["summary"]), doc)

    def test_legacy_structured_checkpoint_without_publication_keeps_raw_history(self):
        call = {'id': 'legacy-read', 'name': 'read_data', 'args': {}}
        original = [AIMessage(content='', tool_calls=[call]),
                    ToolMessage(content='verified synthetic evidence', tool_call_id=call['id'], name=call['name']),
                    AIMessage(content='original answer')]
        asyncio.run(self.manager.save_messages(self.sid, original, idempotency_key='crid:legacy-raw'))
        cutoff = self.manager.get_checkpoint(self.sid)['message_watermark']
        legacy = {'schema': 'citrus-turn-checkpoint/1', 'owner_user_id': self.owner,
                  'session_id': self.sid, 'navigation': {'goal': 'unverified legacy navigation'},
                  'tool_result_call_ids': [call['id']]}
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute('UPDATE sessions SET checkpoint_summary=?, checkpoint_msg_id=? WHERE session_id=?',
                         (json.dumps(legacy), cutoff, self.sid))
            conn.commit()
        restored = self.restore()
        self.assertEqual(restored.history_load_reason, 'legacy_checkpoint_untrusted')
        self.assertEqual([m.content for m in restored.history_messages],
                         ['What changed?', 'A' * 300, '', 'verified synthetic evidence', 'original answer'])
        self.assertEqual(restored.history_messages[2].tool_calls[0]['id'], call['id'])
        self.assertEqual(restored.history_messages[3].tool_call_id, call['id'])
        self.assertNotIn('host_publication', restored.history_messages[3].additional_kwargs)

    def test_missing_covered_row_preserves_remaining_history(self):
        self.seed_legacy_snapshot()
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("DELETE FROM messages WHERE session_id=? AND msg_type='human'", (self.sid,))
            conn.commit()
        ctx = self.restore()
        self.assertEqual(ctx.history_load_reason, "raw_history_incomplete")
        self.assertEqual([m.content for m in ctx.history_messages], ["A" * 300])

    def test_corrupt_body_outside_checkpoint_excerpts_is_excluded(self):
        asyncio.run(self.manager.save_messages(self.sid, [AIMessage(
            content="final", additional_kwargs={"request_id": "req-2"})], idempotency_key="crid:req-2"))
        self.seed_legacy_snapshot()
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("UPDATE messages SET content=? WHERE id=?",
                         ("A" * 200 + "X" + "A" * 99, self.watermark))
            conn.commit()
        ctx = self.restore()
        self.assertEqual(ctx.history_load_reason, "source_integrity_failed")
        self.assertEqual([m.content for m in ctx.history_messages], ["What changed?", "final"])

    def test_different_checkpoint_owner_preserves_authorized_messages(self):
        checkpoint = self.seed_legacy_snapshot()
        checkpoint["owner_user_id"] = self.owner + 1
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("UPDATE sessions SET checkpoint_summary=? WHERE session_id=?",
                         (json.dumps(checkpoint), self.sid))
            conn.commit()
        ctx = self.restore()
        self.assertTrue(ctx.history_scope_authorized)
        self.assertEqual(ctx.history_load_reason, "owner_scope_mismatch")
        self.assertEqual([m.content for m in ctx.history_messages], ["What changed?", "A" * 300])

    def test_wrong_request_scope_performs_no_account_or_evidence_reads(self):
        memory = Mock()
        tracing.set_user_id(self.owner + 1)
        with patch("src.session.files.list_account_files") as files:
            ctx = self.restore(user_id=self.owner + 1, memory=memory)
            files.assert_not_called()
        with patch.object(self.manager, "build_evidence_block") as evidence:
            result = finalize_load_result(ctx, session_manager=self.manager, session_id=self.sid,
                                          budget=None, node_label="session_load", log_prefix="test")
            evidence.assert_not_called()
        self.assertFalse(memory.mock_calls)
        self.assertFalse(ctx.history_scope_authorized)
        self.assertEqual(result["history_load_reason"], "owner_scope_mismatch")
        self.assertEqual(result["history_load_status"], "failed")
        self.assertNotIn("messages", result)

    def test_reopened_legacy_tool_groups_keep_only_exactly_paired_rows(self):
        def batch(call_id):
            return [AIMessage(content="", tool_calls=[{"id": call_id, "name": "read_data", "args": {}}]),
                    ToolMessage(content=call_id, tool_call_id=call_id, name="read_data")]

        asyncio.run(self.manager.save_messages(self.sid, [*batch("intact"), *batch("broken"),
                                                           AIMessage(content="final")]))
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("UPDATE messages SET msg_type='AIMessageChunk' "
                         "WHERE session_id=? AND tool_calls_json LIKE '%intact%'", (self.sid,))
            conn.execute("UPDATE messages SET tool_call_id='wrong' "
                         "WHERE session_id=? AND tool_call_id='broken'", (self.sid,))
            conn.commit()
        ctx = self.restore()
        self.assertEqual(ctx.history_load_reason, "raw_history_incomplete")
        self.assertEqual(ctx.history_messages[2].tool_calls[0]["id"], "intact")
        self.assertEqual(ctx.history_messages[3].tool_call_id, "intact")
        self.assertEqual([m.content for m in ctx.history_messages],
                         ["What changed?", "A" * 300, "", "intact", "final"])
        self.assertNotIn("host_publication", ctx.history_messages[3].additional_kwargs)

    def test_default_turn_with_lease_does_not_read_or_write_navigation(self):
        from src.core import account_leases
        from src.core.capability_policy import CapabilityPolicy
        from src.graph.session_persistence import persist_agent_turn

        existing = self.seed_legacy_snapshot()
        checkpoint = self.manager.get_checkpoint(self.sid)
        lease = object()

        async def under_lease(actual, write):
            self.assertIs(actual, lease)
            return await write

        with (patch.object(manager, "session_manager", self.manager),
              patch.object(account_leases, "current_account_lease", return_value=lease),
              patch.object(account_leases, "run_account_async", side_effect=under_lease),
              patch.object(self.manager, "get_checkpoint") as read_legacy,
              patch.object(self.manager, "set_checkpoint") as write_legacy):
            result = asyncio.run(persist_agent_turn({
                "session_id": self.sid, "request_id": "req-default", "query": "New prompt",
                "answer": "New answer", "idempotency_key": "crid:req-default",
                "turn_trace": [HumanMessage("New prompt")], "turn_outcome": {"status": "completed"},
                "capability_snapshot": CapabilityPolicy().snapshot(),
            }))
            read_legacy.assert_not_called()
            write_legacy.assert_not_called()
        self.assertTrue(result["_trace"]["history_saved"])
        after = self.manager.get_checkpoint(self.sid)
        self.assertEqual(json.loads(after["summary"]), existing)
        self.assertEqual(after["revision"], checkpoint["revision"])
        self.assertEqual(after["checkpoint_watermark"], checkpoint["checkpoint_watermark"])
        self.assertGreater(after["message_watermark"], checkpoint["message_watermark"])
        self.assertEqual([m.content for m in self.restore().history_messages],
                         ["What changed?", "A" * 300, "New prompt", "New answer"])

    def test_new_persistence_never_generates_navigation_and_keeps_working_state(self):
        from src.core.capability_policy import CapabilityPolicy
        from src.graph.session_persistence import persist_agent_turn

        self.assertFalse(hasattr(turn_checkpoint, "schedule_checkpoint"))
        self.assertFalse(hasattr(turn_checkpoint, "build_checkpoint"))
        self.assertFalse(hasattr(ContextManager, "_get_compact_llm"))
        working = {"goal": "verify evidence", "verified_facts": ["check passed"]}
        policy = CapabilityPolicy()
        state = {"session_id": self.sid, "request_id": "req-new", "query": "New turn",
                 "answer": "Verified answer", "idempotency_key": "crid:req-new",
                 "turn_trace": [HumanMessage("New turn")], "context_working_state": working,
                 "turn_outcome": {"status": "completed"}, "capability_snapshot": policy.snapshot()}
        with patch.object(manager, "session_manager", self.manager), patch.object(
                self.manager, "set_checkpoint", side_effect=AssertionError("legacy writer used")):
            saved = asyncio.run(persist_agent_turn(state))
        self.assertTrue(saved["_trace"]["history_saved"])
        after = self.manager.get_checkpoint(self.sid)
        self.assertEqual((after["summary"], after["msg_id"], after["checkpoint_watermark"]), ("", 0, 0))
        self.assertEqual(after["working_state"], working)
        ordinary = self.restore(capability_policy=policy)
        self.assertEqual(ordinary.context_working_state, working)
        self.assertEqual([m.content for m in ordinary.history_messages[1:]],
                         ["What changed?", "A" * 300, "New turn", "Verified answer"])

    def test_isolated_turn_cannot_overwrite_default_working_state(self):
        from src.core.capability_policy import CapabilityPolicy
        from src.graph.session_persistence import persist_agent_turn

        def save(policy, key, value):
            with patch.object(manager, "session_manager", self.manager):
                return asyncio.run(persist_agent_turn({
                    "session_id": self.sid, "query": key, "answer": key + " answer",
                    "idempotency_key": "crid:" + key, "turn_trace": [HumanMessage(key)],
                    "context_working_state": {"goal": key, "source_refs": [{"source_id": value}]},
                    "capability_snapshot": policy.snapshot(),
                }))

        default = CapabilityPolicy()
        isolated = CapabilityPolicy(local_rag=False, experiment_group="isolated-fixture")
        self.assertTrue(save(default, "default", "default-ref")["_trace"]["history_saved"])
        before = self.manager.get_checkpoint(self.sid)["working_state"]
        self.assertTrue(save(isolated, "isolated", "isolated-ref")["_trace"]["history_saved"])
        self.assertEqual(self.manager.get_checkpoint(self.sid)["working_state"], before)
        default_context = self.restore(capability_policy=default)
        restricted_context = self.restore(capability_policy=isolated)
        self.assertEqual(default_context.context_working_state, before)
        self.assertEqual(restricted_context.context_working_state, {})
        self.assertEqual([m.content for m in restricted_context.history_messages], ["isolated", "isolated answer"])
        self.assertNotIn("isolated-ref", str(default_context.history_messages))
        self.assertNotIn("default-ref", str(restricted_context.history_messages))

    def test_scoped_working_state_does_not_cross_history_partition(self):
        from src.core.capability_policy import CapabilityPolicy

        policy = CapabilityPolicy(local_rag=False, experiment_group="isolated-fixture")
        working = {"goal": "scoped evidence", "capability_history": policy.history_key}
        with manager._connect_db(self.manager.db_path) as conn:
            conn.execute("UPDATE sessions SET context_working_state_json=? WHERE session_id=?",
                         (json.dumps(working), self.sid))
            conn.commit()
        isolated = self.restore(capability_policy=policy)
        self.assertEqual(isolated.context_working_state, working)
        self.assertEqual(len(isolated.history_messages), 1)
        ordinary = self.restore(capability_policy=CapabilityPolicy())
        self.assertEqual([m.content for m in ordinary.history_messages], ["What changed?", "A" * 300])
        self.assertEqual(ordinary.context_working_state, {})

    def test_tool_evidence_is_pinned_and_bound(self):
        from src.core import tool_results
        tool_results._schema_ready = False
        body = "FULL-ORIGINAL-" * 500
        asyncio.run(self.manager.save_messages(self.sid, [
            AIMessage(content="", additional_kwargs={"request_id": "req-tool"},
                      tool_calls=[{"id": "call-1", "name": "read_data", "args": {}}]),
            ToolMessage(content=body, tool_call_id="call-1", name="read_data",
                        additional_kwargs={"request_id": "req-tool"}),
            AIMessage(content="final", additional_kwargs={"request_id": "req-tool"}),
        ], idempotency_key="crid:req-tool"))
        self.assertTrue(tool_results.record(
            tool_call_id="call-1", session_id=self.sid, request_id="req-tool",
            tool_name="read_data", status="ok", result_text=body))
        self.assertTrue(tool_results.pin_references(self.sid, ["call-1"]))
        row = tool_results.fetch("call-1")
        doc = self.seed_legacy_snapshot(tool_hashes={"call-1": row["full_sha256"]})
        watermark = self.manager.get_checkpoint(self.sid)["message_watermark"]
        self.assertEqual(doc["tool_result_call_ids"], ["call-1"])
        with patch.object(tool_results, "pin_references", side_effect=AssertionError("readback repinned results")):
            self.assertTrue(validate_checkpoint(doc, self.manager, self.sid, watermark))
            ctx = self.restore()
        self.assertIn(body[:20], ctx.history_messages[3].content)
        self.assertNotIn("legacy navigation", str(ctx.history_messages))
        self.assertTrue(row["pinned"])
        self.assertEqual(row["full_sha256"],
                         hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(doc["tool_result_sha256"], {"call-1": row["full_sha256"]})
        with tool_results._connect() as conn:
            conn.execute("UPDATE tool_results SET pinned=0 WHERE tool_call_id='call-1'")
            conn.commit()
        self.assertEqual(self.restore().history_load_reason, "raw_history_incomplete")
        with tool_results._connect() as conn:
            conn.execute("UPDATE tool_results SET pinned=1, full_sha256=? WHERE tool_call_id='call-1'",
                         ("0" * 64,))
            conn.commit()
        self.assertEqual(self.restore().history_load_reason, "source_integrity_failed")
        # Losing complete evidence invalidates the checkpoint instead of hiding rows.
        with tool_results._connect() as conn:
            conn.execute("UPDATE tool_results SET retained=0 WHERE tool_call_id='call-1'")
            conn.commit()
        self.assertFalse(validate_checkpoint(doc, self.manager, self.sid, watermark))


if __name__ == "__main__":
    unittest.main()
