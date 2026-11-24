"""S2: public working notes (``assistant_progress``).

The model already streams assistant text next to its tool calls.  That text
has two very different meanings depending on how the *round* ends:

* round ends with tool calls -> sanitized public content goes to the timeline.
* round ends without tool calls -> the text **is** the final answer and keeps
  using the existing ``text`` path.

Mixing the two (the "``on_text`` -> ``emit_text``" shortcut) pollutes the
answer, duplicates content and can persist progress text as the reply.  This
module therefore classifies at round level and never touches the answer path.

Hard rules:
* private ``reasoning_content`` is never a source here;
* private protocol markup, structured payloads and credential-like text do not
  create a progress note;
* every failure here is swallowed: public progress must never block the agent.
"""

from __future__ import annotations

from contextvars import ContextVar
import logging
import os
import re
import uuid

from src.core.progress_bus import PUBLIC_WORK_NOTES, emit_progress, get_turn_scope

logger = logging.getLogger(__name__)

#: Longest single public note kept. The timeline row is one line; longer text
#: is truncated instead of silently blowing up the trace.
MAX_CHARS = 400

_ENV_FLAG = "CITRUS_PUBLIC_PROGRESS"

_PRIVATE_TEXT_RE = re.compile(
    r'</?\s*(?:think|analysis|reasoning|tool_calls?|tool_result|calls|invoke|parameter|function)\b'
    r'|\bDSML\b|\bBearer\s+\S+|(?:\{|,)\s*"[^"]+"\s*:', re.I)
_TOOL_MARKER_RE = re.compile(r"\[(?:TOOL_[A-Z_]+|[LWHClwhc]?\d{1,3})\]")

# Credential-ish material must never reach the timeline.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bapi[_\- ]?key\b\s*[:=]"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?:密码|密钥|访问令牌)\s*[：:=]\s*\S+"),
    re.compile(r"\bhttps?://[^/\s:@]+:[^/\s@]+@", re.I),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b"),
)

_WHITESPACE_RE = re.compile(r"\s+")

# Completion declarations are private validation inputs, including truncated or
# fenced JSON. Reject before truncation so an envelope cannot become a note.
_COMPLETION_RE = re.compile(
    r'"(?:schema_version|answer|incidental_partials|proposal_repairs)"\s*:|\bCompletionEnvelope\b',
    re.I)

#: Immutable request-local state: inherited task contexts never mutate a shared
#: default/list. Include the request/job scope for sequential requests as well.
_last_emitted: ContextVar[tuple | None] = ContextVar("public_progress_last_emitted", default=None)


def enabled() -> bool:
    """Public working notes are on unless explicitly disabled."""
    return os.getenv(_ENV_FLAG, "1").strip().lower() not in ("0", "false", "off", "no")


def sanitize(text: str) -> str:
    """Return a single-line, bounded, credential-free public note or ``\"\"``."""
    if not isinstance(text, str) or len(text) > 16_000:
        return ""
    raw = text.strip()
    if (not raw or raw.startswith(("{", "[")) or "```" in raw
            or _COMPLETION_RE.search(raw) or _PRIVATE_TEXT_RE.search(raw)):
        return ""
    cleaned = _TOOL_MARKER_RE.sub("", raw)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(cleaned):
            return ""
    from src.core.pii_mask import mask_sensitive
    cleaned = _WHITESPACE_RE.sub(" ", mask_sensitive(cleaned)).strip()
    # Leading punctuation only ("…") is noise, not a note.
    if len(cleaned) <= 1:
        return ""
    if len(cleaned) > MAX_CHARS:
        cleaned = cleaned[: MAX_CHARS - 1].rstrip() + "…"
    return cleaned


class PublicAnswerPreview:
    def __init__(self):
        self._prefix = ""
        self._published = False
        self._closed = False

    def append(self, chunk: str) -> None:
        if self._closed or self._published:
            return
        if len(self._prefix) + len(chunk) > MAX_CHARS:
            self._closed = True
            return
        self._prefix += chunk
        boundary = max(self._prefix.rfind(char) for char in "。！？!?\n ")
        if boundary < 48:
            return
        if re.search(r"\[[LWHClwhc]\d{0,3}$", self._prefix[:boundary + 1].rstrip()):
            return
        self._closed = True
        text = sanitize(self._prefix[:boundary + 1]) if sanitize(self._prefix) else ""
        if text:
            from src.core.progress_bus import emit_answer_start, emit_text
            emit_answer_start()
            emit_text(text)
            self._published = True

    def clear(self) -> None:
        self._closed = True
        if self._published:
            from src.core.progress_bus import emit_text_reset
            emit_text_reset()
            self._published = False


def emit_retrieval_stage(stage: str) -> None:
    """Publish retrieval milestones without query or source data."""
    try:
        emit_progress("tool_progress", {"retrieval_stage": stage, "visibility": "public",
                                        "origin": "host"})
    except Exception:
        pass


def public_content_text(content) -> str:
    """Extract public text blocks without exposing provider reasoning or data."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    return "".join(
        block if isinstance(block, str) else block["text"]
        for block in content
        if isinstance(block, str) or (
            isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    )


def public_progress_text(chunks, has_tool_calls: bool) -> str:
    """Classify one model round: working note vs final answer.

    The final round produces no progress note and uses the answer path.
    """
    if not has_tool_calls:
        return ""
    if not chunks:
        return ""
    try:
        return sanitize(public_content_text(chunks))
    except Exception:  # noqa: BLE001 - progress must never break execution
        return ""


def emit_public_progress(text: str, *, step_id: str = "",
                         phase: str = "working", note_id: str = "",
                         model_step: int = 0, tool_call_ids=(),
                         job_id: str = "", category: str = "") -> bool:
    """Publish a sanitized note through the existing request/durable queue.

    ``job_id`` is host identity, never a model argument. DurableEventQueue owns
    persistence; this emitter must not append the same event separately.
    """
    try:
        label = PUBLIC_WORK_NOTES.get(category)
        note = label or sanitize(text)
        if not enabled() or not note:
            return False
        from src.core.tracing import get_job_id
        job_id = str(job_id or get_job_id() or "")
        scope = (*get_turn_scope(), job_id)
        event_key = (scope, note_id or (model_step, step_id))
        if _last_emitted.get() == event_key:
            return False
        emit_progress("assistant_progress", {
            "visibility": "public", "origin": "host" if label else "model",
            "category": category if label else "", "step_id": step_id or "analysis",
            "phase": phase,
            "text": note,
            "chars": len(note),
            "note_id": note_id or uuid.uuid4().hex,
            "model_step": max(0, int(model_step)),
            "tool_call_ids": list(tool_call_ids),
            "job_id": job_id,
        })
        _last_emitted.set(event_key)
    except Exception as exc:
        logger.warning("public_progress_failed stage=emit kind=%s", type(exc).__name__)
        return False
    return True


def emit_tool_milestone(category: str, *, job_id: str, tool_call_id: str) -> bool:
    if category not in PUBLIC_WORK_NOTES:
        return False
    return emit_public_progress("", note_id=f"{job_id}:{category}:{tool_call_id}",
                                tool_call_ids=(tool_call_id,), job_id=job_id, category=category)


def reset_emitted() -> None:
    """Clear per-request dedupe state (called when a request finishes)."""
    _last_emitted.set(None)
