"""Public HTTPX boundary for the official DeepSeek Chat Completions wire field."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial

import httpx
from openai import DEFAULT_CONNECTION_LIMITS

from .provider_protocol import (
    _DeepSeekMessage, _ProtocolState, attach_current_tool_reasoning, current_tool_reasoning,
    deepseek_scope, model_deepseek_scope, thinking_rejected, uses_deepseek_protocol,
)


@dataclass(repr=False)
class _ReasoningAttempt:
    scope: tuple[str, str]
    replay: dict
    parts: list[str] = field(default_factory=list)
    terminal_seen: bool = False
    terminal_conflict: bool = False
    response: httpx.Response | None = None

    def protect(self, message):
        if not self.parts or not message.tool_calls or message.invalid_tool_calls:
            return message
        return _DeepSeekMessage(**message.model_dump(),
                                provider_protocol=_ProtocolState(self.scope, "".join(self.parts)))


_attempt = ContextVar[_ReasoningAttempt | None]("deepseek_reasoning_attempt", default=None)


@contextmanager
def _reasoning_attempt(model, messages, options=None):
    """Bind one existing SDK invocation, not the reusable model or client."""
    scope = model_deepseek_scope(model, options)
    evidence = _ReasoningAttempt(scope, current_tool_reasoning(messages, scope)) if scope is not None else None
    token = _attempt.set(evidence)
    try:
        yield evidence
    finally:
        _attempt.reset(token)


@contextmanager
def deepseek_attempt(model, messages, options=None):
    with _reasoning_attempt(model, messages, options) as attempt:
        try:
            yield attempt
        finally:
            if attempt is not None and attempt.response is not None and not attempt.response.is_closed:
                with suppress(httpx.HTTPError):
                    attempt.response.close()


@asynccontextmanager
async def async_deepseek_attempt(model, messages, options=None):
    with _reasoning_attempt(model, messages, options) as attempt:
        try:
            yield attempt
        finally:
            if attempt is not None and attempt.response is not None and not attempt.response.is_closed:
                with suppress(httpx.HTTPError):
                    await attempt.response.aclose()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _ProtocolWire:
    def __init__(self, base_url):
        self.base_url = str(base_url).rstrip("/")
        self.url = httpx.URL(self.base_url + "/chat/completions")
        self.official = uses_deepseek_protocol("deepseek-wire", self.base_url)

    def prepare(self, request):
        from .request_context import observe_provider_request

        official = request.method == "POST" and request.url == self.url and self.official
        attempt = None
        if official:
            payload = json.loads(request.content)
            effective = deepseek_scope(payload.get("model"), self.base_url, payload)
            current = _attempt.get()
            if current is not None and effective == current.scope:
                attempt = current
                rows = payload.get("messages")
                if isinstance(rows, list) and attach_current_tool_reasoning(rows, attempt.replay):
                    headers = request.headers.copy()
                    headers.pop("content-length", None)
                    request = httpx.Request(request.method, request.url, headers=headers,
                                            content=_dump(payload), extensions=request.extensions)
        observe_provider_request(request)
        return request, official, attempt


def _strip(value, attempt):
    if not isinstance(value, dict):
        return value
    if value.get("error"):
        return _safe_error_body(None, value)
    if not isinstance(value.get("choices"), list):
        if isinstance(value.get("chunk"), dict):
            _strip(value["chunk"], attempt)
        return value
    for position, choice in enumerate(value["choices"]):
        if not isinstance(choice, dict):
            continue
        selected = attempt if choice.get("index", position) == 0 else None
        for name in ("delta", "message"):
            item = choice.get(name)
            if isinstance(item, dict):
                reasoning = item.pop("reasoning_content", None)
                item.pop("reasoning", None)
                if selected is not None and isinstance(reasoning, str):
                    if selected.terminal_seen and reasoning:
                        selected.terminal_conflict = True
                    selected.parts.append(reasoning)
        if selected is not None and choice.get("finish_reason") is not None:
            selected.terminal_seen = True
    return value


class _SSERecord:
    def __init__(self, attempt):
        self.attempt = attempt
        self.lines = []

    def add(self, line):
        if line:
            self.lines.append(line)
            return None
        fields, data = [], []
        for field in self.lines:
            if field == "data" or field.startswith("data:"):
                data.append(field[5:].removeprefix(" "))
            else:
                fields.append(field)
        self.lines.clear()
        prefix = "".join(field + "\n" for field in fields).encode("utf-8")
        if not data:
            return prefix + b"\n"
        value = "\n".join(data)
        if value == "[DONE]":
            return prefix + b"data: [DONE]\n\n"
        try:
            return prefix + b"data: " + _dump(_strip(json.loads(value), self.attempt)) + b"\n\n"
        except (UnicodeError, ValueError, RecursionError):
            raise ValueError("invalid_deepseek_stream_event") from None


class _SyncSSE(httpx.SyncByteStream):
    def __init__(self, response, attempt):
        self.response, self.attempt = response, attempt
        self.events = self._events()

    def __iter__(self):
        # The SDK may drain the underlying byte stream after [DONE]. Keep the
        # same cursor even if the inner response happens to be buffered.
        return self.events

    def _events(self):
        parser = _SSERecord(self.attempt)
        for line in self.response.iter_lines():
            item = parser.add(line)
            if item is not None:
                yield item

    def close(self):
        self.response.close()


class _AsyncSSE(httpx.AsyncByteStream):
    def __init__(self, response, attempt):
        self.response, self.attempt = response, attempt
        self.events = self._events()

    def __aiter__(self):
        return self.events

    async def _events(self):
        parser = _SSERecord(self.attempt)
        async for line in self.response.aiter_lines():
            item = parser.add(line)
            if item is not None:
                yield item

    async def aclose(self):
        await self.response.aclose()


def _response_headers(response):
    headers = response.headers.copy()
    for field in ("content-encoding", "content-length", "transfer-encoding"):
        headers.pop(field, None)
    return headers


def _safe_json(response, attempt):
    try:
        return _dump(_strip(response.json(), attempt))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("invalid_deepseek_response") from None


def _safe_error_body(status, raw):
    """Never give provider error text or a replay echo to the SDK."""
    error = {"message": "DeepSeek Chat Completions request failed"}
    structured = raw.get("error", raw) if isinstance(raw, dict) else raw
    classification = ({"error": {k: structured.get(k) for k in ("message", "param", "code")}}
                      if isinstance(structured, dict) else structured)
    if thinking_rejected(status, classification):
        param = structured.get("param") if isinstance(structured, dict) else None
        code = structured.get("code") if isinstance(structured, dict) else None
        error.update(
            message="DeepSeek rejected a thinking option",
            param=param if param in ("thinking", "thinking.type", "enable_thinking", "reasoning_effort") else "thinking",
            code=code if code in ("unsupported_parameter", "unknown_parameter", "unrecognized_parameter") else "unsupported_parameter",
        )
    return {"error": error}


def _safe_error(response):
    try:
        raw = response.json()
    except (UnicodeError, ValueError, RecursionError):
        raw = response.text
    return httpx.Response(response.status_code, json=_safe_error_body(response.status_code, raw))


def _sse(response):
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/event-stream"


# The OpenAI SDK can default to httpx2. Use the same public HTTPX carrier
# for the outer client, the transform, and the environment-aware delegate.
_OPENAI_LIMITS = httpx.Limits(
    max_connections=DEFAULT_CONNECTION_LIMITS.max_connections,
    max_keepalive_connections=DEFAULT_CONNECTION_LIMITS.max_keepalive_connections,
    keepalive_expiry=DEFAULT_CONNECTION_LIMITS.keepalive_expiry,
)


class DeepSeekTransport(httpx.BaseTransport):
    def __init__(self, base_url, inner=None):
        self.wire = _ProtocolWire(base_url)
        if inner is None:
            # A stock client keeps HTTPX's environment proxy and TLS routing.
            # It has no custom transport and never follows redirects.
            default = httpx.Client(follow_redirects=False, limits=_OPENAI_LIMITS)
            self._send = partial(default.send, stream=True, follow_redirects=False)
            self._close = default.close
        else:
            self._send = inner.handle_request
            self._close = inner.close

    def handle_request(self, request):
        original = request
        request, official, attempt = self.wire.prepare(request)
        try:
            response = self._send(request)
        except httpx.RequestError as error:
            if not official:
                raise
            raise type(error)("DeepSeek transport failed", request=original) from None
        if not official:
            return response
        response.request = original
        if not response.is_success:
            try:
                response.read()
                return _safe_error(response)
            finally:
                response.close()
        if _sse(response):
            body = {"stream": _SyncSSE(response, attempt)}
        else:
            try:
                response.read()
                body = {"content": _safe_json(response, attempt)}
            finally:
                response.close()
        result = httpx.Response(response.status_code, headers=_response_headers(response),
                                extensions=response.extensions, **body)
        if attempt is not None and _sse(response):
            attempt.response = result
        return result

    def close(self):
        self._close()


class AsyncDeepSeekTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url, inner=None):
        self.wire = _ProtocolWire(base_url)
        if inner is None:
            default = httpx.AsyncClient(follow_redirects=False, limits=_OPENAI_LIMITS)
            self._send = partial(default.send, stream=True, follow_redirects=False)
            self._close = default.aclose
        else:
            self._send = inner.handle_async_request
            self._close = inner.aclose

    async def handle_async_request(self, request):
        original = request
        request, official, attempt = self.wire.prepare(request)
        try:
            response = await self._send(request)
        except httpx.RequestError as error:
            if not official:
                raise
            raise type(error)("DeepSeek transport failed", request=original) from None
        if not official:
            return response
        response.request = original
        if not response.is_success:
            try:
                await response.aread()
                return _safe_error(response)
            finally:
                await response.aclose()
        if _sse(response):
            body = {"stream": _AsyncSSE(response, attempt)}
        else:
            try:
                await response.aread()
                body = {"content": _safe_json(response, attempt)}
            finally:
                await response.aclose()
        result = httpx.Response(response.status_code, headers=_response_headers(response),
                                extensions=response.extensions, **body)
        if attempt is not None and _sse(response):
            attempt.response = result
        return result

    async def aclose(self):
        await self._close()
