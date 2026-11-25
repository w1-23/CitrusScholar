"""Private, live-message state for DeepSeek's thinking/tool protocol.

Only the official DeepSeek Chat Completions endpoint is opted in. Compatible
proxies need their own verified capability contract before being added here.
The state is deliberately excluded from message/trace serialization; it is not
conversation history. Only the authenticated checkpoint codec opts in.
"""
from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage, get_notification_metadata
from pydantic import Field


def uses_deepseek_protocol(model: str, base_url: str) -> bool:
    try:
        url = urlsplit(str(base_url))
        return (url.scheme == "https" and url.hostname == "api.deepseek.com"
                and url.port in (None, 443) and url.path.rstrip("/") in ("", "/v1")
                and not (url.username or url.password or url.query or url.fragment)
                and str(model).lower().startswith("deepseek-"))
    except ValueError:
        return False


_checkpoint_scope = ContextVar[tuple[str, str] | None]("deepseek_checkpoint_scope", default=None)


@contextmanager
def _checkpoint_protocol(scope: tuple[str, str]):
    """Enable the provider state only during protected, synchronous SDK serde."""
    token = _checkpoint_scope.set(scope)
    try:
        yield
    finally:
        _checkpoint_scope.reset(token)


@dataclass(frozen=True, repr=False)
class _ProtocolState:
    scope: tuple[str, str]
    reasoning: str


class _DeepSeekMessage(AIMessage):
    # Field exclusion covers Pydantic dumps, messages_to_dict and LangChain's
    # Serializable.to_json (callbacks/traces). Do not put this in kwargs/content.
    provider_protocol: _ProtocolState | None = Field(default=None, exclude=True, repr=False)

    def model_dump(self, *args, **kwargs):
        public = super().model_dump(*args, **kwargs)
        # JsonPlus uses this public Pydantic hook for explicitly allowlisted
        # messages even inside SDK-owned graph values. Traces and wire encoding
        # are outside this scope and still use the excluded representation.
        scope = _checkpoint_scope.get()
        state = self.provider_protocol
        if scope is not None and state is not None:
            if state.scope != scope:
                raise ValueError("private_protocol_scope_mismatch")
            return {**public, "provider_protocol": {"scope": state.scope, "reasoning": state.reasoning}}
        return public


class _DeepSeekMessageChunk(_DeepSeekMessage, AIMessageChunk):
    # Decoder compatibility for encrypted snapshots written by the old adapter.
    type: Literal["AIMessageChunk"] = "AIMessageChunk"


def current_tool_loop_start(messages) -> int:
    """Locate the current real user turn, shared by admission and wire encoding.

    Runtime controls cannot reset a tool loop. Repeated projections stamped with
    the same trusted turn identity also retain its complete protocol chain.
    """
    start, turn_id = 0, None
    for i, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        if get_notification_metadata(message) is not None:
            continue
        metadata = message.additional_kwargs
        trusted = metadata.get("context_trusted") is True
        identity = metadata.get("context_turn_id") if trusted else None
        if not isinstance(identity, str) or not identity.strip():
            identity = None
        if trusted and metadata.get("context_origin") == "internal_control":
            if identity is not None and turn_id is not None and identity != turn_id:
                raise ValueError("control_turn_mismatch: internal control does not match the current user turn")
            continue
        if identity is None or identity != turn_id:
            start = i + 1
        turn_id = identity
    return start



def deepseek_scope(model, base_url: str, options: dict | None = None):
    options = options or {}
    thinking = options.get("thinking")
    if (isinstance(thinking, dict) and thinking.get("type") == "disabled"
            or options.get("enable_thinking") is False):
        return None
    return (base_url.rstrip("/"), model) if uses_deepseek_protocol(model, base_url) else None


def model_deepseek_scope(model, options: dict | None = None):
    primary = getattr(model, "primary", model)
    base = getattr(primary, "bound", primary)
    options = {**getattr(primary, "kwargs", {}), **(options or {})}
    if getattr(base, "use_responses_api", None) is not False or options.get("use_responses_api") is True:
        return None
    body = options.get("extra_body", getattr(base, "extra_body", None)) or {}
    name = body.get("model", options.get("model", getattr(base, "model_name", "")))
    return deepseek_scope(name, str(getattr(base, "openai_api_base", "") or ""), {**options, **body})


def _call_ids(message):
    if isinstance(message, AIMessage):
        calls = message.tool_calls
    elif isinstance(message, dict) and message.get("role") == "assistant":
        calls = message.get("tool_calls") or []
    else:
        return ()
    if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
        return ()
    ids = tuple(call.get("id") for call in calls)
    return ids if all(isinstance(call_id, str) and call_id for call_id in ids) else ()


def _paired(messages, start=0):
    """Yield batches with all tool observations immediately after their proposal."""
    for i in range(start, len(messages)):
        message = messages[i]
        if isinstance(message, AIMessage) and message.invalid_tool_calls:
            continue
        ids = _call_ids(message)
        if not ids or len(set(ids)) != len(ids):
            continue
        observations = []
        for observation in messages[i + 1:i + 1 + len(ids)]:
            if isinstance(observation, ToolMessage):
                observations.append(observation.tool_call_id)
            elif isinstance(observation, dict) and observation.get("role") == "tool":
                observations.append(observation.get("tool_call_id"))
        if all(isinstance(item, str) for item in observations) and sorted(ids) == sorted(observations):
            yield i, ids


def current_tool_reasoning(messages, scope):
    if scope is None:
        return {}
    result, duplicate = {}, set()
    start = current_tool_loop_start(messages)
    if not start:
        return {}
    for i, ids in _paired(messages, start):
        if ids in result:
            duplicate.add(ids)
        message = messages[i]
        state = message.provider_protocol if isinstance(message, _DeepSeekMessage) else None
        if state is not None and state.scope == scope:
            result[ids] = state.reasoning
    return {ids: reasoning for ids, reasoning in result.items() if ids not in duplicate}


def attach_current_tool_reasoning(rows, replay):
    """Replay only an unambiguous batch with the exact same paired tool IDs."""
    if not replay:
        return False
    counts = Counter(call_id for row in rows for call_id in _call_ids(row))
    changed = False
    for i, ids in _paired(rows):
        if ids in replay and all(counts[call_id] == 1 for call_id in ids):
            rows[i]["reasoning_content"] = replay[ids]
            changed = True
    return changed


def thinking_rejected(status, body, description=""):
    if status is not None and status not in (400, 404, 422):
        return False
    detail = f"{description} {body or ''}".lower()
    if "reasoning_content" in detail:
        return False
    field = r"\b(?:thinking(?:\.type)?|enable_thinking|reasoning_effort)\b"
    error = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(error, dict) and re.fullmatch(field, str(error.get("param", ""))):
        if error.get("code") in {"unsupported_parameter", "unknown_parameter", "unrecognized_parameter"}:
            return True
    # Require the option itself to be rejected, not merely a nearby mention of
    # thinking mode in an unsupported model/image/tool error.
    return bool(re.search(
        r"(?:unsupported|unknown|unrecognized|unexpected)\s+"
        r"(?:(?:request|keyword|body)\s+)?(?:parameter|argument|field|option|key)"
        r"(?:\s+supplied)?[\s:'\"`=]*" + field, detail)
        or re.search(field + r"['\"`]*\s*(?:(?:parameter|argument|field|option)\s+)?"
                     r"(?:is\s+)?(?:unsupported|not supported|not permitted|not allowed)\b", detail)
        or re.search(field + r"['\"`:\s]*extra inputs are not permitted\b", detail))
