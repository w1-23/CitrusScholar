"""The working-context summary must be reusable by a later turn or a resume.

Archived production logs show the summary model being called again for the same
unchanged history prefix on every new turn, which blocks the first token for
tens of seconds. A persisted descriptor (coverage fingerprint, covered indices,
summary, and archive binding) lets the next turn adopt the same view instead.
"""
import time
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.agent.context_projection import WorkingContextProjection, _cache_expand, _cache_ranges
from src.agent.identity import ExecutionIdentity


class _Archive:
    provider_scope = ("https://api.example.test/v1", "fixture")
    identity = ExecutionIdentity(7, "session-1", "turn-1", "run-1")

    def identity_metadata(self, _kind):
        return {"context_origin": "internal_control"}


class _Model:
    max_retries = 0
    max_tokens = 4096

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):  # pragma: no cover - reuse must avoid this
        self.calls += 1
        return AIMessage(content="unused", response_metadata={"finish_reason": "stop"})


def _history():
    return [SystemMessage(content="control", id="sys"),
            AIMessage(content="", id="call", tool_calls=[{"id": "t1", "name": "read_data", "args": {}}]),
            ToolMessage(content="x" * 4000, id="res1", tool_call_id="t1", name="read_data"),
            HumanMessage(content="latest user request", id="tail")]


def _projector(model):
    return WorkingContextProjection(archive=_Archive(), summary_model=model, enabled=True,
                                    deadline=time.monotonic() + 30, trigger_tokens=128,
                                    target_tokens=100, readback_available=True)


def _descriptor(projector, raw):
    return {"schema": "working_summary_cache/2", "count": 3,
            "digest": projector._coverage_digest(raw, frozenset({1, 2})),
            "removed": [[1, 2]],
            "summary": {"type": "system", "data": {
                "content": "Historical working-context summary: fixture",
                "additional_kwargs": {"context_projection": "working_summary_v1"}}},
            "trigger_tokens": 128, "target_tokens": 100, "requirements": [],
            "provider_scope": list(projector.archive.provider_scope),
            "binding": projector._cache_binding(),
            "archive": {"thread_id": "t" * 64, "checkpoint_id": "cp-1", "message_count": 4,
                        "provider_scope": list(projector.archive.provider_scope),
                        "identity": {"owner_user_id": 7, "session_id": "session-1",
                                     "user_turn_id": "turn-1", "run_id": "run-1"}}}


class ProjectionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_summary_is_reused_without_another_model_call(self):
        raw = _history()
        model = _Model()
        first = _projector(model)
        self.assertIs(first.import_cache(_descriptor(first, raw)), True)
        self.assertTrue(first.imported)

        view = await first.project(raw)
        self.assertEqual(model.calls, 0)
        self.assertEqual(first.reuse_count, 1)
        self.assertIsInstance(view[1], SystemMessage)
        self.assertIn("Historical working-context summary", view[1].content)
        self.assertIs(view[-1], raw[-1])
        # Second projection of the same history is still a reuse, never a
        # second summary call.
        await first.project(raw)
        self.assertEqual(model.calls, 0)
        self.assertEqual(first.reuse_count, 2)

    async def test_descriptor_is_round_trip_stable(self):
        raw = _history()
        model = _Model()
        projector = _projector(model)
        projector.import_cache(_descriptor(projector, raw))
        exported = projector.export_cache()
        self.assertEqual(exported["count"], 3)
        self.assertEqual(exported["removed"], [[1, 2]])
        self.assertEqual(exported["binding"]["owner_user_id"], 7)
        self.assertEqual(exported["archive"]["checkpoint_id"], "cp-1")

        resumed = _projector(model)
        self.assertIs(resumed.import_cache(exported), True)
        view = await resumed.project(raw)
        self.assertEqual(model.calls, 0)
        self.assertIsInstance(view[1], SystemMessage)

    async def test_mismatched_descriptor_is_rejected(self):
        raw = _history()
        projector = _projector(_Model())
        good = _descriptor(projector, raw)
        for mutate in ({"trigger_tokens": 999}, {"target_tokens": 1},
                       {"provider_scope": ["https://other.test/v1", "fixture"]},
                       {"count": 0}, {"digest": "short"}, {"removed": [[5, 9]]},
                       {"removed": []}, {"summary": {"type": "human", "data": {"content": "x"}}},
                       {"schema": "other/1"}, {"binding": None}):
            candidate = dict(good)
            candidate.update(mutate)
            self.assertIs(projector.import_cache(candidate), False, mutate)
        for binding in ({"owner_user_id": 8, "session_id": "session-1", "context_scope": "0" * 64},
                        {**good["binding"], "session_id": "session-2"},
                        {**good["binding"], "context_scope": "1" * 64}):
            candidate = dict(good)
            candidate["binding"] = binding
            self.assertIs(projector.import_cache(candidate), False, binding)
        candidate = dict(good)
        candidate["archive"] = {**good["archive"], "identity": {
            **good["archive"]["identity"], "session_id": "session-2"}}
        self.assertIs(projector.import_cache(candidate), False)

    async def test_work_state_inserted_after_coverage_keeps_reuse(self):
        raw = _history()
        model = _Model()
        projector = _projector(model)
        self.assertIs(projector.import_cache(_descriptor(projector, raw)), True)
        # A host work-state message inserted after the summarized block must not
        # invalidate the summary or force another model call.
        inserted = raw[:3] + [SystemMessage(content="work state", id="ws")] + raw[3:]
        view = await projector.project(inserted)
        self.assertEqual(model.calls, 0)
        self.assertIsInstance(view[1], SystemMessage)
        self.assertIn("Historical working-context summary", view[1].content)
        self.assertIs(view[-1], raw[-1])
        # Changing a summarized original invalidates it.
        changed = list(raw)
        changed[2] = ToolMessage(content="different", id="res1", tool_call_id="t1", name="read_data")
        self.assertNotEqual(projector._coverage_digest(changed, frozenset({1, 2})),
                            _descriptor(projector, raw)["digest"])

    def test_internal_cache_never_reaches_the_model_prompt(self):
        from src.core.context_manager import prompt_working_state
        working = {"goal": "g", "dataset_refs": [{"file_id": "f"}],
                   "projection_cache": {"schema": "working_summary_cache/2", "digest": "0" * 64}}
        visible = prompt_working_state(working)
        self.assertNotIn("projection_cache", visible)
        self.assertEqual(visible["goal"], "g")
        self.assertEqual(prompt_working_state(None), {})

    def test_removed_ranges_are_compact_and_lossless(self):
        removed = {1, 2, 3, 7, 9, 10}
        ranges = _cache_ranges(removed)
        self.assertEqual(ranges, [[1, 3], [7, 7], [9, 10]])
        self.assertEqual(_cache_expand(ranges, limit=11), frozenset(removed))
        self.assertIsNone(_cache_expand([[0, 11]], limit=11))
        self.assertIsNone(_cache_expand([[2, 1]], limit=11))
