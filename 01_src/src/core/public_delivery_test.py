"""Public delivery, private snapshot, and first-packet regression contracts."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from src.core.public_delivery import (
    TurnTimingEvidence, project_citation_bubbles, project_public_event, public_answer_text,
    strip_private_text, timing_fields,
)


class PublicDeliveryTests(unittest.TestCase):
    def test_structured_spills_in_prose_and_encoded_objects(self):
        secret = "private-fixture-value"
        spill = {"nested": [json.dumps({"systemPrompt": secret})]}
        role = {"role": "developer", "content": secret}
        escaped_name = '{"reasoning\\u005fcontent":"' + secret + '"}'
        for leaked in (json.dumps(spill), json.dumps(json.dumps(spill)), json.dumps([role]),
                       escaped_name, f"```json\n{json.dumps(spill)}\n```",
                       repr({"messages": [role]}), json.dumps({"visibility": "private", "text": secret}),
                       json.dumps({"internal": True, "text": secret}),
                       json.dumps({"schema_version": 1, "answer": secret, "incidental_partials": []}),
                       json.dumps({"type": "constructor", "id": ["langchain", "SystemMessage"],
                                   "kwargs": {"content": secret}}),
                       f"<think>{secret}</think>", "{'reasoning_content': '" + secret + "'}"):
            with self.subTest(leaked=leaked):
                visible = public_answer_text(f"LC-MS evidence [L1].\n{leaked}\nSee Table 2.")
                self.assertIn("LC-MS evidence [L1].", visible)
                self.assertIn("See Table 2.", visible)
                self.assertNotIn(secret, visible)
        prose = 'We considered reasoning and draft guidance. Result `{"p_value": 0.02}` [L2].'
        self.assertEqual(strip_private_text(prose), prose)
        self.assertEqual(public_answer_text([{"type": "text", "text": "Final [W1]"},
            {"type": "reasoning", "text": secret}, {"type": "text", "visibility": "draft", "text": secret}]),
            "Final [W1]")

    def test_source_question_and_timing_projection_keep_only_known_fields(self):
        secret = "private-fixture-value"
        evidence = {"kind": "shared", "ref_id": "C2", "title": "Evidence table",
            "read_ref": {"document_id": "doc", "version_id": "v1", "index_generation": "g1",
                "start": 4, "end": 22, "md_sha256": "a" * 64, "reasoning": secret},
            "draft": secret}
        event = {"answer": "Result [C2].", "reasoning_content": {"text": secret},
            "question": {"id": "q", "job_id": "job", "prompt": "Which sheet?",
                "options": [{"id": "a", "label": "Table 2", "description": "public option",
                    "refs": [{"kind": "source_field", "column": "peak area", "sheet": "Results", "draft": secret}]}],
                "system_prompt": secret},
            "artifact_ids": ["a" * 32, {"content": secret}],
            "turn_envelope": {"history_saved": True, "verified_artifacts": 1,
                "acceptance_reason": {"content": secret}, "task_snapshot": {"completion": secret},
                "artifact_delivery": {"status": "available", "verified_file_ids": ["b" * 32], "reasoning": secret}},
            "turn_timings": {"queue_wait_ms": 0, "visible_answer_first_ms": 60, "extra": secret}, "seq": 12}
        projected = project_public_event("done", event)
        self.assertEqual(project_public_event("done", projected), projected)
        self.assertEqual(projected["answer"], "Result [C2].")
        self.assertEqual(projected["question"]["options"][0]["refs"][0]["column"], "peak area")
        self.assertEqual(projected["turn_envelope"]["artifact_delivery"]["verified_file_ids"], ["b" * 32])
        self.assertEqual(projected["turn_timings"]["queue_wait_ms"], 0)
        self.assertIsNone(projected["turn_timings"]["provider_first_packet_ms"])
        self.assertEqual(project_public_event("unknown", event), {"seq": 12})
        self.assertEqual(project_public_event("text", {"content": secret, "phase": "draft", "seq": 13}), {"seq": 13})
        self.assertNotIn(secret, json.dumps([projected, project_citation_bubbles([evidence])]))
        bubbles = project_citation_bubbles([evidence])
        self.assertEqual(bubbles[0]["read_ref"]["start"], 4)
        self.assertEqual(project_public_event("citations", {"display": "bubble_only", "bubbles": [evidence]}),
                         {"display": "bubble_only", "bubbles": bubbles})
        self.assertIsNone(timing_fields({"queue_wait_ms": float('inf')})["queue_wait_ms"])

    def test_visibility_never_credits_uncommitted_preview_or_controls(self):
        timing = TurnTimingEvidence(accepted_at=100, started_at=101)
        timing.attempt("main_loop", {"provider_first_packet_ms": 20}, started_at=101)
        timing.attempt("context_summary", {"provider_first_packet_ms": 5, "attempt_elapsed_ms": 200}, started_at=100)
        for event, data, ts in [
            ("answer_start", {}, 102), ("text", {"content": "legacy preview"}, 103),
            ("text_reset", {}, 104), ("answer_start", {}, 105),
            ("text", {"content": "candidate", "delivery": "draft"}, 106),
            ("text", {"content": " "}, 107), ("tool_result", {"elapsed_ms": 10}, 108),
            ("text", {"content": "answer", "delivery": "committed"}, 110),
        ]:
            timing.observe(event, data, created_at=ts)
        self.assertIsNone(timing.values()["visible_answer_first_ms"])
        timing.observe("done", {"answer": "answer"}, created_at=112)
        self.assertEqual(timing.values(), {"queue_wait_ms": 1000, "provider_first_packet_ms": 20,
            "visible_answer_first_ms": 10000, "tool_elapsed_ms": 10, "context_maintenance_ms": 200})
        fallback = TurnTimingEvidence(accepted_at=10)
        fallback.observe("answer_start", {}, created_at=10.2)
        fallback.observe("text", {"content": "legacy draft with no reset"}, created_at=10.5)
        fallback.observe("done", {"answer": "terminal answer"}, created_at=12)
        self.assertEqual(fallback.values()["visible_answer_first_ms"], 2000)
        fallback.observe("context_maintenance", {"status": "compacted", "elapsed_ms": 310}, created_at=15)
        self.assertEqual(fallback.values()["visible_answer_first_ms"], 2000)
        self.assertEqual(fallback.values()["context_maintenance_ms"], 310)

    def test_durable_replay_preserves_private_state_and_discards_legacy_preview(self):
        from src.core import task_events
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE task_jobs (job_id TEXT PRIMARY KEY, status TEXT, terminal_snapshot TEXT);
            INSERT INTO task_jobs VALUES ('job', 'running', '');
            CREATE TABLE chat_turn_queue (root_job_id TEXT, accepted_at REAL, started_at REAL);
            INSERT INTO chat_turn_queue VALUES ('job', 100, 101);
        """)
        task_events.init_schema(conn)
        conn.execute("INSERT INTO task_events VALUES (?,?,?,?,?)", ("job", 1, "text", json.dumps({"content": "old private preview"}), 102))
        conn.execute("INSERT INTO task_events VALUES (?,?,?,?,?)", ("job", 2, "text_reset", "{}", 103))
        task_events._append(conn, "job", "task_state", {"schema_version": 1, "system_prompt": "storage fixture"})
        self.assertIsNone(task_events._append(conn, "job", "text", {"content": "new draft", "delivery": "draft"}))
        task_events._append(conn, "job", "answer_start", {})
        with patch.object(task_events.time, "time", side_effect=[108, 109]):
            task_events._append(conn, "job", "text", {"content": "public final", "delivery": "committed"})
            task_events._append(conn, "job", "done", {"answer": "public final", "unknown": "hidden fixture"})
        task_events._append(conn, "job", "context_maintenance", {
            "status": "compacted", "elapsed_ms": 55, "draft": "hidden fixture"})

        with patch.object(task_events, "public_job_terminal", return_value={}):
            page = task_events.replay("job", limit=1, conn=conn)
            self.assertEqual(page["events"][0]["data"], {"seq": 1})
            exported = task_events.replay("job", conn=conn)
        self.assertIn("storage fixture", conn.execute("SELECT data FROM task_events WHERE event='task_state'").fetchone()[0])
        public = json.dumps(exported)
        self.assertNotIn("storage fixture", public)
        self.assertNotIn("hidden fixture", public)
        self.assertIn("public final", public)
        self.assertEqual(exported["events"][-2]["data"]["turn_timings"]["visible_answer_first_ms"], 8000)
        self.assertEqual(exported["events"][-1]["data"], {
            "seq": exported["events"][-1]["seq"], "status": "compacted", "elapsed_ms": 55})
        conn.close()

    def test_native_first_packet_is_recorded_before_preview_and_never_exposes_reasoning(self):
        from langchain_core.messages import AIMessage, AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk
        from src.agent.model import _StreamEvidence
        from src.core.request_context import _observation

        observed, scope = [], {}
        saved = _observation.set(scope)
        try:
            evidence = _StreamEvidence(lambda piece: observed.append((scope["provider_first_packet_ms"], piece)))
            evidence.on_llm_new_token("", chunk=ChatGenerationChunk(message=AIMessageChunk(
                content="", additional_kwargs={"reasoning_content": "private fixture"})))
            packet = scope["provider_first_packet_ms"]
            self.assertEqual(observed, [])
            evidence.on_llm_new_token("preview text", chunk=ChatGenerationChunk(message=AIMessageChunk(
                content="preview text", response_metadata={"finish_reason": "stop"})))
            response = evidence.check(AIMessage(content="preview text", additional_kwargs={
                "reasoning_content": "private fixture"}), allow_tool_stop=False, output_limit=None)
            self.assertEqual(observed, [(packet, "preview text")])
            self.assertEqual(response.content, "preview text")
            self.assertNotIn("reasoning_content", response.additional_kwargs)
            self.assertEqual(project_public_event("text", {"content": response.content, "phase": "draft"}), {})
        finally:
            _observation.reset(saved)

    def test_exporter_retains_receipt_and_user_prose_not_nested_secret(self):
        path = Path(__file__).resolve().parents[3] / "04_scripts" / "export_8043_turns.py"
        spec = importlib.util.spec_from_file_location("export_8043_turns", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sample = {"label": "LC-MS sample", "systemPrompt": "private fixture",
                  "arguments": {"method": "peak area", "nested": [
                      json.dumps({"role": "system", "content": "private fixture"}),
                      {"draft": "private fixture", "value": "normal evidence"}]},
                  "authorization": "Bearer protected-credential"}
        safe = module.render(sample)
        for visible in ("LC-MS sample", "peak area", "normal evidence", "[REDACTED]"):
            self.assertIn(visible, safe)
        for hidden in ("private fixture", "protected-credential", "systemPrompt"):
            self.assertNotIn(hidden, safe)


if __name__ == "__main__":
    unittest.main()
