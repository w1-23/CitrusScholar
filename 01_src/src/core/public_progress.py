"""Emit host-verified working notes through the durable public event path.

Only fixed host milestones are emitted. Sanitization of model narration is
kept for historical rendering, never for new execution progress.
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
    """Compatibility shim for hosts still passing an unreviewed delta callback.

    The first model round can become a tool round, be rejected, or be replaced.
    A later reset cannot undo disclosure, so no preview reaches live or replay SSE.
    """

    def append(self, chunk: str) -> None:
        pass

    def clear(self) -> None:
        pass


def emit_retrieval_stage(stage: str) -> None:
    """Publish retrieval milestones without query or source data."""
    category = f"retrieval_{stage}"
    if category in PUBLIC_WORK_NOTES:
        emit_public_progress("", category=category, note_id=category)


def emit_public_progress(text: str, *, step_id: str = "",
                         phase: str = "working", note_id: str = "",
                         model_step: int = 0, tool_call_ids=(),
                         job_id: str = "", category: str = "") -> bool:
    """Publish a fixed host note through the existing request/durable queue.

    The legacy positional text parameter cannot supply a new public note.

    ``job_id`` is host identity, never a model argument. DurableEventQueue owns
    persistence; this emitter must not append the same event separately.
    """
    try:
        note = PUBLIC_WORK_NOTES.get(category)
        if not enabled() or not note:
            return False
        from src.core.tracing import get_job_id
        job_id = str(job_id or get_job_id() or "")
        scope = (*get_turn_scope(), job_id)
        event_key = (scope, note_id or (model_step, step_id))
        if _last_emitted.get() == event_key:
            return False
        emit_progress("assistant_progress", {
            "visibility": "public", "origin": "host",
            "category": category, "step_id": step_id or "analysis",
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
