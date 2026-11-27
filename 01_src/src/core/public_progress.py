"""Bridge explicitly public native TodoList titles onto durable progress events.

F-14: the native LangGraph stream is the execution source of truth. This module
also projects the *public* part of that stream (model tokens, tool proposals,
sub-agent lifecycle, interrupt/resume) onto the existing ``assistant_progress``
event so ``task_events`` stays a durable log, recovery cursor and audit trail
instead of a second Agent Loop.

Nothing here reads or forwards checkpoint ids, channel names, graph state or
provider reasoning. Every projected frame is validated against an exact key
whitelist before it can be stored or replayed.
"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
import hashlib
import logging
import re
import time
import unicodedata

from langgraph.config import get_stream_writer
from langgraph.types import GraphOutput
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

# ── F-14 native stream projection contract ─────────────────────────────────
# Wire event: ``assistant_progress`` with ``source == "native_stream"``.
# ``kind`` names the native fact; ``status`` is the matching lifecycle phase.
# Version 1 is the only accepted version; a future incompatible change must
# bump ``schema_version`` and keep projecting older frames.
NATIVE_STREAM_SCHEMA_VERSION = 1
NATIVE_STREAM_SOURCE = "native_stream"
NATIVE_STREAM_MODES = ("updates", "messages")
NATIVE_STREAM_KIND_STATUS = {
    "model_preview": "streaming",
    "tool_proposed": "proposed",
    "subagent_started": "started",
    "subagent_completed": "completed",
    "subagent_failed": "failed",
    "interrupt": "requested",
    "resume": "resumed",
}
# Events an authorized in-graph emitter may publish for a sub-agent; today the
# native ``task`` tool is closed, so nothing sends them. Defined, never faked.
NATIVE_SUBAGENT_TERMINAL = frozenset({"subagent_completed", "subagent_failed"})
_NATIVE_BASE_KEYS = frozenset({"origin", "source", "visibility", "schema_version",
    "job_id", "run_id", "status", "kind", "at_ms", "note_id"})
_NATIVE_KIND_KEYS = {
    "model_preview": frozenset({"call_id", "delta_index", "text"}),
    "tool_proposed": frozenset({"tool_call_id", "tool_name"}),
    "subagent_started": frozenset({"subagent_id", "subagent_depth"}),
    "subagent_completed": frozenset({"subagent_id", "reason_code"}),
    "subagent_failed": frozenset({"subagent_id", "reason_code"}),
    "interrupt": frozenset({"interrupt_id"}),
    "resume": frozenset({"reason_code"}),
}
_NATIVE_STREAM_ID = re.compile(r"stream:[a-f0-9]{64}")
_NATIVE_CALL_ID = re.compile(r"mdl:[a-f0-9]{16}")
_NATIVE_SUBAGENT_ID = re.compile(r"sa:[a-f0-9]{16}")
_PREVIEW_LIMIT = 400
_NATIVE_REASON_CODES = frozenset({"recovery", "answer", "user", "internal", "cancelled",
    "paused", "completed", "failed", "unknown"})
# Preview text is published line by line. These markers never belong to public
# answer prose, so an unfinished private block cannot ride out as a live delta.
_PREVIEW_DENY = re.compile(
    r"<\s*/?\s*(?:analysis|thought|thinking|reasoning|scratchpad)\b|"
    r"\bsystem prompt\b|\bdeveloper message\b|\bbearer\s+\S|"
    r"\b(?:api[_ ]?key|access[_ ]?key|secret[_ ]?key|password|passwd|private key)"
    r"[\"']?\s*[:=]|\bsk-[A-Za-z0-9_-]{8,}|"
    r"系统提示词|开发者消息|思维链|推理过程", re.I)

PUBLIC_TODO_INSTRUCTIONS = ("Todos are private. An active title prefixed 公开步骤： is public. "
    "Use a generic action phrase of at most 80 characters in the user's language. No user text, "
    "names, IDs, queries, paths, code, results, secrets, reasoning or drafts. Omit the prefix "
    "if unsure. Never add a todo solely to announce progress.")

_PREFIX = "公开步骤："
_MAX_TITLE = 80
_TODO_STATUSES = ("pending", "in_progress", "completed")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NOTE_ID = re.compile(r"todo:[a-f0-9]{64}")
_PRIVATE_MARKER = re.compile(
    r"\b(?:i\s+(?:think|need|should|must|will|reason|have)|let me|chain of thought|"
    r"reasoning|thought process|system prompt|developer message|bearer|api key|"
    r"password|secret|token|private|draft|dsml|https?|sk[- ]\w+)\b"
    r"|我(?:认为|需要|应该|必须|先|将|会)|让我|思考过程|推理过程|思维链|草稿|内部指令|"
    r"系统提示|开发者消息|密码|密钥|访问令牌|\d{4,}|[A-Za-z0-9]{26,}", re.I)
_EVENT_KEYS = {"origin", "source", "visibility", "status", "note_id", "job_id", "text", "seq"}
_LOG = logging.getLogger(__name__)


def _title(value: str) -> str:
    """Require a bounded, plain action label. Reject rather than truncate or repair."""
    if not isinstance(value, str) or len(value) > _MAX_TITLE:
        return ""
    value = value.strip(" ")
    if len(value) < 2 or _PRIVATE_MARKER.search(value):
        return ""
    if any(not (unicodedata.category(char)[0] in "LM" or char in " -") for char in value):
        return ""
    if not any(char.isalpha() for char in value):
        return ""
    from src.core.pii_mask import mask_sensitive

    return value if mask_sensitive(value) == value else ""


def _source_copy(title: str, protected_text: tuple[str, ...]) -> bool:
    def normalize(value):
        return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalpha())
    clean = normalize(title)
    return any(clean == source or len(clean) >= 8 and clean in source
               for value in protected_text if isinstance(value, str) and value
               if (source := normalize(value)))


def public_todo_title(todos, *, protected_text: tuple[str, ...] = ()) -> str:
    """Select only the one unambiguous in-progress label, never other todos."""
    if not isinstance(todos, list) or not 1 <= len(todos) <= 12:
        return ""
    if any(not isinstance(item, dict) or item.keys() != {"content", "status"}
           or not isinstance(item["content"], str) or item["status"] not in _TODO_STATUSES
           for item in todos):
        return ""
    active = [item["content"] for item in todos if item["status"] == "in_progress"]
    if len(active) != 1 or not active[0].startswith(_PREFIX):
        return ""
    title = _title(active[0][len(_PREFIX):])
    return title if title and not _source_copy(title, protected_text) else ""


def project_native_progress(data: dict) -> dict:
    """Accept the typed public event, never older host or model narration."""
    if not isinstance(data, dict):
        return {}
    if data.get("source") == NATIVE_STREAM_SOURCE:
        # Provisional model prose has no trusted public provenance. This also
        # suppresses its content on replay of rows written by earlier releases.
        return {} if data.get("kind") == "model_preview" else project_native_stream_event(data)
    if (data.keys() - _EVENT_KEYS or data.get("origin") != "langgraph"
            or data.get("source") != "write_todos" or data.get("visibility") != "public"
            or not isinstance(data.get("job_id"), str) or not _ID.fullmatch(data["job_id"])
            or not isinstance(data.get("note_id"), str) or not _NOTE_ID.fullmatch(data["note_id"])):
        return {}
    if data.get("status") == "in_progress":
        title = _title(data.get("text"))
        if not title or title != data["text"]:
            return {}
    elif data.get("status") != "inactive" or "text" in data:
        return {}
    return {k: v for k, v in data.items() if k != "seq"}


def preview_text(value) -> str:
    """Return only settled public prose from one native delta, else ''.

    A live delta is a fragment: a private block may be opened in one chunk and
    closed many chunks later, so per-chunk structural stripping is not enough.
    Only text up to the last line break is settled enough to publish, and only
    when no private marker survives. The unline-broken tail is held back by the
    projector and re-checked when the model call ends.
    """
    from src.core.public_delivery import public_answer_text

    text = public_answer_text(value)
    if not text:
        return ""
    cut = text.rfind("\n")
    if cut < 0:
        return ""
    settled = text[:cut]
    if _PREVIEW_DENY.search(settled):
        return ""
    return settled


def _settled_tail(value) -> str:
    """Final tail of a finished model call; accepted only when structurally clean."""
    from src.core.public_delivery import public_answer_text, strip_private_text

    text = public_answer_text(value)
    if not text or _PREVIEW_DENY.search(text):
        return ""
    # An unterminated tag or container can only be a truncated private block.
    if text.count("<") != text.count(">") or strip_private_text(text) != text:
        return ""
    return text


def native_stream_note_id(*parts: object) -> str:
    """Deterministic identity so a re-observed native fact is never stored twice."""
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()
    return f"stream:{digest}"


def _opaque_token(value: str, prefix: str) -> str:
    """Derive a stable opaque id so internal namespace strings never leak."""
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _safe_token(value) -> str:
    """Keep only a bare identifier; anything else is refused, never repaired."""
    return value if isinstance(value, str) and _ID.fullmatch(value) else ""


# ── explicit sub-agent lifecycle (custom stream entry) ─────────────────────
# Only code that has actually entered the native ``task`` handler may publish a
# start, and only a confirmed acceptance may publish a completion. A model tool
# proposal is *not* a start, and the ``task`` tool's ordinary string branches
# (unknown type, refusal, recursion refusal) are *not* a completion. The marker
# is a typed object, so no model or user text can forge one.
_SUBAGENT_STATUS_KIND = {"started": "subagent_started", "completed": "subagent_completed",
                         "failed": "subagent_failed"}
_SUBAGENT_STATUS_REASON = {"started": "", "completed": "completed", "failed": "failed"}


@dataclass(frozen=True)
class _SubagentProgress:
    job_id: str
    subagent_id: str
    status: str
    reason_code: str
    depth: int


def subagent_stream_id(value) -> str:
    """Derive the public opaque sub-agent id from any stable caller key.

    The harness may pass the native ``task`` tool_call_id or an explicit id; the
    public surface only ever sees ``sa:<16 hex>``, so no namespace, thread id or
    checkpoint detail can leak through this channel.
    """
    key = value if isinstance(value, str) and value else ""
    if _NATIVE_SUBAGENT_ID.fullmatch(key):
        return key
    return _opaque_token(f"task\0{key}", "sa") if key else ""


def project_subagent_progress(progress, *, job_id: str | None = None) -> list[dict]:
    """Validate one explicit sub-agent lifecycle fact into a public frame."""
    if type(progress) is not _SubagentProgress:
        return []
    if job_id is not None and progress.job_id != job_id:
        return []
    kind = _SUBAGENT_STATUS_KIND.get(progress.status)
    if kind is None:
        return []
    subagent_id = subagent_stream_id(progress.subagent_id)
    if not subagent_id:
        return []
    if progress.status == "started":
        extra = {"subagent_id": subagent_id,
                 "subagent_depth": max(1, min(8, int(progress.depth or 1)))}
        identity = f"started:{subagent_id}"
    else:
        code = progress.reason_code if progress.reason_code in _NATIVE_REASON_CODES \
            else _SUBAGENT_STATUS_REASON[progress.status]
        extra = {"subagent_id": subagent_id, "reason_code": code}
        identity = f"{progress.status}:{subagent_id}"
    payload = {"origin": "langgraph", "source": NATIVE_STREAM_SOURCE,
               "visibility": "public", "schema_version": NATIVE_STREAM_SCHEMA_VERSION,
               "job_id": progress.job_id, "run_id": progress.job_id,
               "status": NATIVE_STREAM_KIND_STATUS[kind], "kind": kind,
               "at_ms": int(time.time() * 1000),
               "note_id": native_stream_note_id(progress.job_id, progress.job_id, kind, identity),
               **extra}
    public = project_native_stream_event(payload)
    return [public] if public else []


def write_subagent_progress(job_id: str, *, subagent_id: str = "", status: str,
                            tool_call_id: str = "", depth: int = 1,
                            reason_code: str = "") -> bool:
    """Publish one explicit sub-agent lifecycle fact. Never raises.

    Call from the native ``task`` handler only:

    * ``status="started"`` right after the handler is actually entered;
    * ``status="completed"`` only once the sub-agent result has been accepted and
      the paired receipt/observation is recorded;
    * ``status="failed"`` only for a confirmed failure fact.

    Do **not** call it for a model proposal, a refusal/unknown-type reply, a
    cancellation or any unconfirmed outcome. Inside a graph run the fact travels
    as a typed ``custom`` stream object; outside one it falls back to the
    request-scoped progress queue, so either call site produces exactly one
    durable, replayable event.
    """
    marker = _SubagentProgress(job_id=str(job_id or ""), subagent_id=str(subagent_id or ""),
                               status=str(status or ""), reason_code=str(reason_code or ""),
                               depth=int(depth or 1))
    key = marker.subagent_id or str(tool_call_id or "")
    marker = _SubagentProgress(job_id=marker.job_id, subagent_id=subagent_stream_id(key),
                               status=marker.status, reason_code=marker.reason_code,
                               depth=marker.depth)
    if not marker.job_id or not marker.subagent_id or marker.status not in _SUBAGENT_STATUS_KIND:
        return False
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(marker)
        return True
    except RuntimeError:
        pass  # No graph run in this context: use the request-scoped queue below.
    except Exception as exc:
        _LOG.warning("subagent_progress_stream_failed kind=%s", type(exc).__name__)
        return False
    try:
        from src.core.progress_bus import emit_progress

        for event in project_subagent_progress(marker, job_id=marker.job_id):
            emit_progress("assistant_progress", event)
        return True
    except Exception as exc:
        _LOG.warning("subagent_progress_emit_failed kind=%s", type(exc).__name__)
        return False


def project_native_stream_event(data: dict) -> dict:
    """Validate one native stream projection. Unknown or unsafe frames fail closed."""
    if not isinstance(data, dict) or data.get("origin") != "langgraph":
        return {}
    if data.get("visibility") != "public" or type(data.get("schema_version")) is not int:
        return {}
    if data["schema_version"] != NATIVE_STREAM_SCHEMA_VERSION:
        return {}
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in NATIVE_STREAM_KIND_STATUS:
        return {}
    if data.get("status") != NATIVE_STREAM_KIND_STATUS[kind]:
        return {}
    # The durable event store stamps ``seq`` on every stored payload; it is
    # added by the event log, never supplied by the projection itself.
    if data.keys() - {"seq"} - (_NATIVE_BASE_KEYS | _NATIVE_KIND_KEYS[kind]):
        return {}
    for key in ("job_id", "run_id"):
        if not isinstance(data.get(key), str) or not _ID.fullmatch(data[key]):
            return {}
    if not isinstance(data.get("note_id"), str) or not _NATIVE_STREAM_ID.fullmatch(data["note_id"]):
        return {}
    if type(data.get("at_ms")) is not int or not 0 < data["at_ms"] < 4102444800000:
        return {}
    public = {key: value for key, value in data.items() if key != "seq"}
    if kind == "model_preview":
        if not isinstance(data.get("call_id"), str) or not _NATIVE_CALL_ID.fullmatch(data["call_id"]):
            return {}
        if type(data.get("delta_index")) is not int or data["delta_index"] < 0:
            return {}
        if not isinstance(data.get("text"), str) or not 1 <= len(data["text"]) <= _PREVIEW_LIMIT:
            return {}
        from src.core.public_delivery import strip_private_text

        # A published fragment must contain no private structure and no marker
        # that only ever appears in prompts, reasoning or secrets.
        if strip_private_text(data["text"]) != data["text"] or _PREVIEW_DENY.search(data["text"]):
            return {}
        if not data["text"].strip():
            return {}
    elif kind == "tool_proposed":
        for key in ("tool_call_id", "tool_name"):
            if not isinstance(data.get(key), str) or not _ID.fullmatch(data[key]):
                return {}
    elif kind in {"subagent_started", "subagent_completed", "subagent_failed"}:
        if not isinstance(data.get("subagent_id"), str) or not _NATIVE_SUBAGENT_ID.fullmatch(data["subagent_id"]):
            return {}
        if kind == "subagent_started":
            depth = data.get("subagent_depth")
            if type(depth) is not int or not 1 <= depth <= 8:
                return {}
        elif not isinstance(data.get("reason_code"), str) or data["reason_code"] not in _NATIVE_REASON_CODES:
            return {}
    elif kind == "interrupt":
        value = data.get("interrupt_id")
        if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
            return {}
    elif kind == "resume":
        if not isinstance(data.get("reason_code"), str) or data["reason_code"] not in _NATIVE_REASON_CODES:
            return {}
    return public


def unpack_stream_chunk(chunk):
    """Normalize one ``astream`` chunk into ``(namespace, mode, data)``.

    Accepts the v1 tuple form (2- or 3-tuple) and the v2 dict form so the API
    layer keeps one code path regardless of the streaming version it asks for.
    Unknown shapes are reported as an ``updates`` payload at the root graph and
    never raise: an unrecognized frame must not abort a live turn.
    """
    if isinstance(chunk, dict):
        if "type" not in chunk:
            return (), "updates", chunk
        namespace = chunk.get("ns") or ()
        return tuple(namespace), str(chunk.get("type") or ""), chunk.get("data")
    if isinstance(chunk, tuple) and len(chunk) in (2, 3):
        if len(chunk) == 3:
            return tuple(chunk[0] or ()), str(chunk[1] or ""), chunk[2]
        return (), str(chunk[0] or ""), chunk[1]
    return (), "updates", chunk


class NativeStreamProjector:
    """Turn native ``updates``/``messages`` frames into bounded public facts.

    The projector is deliberately dumb about the graph: it never inspects
    channels, checkpoints or state keys, never re-derives the Agent Loop, and
    stops producing frames once the turn closes. Every frame it returns is
    validated by :func:`project_native_stream_event` before it is handed to the
    durable event log, so callers may emit them unguarded.
    """

    def __init__(self, *, job_id: str, run_id: str, max_events: int = 60,
                 max_chars: int = 4000, min_chars: int = 64,
                 min_interval: float = 0.5, now=time.time, model_preview: bool = True):
        self._model_preview = model_preview
        self.job_id, self.run_id = str(job_id), str(run_id or job_id)
        self.max_events, self.max_chars = max(1, int(max_events)), max(0, int(max_chars))
        self.min_chars, self.min_interval = max(1, int(min_chars)), max(0.0, float(min_interval))
        self._now = now
        self._events = 0
        self._chars = 0
        self._closed = False
        self._call_id = ""
        self._pending = ""
        self._tail = ""
        self._index = 0
        self._last_flush = 0.0
        self._preview_blocked = False
        self._call_published = False
        self._seen_tools: set[str] = set()
        self._calls: dict[int, dict] = {}
        self._interrupts: set[str] = set()
        self._resumed = False

    # -- public surface ----------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, mode, data, namespace=()):
        """Return public frames for one native chunk (never raises).

        Nested namespaces never become sub-agent events: the SDK's ``tools``
        task namespace is not a sub-agent identity, and sub-agent lifecycle is
        published explicitly by the native ``task`` handler instead.
        """
        if self._closed:
            return []
        try:
            if mode == "messages":
                return self._message(data, tuple(namespace or ()))
            if mode == "updates":
                return [] if namespace else self._updates(data)
            return []
        except Exception:  # A projection bug must never abort a live turn.
            _LOG.warning("native_stream_projection_failed kind=%s", type(data).__name__)
            return []

    def flush(self):
        """Settle the current model call: its tool proposals, then its tail."""
        if self._closed:
            return []
        events = self._settle_tool_fragments()
        tail, self._tail, self._pending = self._tail, "", ""
        text = _settled_tail(tail) if tail else ""
        if text:
            self._pending = text
        if self._pending:
            events += self._publish()
        return events

    def close(self, reason_code: str = "") -> list:
        """Close the projection; the turn owns the terminal fact, not this class."""
        events = self.flush()
        self._closed = True
        return events

    def resume(self, reason_code: str = "recovery"):
        """Report an admitted resume of this run exactly once."""
        if self._closed or self._resumed:
            return []
        code = str(reason_code or "recovery").lower()
        self._resumed = True
        return self._event("resume", reason_code=code if code in _NATIVE_REASON_CODES else "unknown",
                           identity="resume")

    def interrupt(self, value):
        """Report one native interrupt without exposing its checkpoint payload."""
        if self._closed:
            return []
        raw = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
        interrupt_id = _safe_token(raw)
        if interrupt_id and interrupt_id in self._interrupts:
            return []
        if interrupt_id:
            self._interrupts.add(interrupt_id)
        extra = {"interrupt_id": interrupt_id} if interrupt_id else {}
        return self._event("interrupt", identity=interrupt_id or "interrupt", **extra)

    # -- native frames -----------------------------------------------------
    def _message(self, data, namespace):
        """Project one ``(message, metadata)`` pair.

        Only a streaming ``AIMessageChunk`` may publish answer text. A
        ``ToolMessage`` carries tool output or a sub-agent report: it is never
        prose for the user, and it is never treated as a sub-agent outcome
        because the native ``task`` tool also returns plain strings for
        refusals and unknown sub-agent types. Sub-agent lifecycle is published
        explicitly by :func:`write_subagent_progress`.
        """
        if not isinstance(data, (tuple, list)) or len(data) != 2:
            return []
        message = data[0]
        if isinstance(message, ToolMessage):
            return []
        if namespace:
            return []  # A nested agent's tokens and messages stay private.
        if not isinstance(message, AIMessageChunk):
            return []  # Aggregate messages are not new streamed deltas.
        fragments = getattr(message, "tool_call_chunks", None)
        if fragments:
            # Native tool proposal: the model's own call identity, never args.
            self._note_tool_fragments(fragments)
            return []
        if not self._model_preview:
            return []
        if (getattr(message, "additional_kwargs", None) or {}).get("reasoning_content"):
            return []
        if getattr(message, "tool_calls", None):
            # A call that ends in tool proposals is working narration, not answer.
            return []
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content:
            return []
        call_id = _opaque_token(getattr(message, "id", "") or "", "mdl")
        events = []
        if call_id != self._call_id:
            # A new model call starts: settle the previous one first, because a
            # sequential node cannot be generating two answers at once.
            events = self.flush()
            self._call_id, self._index, self._last_flush = call_id, 0, 0.0
            self._call_published = False
        self._tail += content
        # A very long single line is still published at a sentence boundary; a
        # short unfinished line stays buffered because it may open a private
        # block whose closing marker only arrives many deltas later.
        settled = self._settle(self._tail, force=len(self._tail) > _PREVIEW_LIMIT)
        if settled:
            self._tail = self._after_settled(self._tail, settled)
            self._pending += settled
        if self._pending:
            # The first settled text of a call is published immediately; later
            # fragments coalesce so one answer cannot flood the durable log.
            due = (not self._call_published or len(self._pending) >= self.min_chars
                   or self._now() - self._last_flush >= self.min_interval)
            if due:
                events += self._publish()
        return events

    def _settle(self, tail, *, force):
        """Return the part of the buffered tail that is safe to publish now."""
        settled = preview_text(tail)
        if settled or not force:
            return settled
        cut = min(len(tail), _PREVIEW_LIMIT)
        for index in range(cut, 0, -1):
            if tail[index - 1] in " 。.,，;；:：!！?？":
                cut = index
                break
        candidate = tail[:cut]
        return candidate if candidate.strip() and not _PREVIEW_DENY.search(candidate) else ""

    @staticmethod
    def _after_settled(tail, settled):
        """Drop published raw text, keeping the unpublished tail."""
        cut = tail.rfind("\n")
        if cut >= 0 and cut + 1 >= len(settled):
            return tail[cut + 1:]
        return tail[len(settled):] if tail.startswith(settled) else tail[-_PREVIEW_LIMIT:]

    def _publish(self):
        """Publish buffered settled text as bounded preview frames."""
        pending, self._pending, frames = self._pending, "", []
        while pending:
            piece, pending = pending[:_PREVIEW_LIMIT], pending[_PREVIEW_LIMIT:]
            frames.extend(self._event("model_preview", text=piece, call_id=self._call_id,
                                      delta_index=self._index, identity=f"preview:{self._index}"))
            self._index += 1
            if self._preview_blocked:
                return frames
        self._last_flush = self._now()
        self._call_published = True
        return frames

    def _updates(self, data):
        """Project tool proposals and interrupts out of one root ``updates`` payload."""
        if not isinstance(data, dict):
            return []
        events = []
        interrupts = data.get("__interrupt__")
        if interrupts:
            for value in interrupts if isinstance(interrupts, (list, tuple)) else (interrupts,):
                events.extend(self.interrupt(value))
        # State loaders and persistence nodes can return historical messages.
        # Only the execution node can announce a current proposal.
        events.extend(self._tool_calls(data.get("supervisor")))
        return events

    def _tool_calls(self, output):
        """A native tool batch in a node update is also the start of a tool cycle."""
        messages = output.get("messages") if isinstance(output, dict) else None
        if not isinstance(messages, (list, tuple)):
            return []
        events = []
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            meta = message.additional_kwargs
            if meta.get("run_id", self.run_id) != self.run_id:
                continue
            for call in message.tool_calls or ():
                tool_call_id = _safe_token(call.get("id"))
                tool_name = _safe_token(call.get("name"))
                if not tool_call_id or not tool_name or tool_call_id in self._seen_tools:
                    continue
                self._seen_tools.add(tool_call_id)
                events.extend(self._event("tool_proposed", tool_call_id=tool_call_id,
                                          tool_name=tool_name, identity=tool_call_id))
        return events

    def _note_tool_fragments(self, fragments):
        """Remember a streamed tool proposal by index; args are never kept."""
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            index = fragment.get("index")
            slot = self._calls.setdefault(index if type(index) is int else 0,
                                          {"id": "", "name": ""})
            if _safe_token(fragment.get("id")):
                slot["id"] = fragment["id"]
            if _safe_token(fragment.get("name")):
                slot["name"] = fragment["name"]

    def _settle_tool_fragments(self):
        """Publish the proposals of a finished model call, exactly once each."""
        calls, self._calls, events = self._calls, {}, []
        for _, slot in sorted(calls.items()):
            tool_call_id, tool_name = slot.get("id", ""), slot.get("name", "")
            if not tool_call_id or not tool_name or tool_call_id in self._seen_tools:
                continue
            self._seen_tools.add(tool_call_id)
            events.extend(self._event("tool_proposed", tool_call_id=tool_call_id,
                                      tool_name=tool_name, identity=tool_call_id))
        return events

    # -- frame construction ------------------------------------------------
    def _event(self, kind, *, identity="", **extra):
        """Build one validated public frame; drop it if it fails validation."""
        if self._closed:
            return []
        at_ms = int(self._now() * 1000)
        note = native_stream_note_id(self.job_id, self.run_id, kind, identity)
        payload = {"origin": "langgraph", "source": NATIVE_STREAM_SOURCE,
                   "visibility": "public", "schema_version": NATIVE_STREAM_SCHEMA_VERSION,
                   "job_id": self.job_id, "run_id": self.run_id,
                   "status": NATIVE_STREAM_KIND_STATUS[kind], "kind": kind,
                   "at_ms": at_ms, "note_id": note, **extra}
        public = project_native_stream_event(payload)
        if not public:
            return []
        if kind == "model_preview":
            if self._events >= self.max_events or self._chars >= self.max_chars:
                self._preview_blocked = True
                return []
            self._events += 1
            self._chars += len(public["text"])
        return [public]



@dataclass(frozen=True)
class _PublicTodoProgress:
    job_id: str
    native_update_id: str
    title: str

    def event(self) -> dict:
        status = "in_progress" if self.title else "inactive"
        title = {"text": self.title} if self.title else {}
        digest = hashlib.sha256(
            f"{self.job_id}\0{self.native_update_id}\0{status}\0{self.title}".encode()).hexdigest()
        return project_native_progress({"origin": "langgraph", "source": "write_todos",
            "visibility": "public", "job_id": self.job_id, "note_id": f"todo:{digest}",
            "status": status, **title})


def write_native_todo_progress(job_id: str, native_update_id: str, todos: list, *,
                               protected_text: tuple[str, ...]) -> None:
    """Call from the authorized graph node after the native todo commit."""
    title = public_todo_title(todos, protected_text=protected_text)
    get_stream_writer()(_PublicTodoProgress(job_id, native_update_id, title))


async def invoke_with_public_progress(graph, initial, config, *, context, job_id):
    """Match native ainvoke(v2) values/interrupts, plus opt-in custom events.

    ``custom`` is the explicit, code-authored channel: the root-scoped native
    todo title, and the sub-agent lifecycle fact that only the native ``task``
    handler may publish (see :func:`write_subagent_progress`). Both are typed
    markers, so no model or user text can produce them.
    """
    from src.core.progress_bus import emit_progress

    latest, interrupts, active = None, [], ""
    async with aclosing(graph.astream(initial, config, context=context, durability="sync",
            stream_mode=["values", "custom"], output_keys=graph.output_channels, version="v2")) as events:
        async for part in events:
            if part["type"] == "values":
                latest = part["data"]
                interrupts.extend(part.get("interrupts", ()))
            elif part["type"] == "custom":
                value = part["data"]
                if type(value) is _SubagentProgress:
                    # A nested writer still reports this run: the marker carries
                    # the job and the explicit status, so no namespace is needed.
                    for event in project_subagent_progress(value, job_id=job_id):
                        try:
                            emit_progress("assistant_progress", event)
                        except Exception as exc:
                            _LOG.warning("subagent_progress_forward_failed kind=%s",
                                         type(exc).__name__)
                    continue
                if part["ns"] or type(value) is not _PublicTodoProgress \
                        or value.job_id != job_id or value.title == active:
                    continue
                if event := value.event():
                    try:
                        emit_progress("assistant_progress", event)
                        active = value.title
                    except Exception as exc:
                        _LOG.warning("public_todo_progress_failed kind=%s", type(exc).__name__)
    return GraphOutput(value=latest, interrupts=tuple(interrupts))
