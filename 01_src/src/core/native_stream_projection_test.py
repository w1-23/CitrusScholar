"""F-14: native LangGraph stream projection onto the durable public protocol.

The projection must be the *only* place where native streaming facts become
public events: bounded, versioned, identity preserving, restart/replay safe and
never a second Agent Loop. ``task_events`` keeps owning the durable log, the
recovery cursor and the audit trail.
"""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from src.api import main
from src.core import jobs, progress_bus, task_events
from src.core.public_progress import (NATIVE_STREAM_MODES, NATIVE_STREAM_SCHEMA_VERSION,
    NATIVE_STREAM_SOURCE, NativeStreamProjector, _SubagentProgress,
    invoke_with_public_progress, project_native_progress, project_native_stream_event,
    project_subagent_progress, native_stream_note_id, subagent_stream_id,
    unpack_stream_chunk, write_subagent_progress)
from src.session import manager

JOB = "job-native-stream"
RUN = "run-native-stream"
_FORBIDDEN_KEYS = {"checkpoint", "checkpoint_ns", "channel", "channels", "thread_id",
                   "config", "metadata", "ns", "namespace", "state", "values"}


def _frame(kind, **extra):
    status = {"model_preview": "streaming", "tool_proposed": "proposed",
              "subagent_started": "started", "subagent_completed": "completed",
              "subagent_failed": "failed", "interrupt": "requested",
              "resume": "resumed"}[kind]
    return {"origin": "langgraph", "source": NATIVE_STREAM_SOURCE, "visibility": "public",
            "schema_version": NATIVE_STREAM_SCHEMA_VERSION, "job_id": JOB, "run_id": RUN,
            "status": status, "kind": kind, "at_ms": 1_700_000_000_000,
            "note_id": native_stream_note_id(JOB, RUN, kind, "x"), **extra}


def _chunk(content, *, call="lc_run--native-1", tool_chunks=(), reasoning=""):
    message = AIMessageChunk(content=content, id=call,
                             tool_call_chunks=list(tool_chunks),
                             additional_kwargs={"reasoning_content": reasoning} if reasoning else {})
    return (message, {"langgraph_node": "supervisor", "langgraph_step": 1,
                      "langgraph_checkpoint_ns": "supervisor:private-checkpoint",
                      "checkpoint_ns": "supervisor:private-checkpoint"})


def _tool_message(*calls):
    return AIMessage(content="", id="lc_run--tools", tool_calls=[
        {"name": name, "id": call_id, "args": {"private": "argument never published"}}
        for name, call_id in calls])


def _projector(**kwargs):
    clock = {"now": 1_700_000_000.0}

    def now():
        clock["now"] += 0.6
        return clock["now"]

    options = {"job_id": JOB, "run_id": RUN, "now": now}
    options.update(kwargs)
    return NativeStreamProjector(**options)


class NativeStreamSchemaTests(unittest.TestCase):
    def test_every_kind_has_one_stable_versioned_shape(self):
        cases = {
            "model_preview": {"call_id": "mdl:" + "a" * 16, "delta_index": 0, "text": "第一行结论\n"},
            "tool_proposed": {"tool_call_id": "call_1", "tool_name": "read_tool_result"},
            "subagent_started": {"subagent_id": "sa:" + "b" * 16, "subagent_depth": 1},
            "subagent_completed": {"subagent_id": "sa:" + "b" * 16, "reason_code": "completed"},
            "subagent_failed": {"subagent_id": "sa:" + "b" * 16, "reason_code": "failed"},
            "interrupt": {"interrupt_id": "approval_1"},
            "resume": {"reason_code": "recovery"},
        }
        self.assertEqual(set(cases), {
            "model_preview", "tool_proposed", "subagent_started", "subagent_completed",
            "subagent_failed", "interrupt", "resume"})
        for kind, extra in cases.items():
            with self.subTest(kind=kind):
                payload = _frame(kind, **extra)
                self.assertEqual(project_native_stream_event(payload), payload)
                self.assertEqual(project_native_progress(payload), {} if kind == "model_preview" else payload)
                self.assertFalse(set(payload) & _FORBIDDEN_KEYS)
                self.assertEqual(payload["schema_version"], 1)

    def test_unknown_kind_or_version_is_ignored_not_crashed(self):
        unknown = _frame("model_preview", call_id="mdl:" + "a" * 16, delta_index=0, text="x\n")
        unknown["kind"] = "future_kind"
        self.assertEqual(project_native_progress(unknown), {})
        for version in (0, 2, 99, "1", True, None):
            with self.subTest(version=version):
                payload = _frame("tool_proposed", tool_call_id="call_1", tool_name="tool")
                payload["schema_version"] = version
                self.assertEqual(project_native_stream_event(payload), {})

    def test_internal_stream_internals_never_survive_projection(self):
        base = {"tool_call_id": "call_1", "tool_name": "read_tool_result"}
        for leaked in ({"checkpoint_ns": "supervisor:abc"}, {"channel": "messages"},
                       {"ns": ["task:1"]}, {"metadata": {"langgraph_node": "supervisor"}},
                       {"values": {"messages": []}}, {"status": "started"}):
            with self.subTest(leaked=leaked):
                self.assertEqual(project_native_stream_event(_frame("tool_proposed", **base, **leaked)), {})
        # A real namespace string can never masquerade as a public sub-agent id.
        self.assertEqual(project_native_stream_event(_frame("subagent_started",
            subagent_id="task:6f2f1b7c", subagent_depth=1)), {})
        self.assertEqual(project_native_stream_event(_frame("subagent_started",
            subagent_id="sa:" + "b" * 16, subagent_depth=9)), {})

    def test_preview_text_is_never_private_structure(self):
        def preview(text):
            return project_native_stream_event(_frame("model_preview",
                call_id="mdl:" + "a" * 16, delta_index=0, text=text))
        self.assertTrue(preview("结果已核对。\n"))
        for private in ('{"api_key": "sk-secret-value"}\n', '<analysis>private</analysis>\n',
                        "系统提示词：不得外泄\n", "Bearer abcdefghijklmnop\n", "x" * 401, "   \n"):
            with self.subTest(private=private):
                self.assertEqual(preview(private), {})

    def test_existing_todo_projection_is_unchanged(self):
        todo = {"origin": "langgraph", "source": "write_todos", "visibility": "public",
                "job_id": JOB, "note_id": "todo:" + "f" * 64, "status": "in_progress",
                "text": "核对实验方法"}
        self.assertEqual(project_native_progress(todo), todo)
        self.assertEqual(project_native_progress({**todo, "text": "Bearer token abc"}), {})


class UnpackStreamChunkTests(unittest.TestCase):
    def test_every_supported_chunk_shape_is_normalized(self):
        payload = {"supervisor": {"answer": "x"}}
        self.assertEqual(unpack_stream_chunk(("updates", payload)), ((), "updates", payload))
        self.assertEqual(unpack_stream_chunk((("task:1",), "updates", payload)),
                         (("task:1",), "updates", payload))
        self.assertEqual(unpack_stream_chunk({"type": "messages", "ns": [], "data": ("m", {})}),
                         ((), "messages", ("m", {})))
        self.assertEqual(unpack_stream_chunk(payload), ((), "updates", payload))


class NativeStreamProjectorTests(unittest.TestCase):
    def test_only_settled_public_lines_become_previews(self):
        projector = _projector(min_chars=1)
        frames = projector.feed("messages", _chunk("第一段结论\n", call="lc_run--a"))
        self.assertEqual([frame["kind"] for frame in frames], ["model_preview"])
        self.assertEqual(frames[0]["text"], "第一段结论")
        self.assertEqual(frames[0]["delta_index"], 0)
        # An unfinished line is held back, never published as a torn fragment.
        self.assertEqual(projector.feed("messages", _chunk("未完成", call="lc_run--a")), [])
        self.assertEqual([frame["text"] for frame in projector.flush()], ["未完成"])

    def test_tool_arguments_and_provider_reasoning_are_never_published(self):
        projector = _projector(min_chars=1)
        args = _chunk("", call="lc_run--a", tool_chunks=[{"name": "write_file", "args": '{"p": "x"}',
                                                         "index": 0, "id": "call_1"}])
        self.assertEqual(projector.feed("messages", args), [])
        self.assertEqual(projector.feed("messages", _chunk("内部推理\n", call="lc_run--a",
                                                          reasoning="private thought")), [])

    def test_a_new_model_call_settles_the_previous_one_first(self):
        projector = _projector(min_chars=1000, min_interval=10.0)
        self.assertEqual(projector.feed("messages", _chunk("旧调用尾部", call="lc_run--a")), [])
        frames = projector.feed("messages", _chunk("新调用开始\n", call="lc_run--b"))
        self.assertEqual([frame["kind"] for frame in frames], ["model_preview", "model_preview"])
        self.assertEqual(frames[0]["text"], "旧调用尾部")
        self.assertEqual(frames[1]["text"], "新调用开始")
        self.assertNotEqual(frames[0]["call_id"], frames[1]["call_id"])

    def test_preview_budget_is_bounded_per_turn(self):
        projector = _projector(max_events=3, max_chars=10_000, min_chars=1, min_interval=0.0)
        produced = []
        for index in range(20):
            produced.extend(projector.feed("messages", _chunk(f"第{index}行\n", call="lc_run--a")))
        self.assertEqual(len(produced), 3)
        self.assertTrue(all(frame["kind"] == "model_preview" for frame in produced))
        # Facts other than previews keep flowing when the preview budget is spent.
        self.assertEqual([frame["kind"] for frame in projector.feed("updates", {
            "supervisor": {"messages": [_tool_message(("read_tool_result", "call_9"))]}})],
            ["tool_proposed"])

    def test_native_tool_batch_is_proposed_once_without_arguments(self):
        projector = _projector()
        output = {"supervisor": {"messages": [_tool_message(("read_tool_result", "call_1"),
                                                           ("execute", "call_2"))]}}
        frames = projector.feed("updates", output)
        self.assertEqual([(frame["kind"], frame["tool_name"], frame["tool_call_id"]) for frame in frames],
                         [("tool_proposed", "read_tool_result", "call_1"),
                          ("tool_proposed", "execute", "call_2")])
        self.assertEqual(projector.feed("updates", output), [])  # Re-observed batch is a fact once.
        self.assertNotIn("private", json.dumps(frames))

    def test_history_updates_cannot_claim_current_tool_proposals(self):
        projector = _projector(model_preview=False)
        call = _tool_message(("read_tool_result", "reused_call"))
        prior = call.model_copy(update={"additional_kwargs": {"run_id": "earlier-run"}})
        self.assertEqual(projector.feed("updates", {"session_load": {"messages": [call]},
            "session_save": {"messages": [call]}, "supervisor": {"messages": [prior]}}), [])
        self.assertEqual(projector.feed("messages", _chunk("uncommitted draft fixture\n", call="lc_run--a")), [])
        [frame] = projector.feed("updates", {"supervisor": {"messages": [call]}})
        self.assertEqual(frame["tool_call_id"], "reused_call")
        self.assertEqual(projector.close(), [])

    def test_interrupt_reports_identity_without_checkpoint_payload(self):
        from langgraph.types import Interrupt

        projector = _projector()
        value = Interrupt(value={"prompt": "private approval payload"},
                          id="a1b2c3d4e5f60718293a4b5c6d7e8f90")
        [frame] = projector.feed("updates", {"__interrupt__": (value,)})
        self.assertEqual((frame["kind"], frame["status"]), ("interrupt", "requested"))
        self.assertEqual(frame["interrupt_id"], value.id)
        self.assertNotIn("private approval payload", json.dumps(frame))
        self.assertEqual(projector.feed("updates", {"__interrupt__": (value,)}), [])
        [anonymous] = NativeStreamProjector(job_id=JOB, run_id=RUN).feed(
            "updates", {"__interrupt__": ({"value": {"private": "x"}},)})
        self.assertNotIn("interrupt_id", anonymous)

    def test_a_namespace_never_becomes_a_subagent(self):
        """SDK ``tools:<uuid>`` namespaces are not sub-agent identities."""
        projector = _projector()
        namespace = ("tools:6f2f1b7c-9d0e-4a51-8c6c-1b2c3d4e5f60",)
        self.assertEqual(projector.feed("updates", {"tools": {"messages": []}}, namespace), [])
        self.assertEqual(projector.feed("messages", _chunk("子代理草稿\n"), namespace), [])
        self.assertEqual(projector.feed("messages", _chunk("子代理提案", tool_chunks=[
            {"index": 0, "id": "call_nested", "name": "read_file", "args": "{}"}]),
            namespace), [])
        self.assertEqual(projector.flush(), [])

    def test_a_task_tool_result_is_never_a_subagent_outcome(self):
        """The tool also returns plain strings; only an explicit fact may settle."""
        projector = _projector(min_chars=1)
        result = ToolMessage(content="We cannot invoke subagent ...", tool_call_id="call_task_1",
                             name="task", status="success")
        self.assertEqual(projector.feed("messages", (result, {})), [])
        self.assertEqual(projector.feed("updates", {"supervisor": {"messages": [result]}}), [])
        self.assertEqual(projector.flush(), [])
        self.assertNotIn("cannot invoke", json.dumps(projector.feed("messages", (result, {}))))

    def test_only_streamed_chunks_may_become_answer_text(self):
        """Tool output and whole AI messages are never provisional answer prose."""
        projector = _projector(min_chars=1)
        tool = ToolMessage(content="子代理报告正文第一段\n", tool_call_id="call_1", name="task")
        self.assertEqual(projector.feed("messages", (tool, {})), [])
        whole = AIMessage(content="整段答复正文\n", id="lc_run--z")
        self.assertEqual(projector.feed("messages", (whole, {})), [])
        self.assertEqual(projector.flush(), [])
        # A streaming chunk of the same call still previews normally.
        frames = projector.feed("messages", _chunk("流式片段\n", call="lc_run--stream"))
        self.assertEqual([frame["kind"] for frame in frames], ["model_preview"])
        self.assertEqual(frames[0]["text"], "流式片段")

    def test_streamed_tool_proposal_is_published_once_without_arguments(self):
        projector = _projector()
        fragments = [{"index": 0, "id": "call_1", "name": "read_tool_result",
                      "args": '{"private": "argument never published"}'}]
        self.assertEqual(projector.feed("messages", _chunk("", tool_chunks=fragments)), [])
        [frame] = projector.flush()
        self.assertEqual((frame["kind"], frame["tool_call_id"], frame["tool_name"]),
                         ("tool_proposed", "call_1", "read_tool_result"))
        self.assertNotIn("argument never published", json.dumps(frame))
        self.assertEqual(projector.flush(), [])

    def test_tool_round_narration_is_not_answer_text(self):
        projector = _projector(min_chars=1)
        message = AIMessage(content="我先读取记录。\n", id="lc_run--b", tool_calls=[
            {"name": "read_tool_result", "id": "call_1", "args": {}}])
        self.assertEqual(projector.feed("messages", (message, {})), [])

    def test_aggregate_message_of_a_streamed_call_is_not_republished(self):
        projector = _projector(min_chars=1)
        projector.feed("messages", _chunk("结论", call="lc_run--a"))
        self.assertEqual(projector.feed("messages", (AIMessage(content="结论", id="lc_run--a"), {})), [])

    def test_resume_reports_once_and_close_stops_production(self):
        projector = _projector()
        [frame] = projector.resume("recovery")
        self.assertEqual((frame["kind"], frame["reason_code"]), ("resume", "recovery"))
        self.assertEqual(projector.resume("recovery"), [])
        closed = projector.close()
        self.assertEqual(closed, [])
        self.assertTrue(projector.closed)
        self.assertEqual(projector.feed("updates", {"supervisor": {"messages": [
            _tool_message(("read_tool_result", "call_1"))]}}), [])
        self.assertEqual(projector.resume("answer"), [])

    def test_reobserved_native_input_is_byte_identical(self):
        def run():
            projector = _projector(min_chars=1)
            frames = projector.feed("messages", _chunk("同一段结论\n"))
            frames += projector.feed("updates", {"supervisor": {"messages": [
                _tool_message(("read_tool_result", "call_1"))]}})
            frames += projector.feed("updates", {"__interrupt__": (
                {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},)})
            frames += projector.flush()
            return frames

        first, second = run(), run()
        self.assertEqual([frame["note_id"] for frame in first],
                         [frame["note_id"] for frame in second])
        self.assertEqual(len({frame["note_id"] for frame in first}), len(first))


class ExplicitSubagentStreamTests(unittest.TestCase):
    """Only the native ``task`` handler may publish a sub-agent lifecycle fact."""

    def _marker(self, status, **overrides):
        fields = {"job_id": JOB, "subagent_id": subagent_stream_id("call_task_1"),
                  "status": status, "reason_code": "", "depth": 1}
        fields.update(overrides)
        return _SubagentProgress(**fields)

    def test_started_and_completed_are_exact_versioned_frames(self):
        started = project_subagent_progress(self._marker("started"), job_id=JOB)
        self.assertEqual(len(started), 1)
        frame = started[0]
        self.assertEqual((frame["kind"], frame["status"], frame["schema_version"]),
                         ("subagent_started", "started", NATIVE_STREAM_SCHEMA_VERSION))
        self.assertEqual(frame["subagent_id"], subagent_stream_id("call_task_1"))
        self.assertEqual(frame["subagent_depth"], 1)
        self.assertFalse(set(frame) & {"checkpoint_ns", "channel", "ns", "tool_call_id"})
        completed = project_subagent_progress(self._marker("completed"), job_id=JOB)[0]
        self.assertEqual((completed["kind"], completed["status"], completed["reason_code"]),
                         ("subagent_completed", "completed", "completed"))
        failed = project_subagent_progress(self._marker("failed", reason_code="failed"), job_id=JOB)[0]
        self.assertEqual((failed["kind"], failed["reason_code"]), ("subagent_failed", "failed"))

    def test_unusable_facts_are_refused_not_repaired(self):
        for overrides in ({"status": "proposed"}, {"status": "cancelled"}, {"status": ""},
                          {"job_id": "other-job"}, {"subagent_id": ""}):
            with self.subTest(overrides=overrides):
                fields = {"job_id": JOB, "subagent_id": subagent_stream_id("call_task_1"),
                          "status": "started", "reason_code": "", "depth": 1}
                fields.update(overrides)
                self.assertEqual(project_subagent_progress(_SubagentProgress(**fields), job_id=JOB), [])
        self.assertEqual(project_subagent_progress({"status": "started"}, job_id=JOB), [])
        # Any caller key that is not already public is hashed, never published:
        # a namespace-looking key cannot leak through this channel.
        hashed = project_subagent_progress(self._marker(
            "started", subagent_id="task:6f2f1b7c"), job_id=JOB)[0]
        self.assertRegex(hashed["subagent_id"], r"^sa:[a-f0-9]{16}$")
        self.assertNotIn("6f2f1b7c", json.dumps(hashed))
        # An out-of-range depth is clamped into the published contract range.
        deep = project_subagent_progress(self._marker("started", depth=99), job_id=JOB)[0]
        self.assertEqual(deep["subagent_depth"], 8)
        # Unknown reason codes fall back to the status word, never to raw text.
        odd = project_subagent_progress(self._marker("failed", reason_code="secret detail"),
                                        job_id=JOB)[0]
        self.assertEqual(odd["reason_code"], "failed")

    def test_identity_is_stable_and_opaque(self):
        first = subagent_stream_id("call_task_1")
        self.assertEqual(first, subagent_stream_id("call_task_1"))
        self.assertNotEqual(first, subagent_stream_id("call_task_2"))
        self.assertTrue(first.startswith("sa:"))
        self.assertNotIn("call_task_1", first)
        explicit = subagent_stream_id("sa:" + "b" * 16)
        self.assertEqual(explicit, "sa:" + "b" * 16)
        self.assertEqual(subagent_stream_id(""), "")

    def test_the_entry_uses_the_custom_stream_when_inside_a_run(self):
        written = []
        with patch("langgraph.config.get_stream_writer", return_value=written.append):
            self.assertTrue(write_subagent_progress(JOB, tool_call_id="call_task_1", status="started"))
            self.assertTrue(write_subagent_progress(JOB, subagent_id=subagent_stream_id("call_task_1"),
                                                    status="completed", reason_code="completed"))
        self.assertEqual([type(item) for item in written], [_SubagentProgress, _SubagentProgress])
        self.assertEqual([item.status for item in written], ["started", "completed"])
        self.assertEqual(written[0].subagent_id, subagent_stream_id("call_task_1"))

    def test_the_entry_falls_back_to_the_request_queue_outside_a_run(self):
        emitted = []
        with patch("langgraph.config.get_stream_writer",
                   side_effect=RuntimeError("Called get_config outside of a runnable context")), \
                patch.object(progress_bus, "emit_progress", side_effect=lambda kind, data: emitted.append((kind, data))):
            self.assertTrue(write_subagent_progress(JOB, tool_call_id="call_task_1", status="started"))
        self.assertEqual([(kind, data["kind"]) for kind, data in emitted],
                         [("assistant_progress", "subagent_started")])

    def test_the_entry_never_raises_on_bad_input_or_a_broken_writer(self):
        self.assertFalse(write_subagent_progress("", tool_call_id="call_task_1", status="started"))
        self.assertFalse(write_subagent_progress(JOB, tool_call_id="", subagent_id="", status="started"))
        self.assertFalse(write_subagent_progress(JOB, tool_call_id="call_task_1", status="proposed"))
        with patch("langgraph.config.get_stream_writer", side_effect=ValueError("broken")), \
                patch.object(progress_bus, "emit_progress") as emit:
            self.assertFalse(write_subagent_progress(JOB, tool_call_id="call_task_1", status="started"))
        emit.assert_not_called()

    def test_the_public_consumer_projects_nested_custom_facts(self):
        marker = self._marker("started")
        foreign = self._marker("completed", job_id="other-job")
        parts = [
            {"type": "values", "data": {"messages": []}, "ns": (), "interrupts": []},
            {"type": "custom", "data": marker, "ns": ()},
            # A nested writer still reports this run.
            {"type": "custom", "data": self._marker("completed"), "ns": ("tools:1",)},
            {"type": "custom", "data": foreign, "ns": ()},
            {"type": "values", "data": {"messages": []}, "ns": (), "interrupts": []},
        ]

        class FakeGraph:
            output_channels = ["messages"]

            async def astream(self, initial, config, **kwargs):
                self.kwargs = kwargs
                for part in parts:
                    yield part

        graph, emitted = FakeGraph(), []

        async def run():
            with patch.object(progress_bus, "emit_progress",
                              side_effect=lambda kind, data: emitted.append((kind, data))):
                return await invoke_with_public_progress(graph, {"messages": []}, {}, context=None,
                                                        job_id=JOB)

        result = asyncio.run(run())
        self.assertEqual([data["kind"] for _, data in emitted],
                         ["subagent_started", "subagent_completed"])
        self.assertEqual(emitted[0][0], "assistant_progress")
        self.assertEqual(set(graph.kwargs["stream_mode"]), {"values", "custom"})
        self.assertEqual(result.value, {"messages": []})

    def test_a_real_graph_run_carries_the_fact_through_the_custom_stream(self):
        """End-to-end: a handler inside a real graph run publishes one fact pair."""
        from typing import Annotated, TypedDict

        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.graph.message import add_messages

        class State(TypedDict):
            messages: Annotated[list, add_messages]

        def handler(state):
            self.assertTrue(write_subagent_progress(JOB, tool_call_id="call_task_1",
                                                    status="started"))
            self.assertTrue(write_subagent_progress(JOB, tool_call_id="call_task_1",
                                                    status="completed", reason_code="completed"))
            return {"messages": []}

        builder = StateGraph(State)
        builder.add_node("task_handler", handler)
        builder.add_edge(START, "task_handler")
        builder.add_edge("task_handler", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        emitted = []

        async def run():
            with patch.object(progress_bus, "emit_progress",
                              side_effect=lambda kind, data: emitted.append((kind, data))):
                return await invoke_with_public_progress(graph, {"messages": []},
                    {"configurable": {"thread_id": "native-stream-thread"}}, context=None,
                    job_id=JOB)

        asyncio.run(run())
        self.assertEqual([(kind, data["kind"]) for kind, data in emitted],
                         [("assistant_progress", "subagent_started"),
                          ("assistant_progress", "subagent_completed")])
        self.assertEqual(emitted[0][1]["subagent_id"], emitted[1][1]["subagent_id"])
        self.assertEqual(emitted[1][1]["reason_code"], "completed")

    def test_duplicate_facts_share_one_durable_note_id(self):
        first = project_subagent_progress(self._marker("started"), job_id=JOB)[0]
        second = project_subagent_progress(self._marker("started"), job_id=JOB)[0]
        self.assertEqual(first["note_id"], second["note_id"])
        self.assertEqual(len({project_subagent_progress(self._marker(status), job_id=JOB)[0]["note_id"]
                              for status in ("started", "completed", "failed")}), 3)


class NativeStreamDurabilityTests(unittest.TestCase):
    """The durable log, cursor replay and terminal fencing stay authoritative."""

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(jobs, '_DB_PATH', root / 'jobs.db'))
        self.enterContext(patch.object(manager, 'STATE_DIR', root))
        self.enterContext(patch.object(manager, 'DB_PATH', root / 'history.db'))
        self.enterContext(patch.object(manager.SessionManager, '_instance', None))
        self.sessions = manager.SessionManager()
        self.enterContext(patch.object(main, 'session_manager', self.sessions))
        self.owner = 111
        self.enterContext(patch.object(main, 'current_user', side_effect=lambda _: {'user_id': self.owner}))
        self.sid = 'native-stream-session'
        asyncio.run(self.sessions.get_or_create_session(self.sid, owner_user_id=self.owner))
        self.claim = jobs.claim_job(self.sid, 'native-stream-request', owner_user_id=self.owner,
                                    client_request_id='native-stream-client', payload={})
        app = FastAPI()
        app.add_api_route('/jobs/{job_id}', main.get_job_status)
        app.add_api_route('/jobs/{job_id}/events', main.get_job_events)
        self.client = self.enterContext(TestClient(app))
        self.projector = NativeStreamProjector(job_id=self.claim.job_id,
                                               run_id=self.claim.job_id)

    def _feed(self, queue, mode, data, namespace=()):
        """Emit exactly as the API layer does: project, then durable enqueue."""
        frames = self.projector.feed(mode, data, namespace)
        for frame in frames:
            progress_bus.emit_progress('assistant_progress', frame)
        return frames

    def _stored(self):
        with task_events.read_transaction() as conn:
            rows = conn.execute("SELECT seq,event,data FROM task_events WHERE job_id=? ORDER BY seq",
                                (self.claim.job_id,)).fetchall()
        return [(row['seq'], row['event'], json.loads(row['data'])) for row in rows]

    def test_explicit_subagent_facts_are_durable_and_replay_by_cursor(self):
        queue = task_events.DurableEventQueue(self.claim.job_id)
        job_id = self.claim.job_id
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue), \
                patch("langgraph.config.get_stream_writer",
                      side_effect=RuntimeError("Called get_config outside of a runnable context")):
            async def scenario():
                self.assertTrue(write_subagent_progress(job_id, tool_call_id="call_task_1",
                                                        status="started"))
                self.assertTrue(write_subagent_progress(job_id, tool_call_id="call_task_1",
                                                        status="completed", reason_code="completed"))
                # Re-observation of the same facts must not add rows.
                write_subagent_progress(job_id, tool_call_id="call_task_1", status="started")
                write_subagent_progress(job_id, tool_call_id="call_task_1", status="completed",
                                        reason_code="completed")
                await asyncio.sleep(0)
            asyncio.run(scenario())

        rows = self._stored()
        self.assertEqual([row[2]['kind'] for row in rows],
                         ['subagent_started', 'subagent_completed'])
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(rows[0][2]['subagent_id'], rows[1][2]['subagent_id'])
        page = self.client.get(f'/jobs/{job_id}/events').json()
        self.assertEqual([row['data']['kind'] for row in page['events']],
                         ['subagent_started', 'subagent_completed'])
        tail = self.client.get(f'/jobs/{job_id}/events?after=1').json()
        self.assertEqual([row['data']['kind'] for row in tail['events']], ['subagent_completed'])
        self.assertEqual(tail['next_cursor'], 2)

    def test_projection_is_durable_before_notification_and_replays_by_cursor(self):
        queue = task_events.DurableEventQueue(self.claim.job_id)
        notified = []
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue), \
                patch.object(queue, '_notify', side_effect=lambda item: notified.append(item)):
            async def scenario():
                self._feed(queue, 'messages', (AIMessageChunk(content="第一段结论\n", id="lc_run--a"),
                                               {"langgraph_node": "supervisor"}))
                self._feed(queue, 'updates', {"supervisor": {"messages": [
                    _tool_message(("read_tool_result", "call_1"))]}})
                self._feed(queue, 'updates', {"__interrupt__": (
                    {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},)})
                await asyncio.sleep(0)
            asyncio.run(scenario())

        rows = self._stored()
        self.assertEqual([row[2]['kind'] for row in rows], ['tool_proposed', 'interrupt'])
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(len(notified), len(rows))
        # Durable first: every notified frame already exists in the log.
        self.assertEqual([json.loads(item['data']) for item in notified],
                         [row[2] for row in rows])
        page = self.client.get(f'/jobs/{self.claim.job_id}/events').json()
        self.assertEqual([row['seq'] for row in page['events']], [1, 2])
        self.assertEqual([row['data']['kind'] for row in page['events']], ['tool_proposed', 'interrupt'])
        for row in page['events']:
            self.assertFalse(set(row['data']) & _FORBIDDEN_KEYS)
        # A reconnect resumes at its own cursor: no duplicate, no replay of 1.
        tail = self.client.get(f'/jobs/{self.claim.job_id}/events?after=1').json()
        self.assertEqual([row['seq'] for row in tail['events']], [2])
        self.assertEqual(tail['next_cursor'], 2)
        self.assertFalse(tail['has_more'])

    def test_reconnecting_reobservation_invents_no_second_row(self):
        queue = task_events.DurableEventQueue(self.claim.job_id)
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue):
            async def scenario():
                for _ in range(2):
                    self._feed(queue, 'messages', (AIMessageChunk(content="结论\n", id="lc_run--a"),
                                                   {"langgraph_node": "supervisor"}))
                    self._feed(queue, 'updates', {"supervisor": {"messages": [
                        _tool_message(("read_tool_result", "call_1"))]}})
                await asyncio.sleep(0)
            asyncio.run(scenario())
        rows = self._stored()
        self.assertEqual([row[2]['kind'] for row in rows], ['tool_proposed'])
        self.assertTrue(queue.empty() or queue.qsize() <= len(rows))

    def test_terminal_frame_fences_late_native_events(self):
        queue = task_events.DurableEventQueue(self.claim.job_id)
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue):
            async def scenario():
                self._feed(queue, 'updates', {"supervisor": {"messages": [
                    _tool_message(("read_tool_result", "call_1"))]}})
                task_events.append_event(self.claim.job_id, 'done', {
                    'job_id': self.claim.job_id, 'answer': '已交付',
                    'turn_envelope': {'display_status': 'completed'}})
                self._feed(queue, 'updates', {"supervisor": {"messages": [
                    _tool_message(("execute", "call_2"))]}})
                await asyncio.sleep(0)
            asyncio.run(scenario())
        rows = self._stored()
        self.assertEqual([row[1] for row in rows], ['assistant_progress', 'done'])
        replay = self.client.get(f'/jobs/{self.claim.job_id}/events').json()
        self.assertEqual([row['event'] for row in replay['events']], ['assistant_progress', 'done'])

    def test_cancelled_turn_keeps_projection_but_stops_it(self):
        queue = task_events.DurableEventQueue(self.claim.job_id)
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue):
            async def scenario():
                self._feed(queue, 'updates', {"supervisor": {"messages": [
                    _tool_message(("execute", "call_1"))]}})
                for frame in self.projector.close('cancelled'):
                    progress_bus.emit_progress('assistant_progress', frame)
                self._feed(queue, 'updates', {"supervisor": {"messages": [
                    _tool_message(("execute", "call_2"))]}})
                await asyncio.sleep(0)
            asyncio.run(scenario())
        rows = self._stored()
        self.assertEqual([row[2]['kind'] for row in rows], ['tool_proposed'])
        self.assertEqual(rows[0][2]['tool_call_id'], 'call_1')
        page = self.client.get(f'/jobs/{self.claim.job_id}/events').json()
        self.assertEqual([row['data'].get('kind') for row in page['events']], ['tool_proposed'])

    def test_projection_never_shadows_the_persisted_answer(self):
        queue = task_events.DurableEventQueue(self.claim.job_id)
        with patch.object(progress_bus, 'get_progress_queue', return_value=queue):
            async def scenario():
                for index in range(5):
                    self._feed(queue, 'messages', (AIMessageChunk(content=f"第{index}段\n",
                                                                  id="lc_run--a"), {}))
                for frame in self.projector.close():
                    progress_bus.emit_progress('assistant_progress', frame)
                progress_bus.emit_text('最终交付答复')
                await asyncio.sleep(0)
            asyncio.run(scenario())
        rows = self._stored()
        # The public journal never persists provisional answer text.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[-1][1], 'text')
        self.assertEqual(rows[-1][2]['content'], '最终交付答复')
        self.assertEqual(rows[-1][2]['delivery'], 'committed')
        self.assertEqual([row[0] for row in rows], list(range(1, len(rows) + 1)))
        # The committed answer is the only frame carrying answer text on replay.
        replay = self.client.get(f'/jobs/{self.claim.job_id}/events').json()
        committed = [row['data'] for row in replay['events'] if row['event'] == 'text']
        self.assertEqual(committed, [rows[-1][2]])


class NativeStreamApiWiringTests(unittest.TestCase):
    """The outer API must ask for the merged native modes, not updates alone."""

    def test_stream_modes_requested_from_the_graph(self):
        self.assertEqual(tuple(NATIVE_STREAM_MODES), ("updates", "messages"))
        source = (Path(__file__).resolve().parents[1] / 'api' / 'main.py').read_text()
        self.assertIn('stream_mode=list(NATIVE_STREAM_MODES)', source)
        self.assertIn('subgraphs=True', source)
        self.assertNotIn('stream_mode="updates"', source)
