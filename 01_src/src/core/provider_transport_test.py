"""Pinned SDK parity at the Chat Completions boundary, without any provider traffic."""
import asyncio
import gzip
import json
import os
import socket
import socketserver
import threading
import unittest
from unittest.mock import patch

import httpx
import pytest
from openai import APIError, BadRequestError, DefaultAsyncHttpxClient, DefaultHttpxClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, ToolMessage, messages_to_dict
from langchain_openai import ChatOpenAI
from langsmith import tracing_context

from .provider_protocol import _DeepSeekMessage, _ProtocolState
from .llm_pool import _new_client
from .provider_protocol import thinking_rejected
from .provider_transport import AsyncDeepSeekTransport, DeepSeekTransport, async_deepseek_attempt, deepseek_attempt

BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-fixture"
HIDDEN = "mock-reasoning-证据"
OTHER = "mock-reasoning-不同"


class _Trace(BaseCallbackHandler):
    raise_error = True

    def __init__(self):
        self.records = []
        self.chunk_count = 0

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.records.extend(messages_to_dict(batch) for batch in messages)

    def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        self.records.append(chunk)
        self.chunk_count += 1

    def on_llm_end(self, response, **kwargs):
        self.records.append(response)

    def on_llm_error(self, error, **kwargs):
        self.records.append(error)
        if isinstance(error, BadRequestError):
            self.records.extend((error.body, error.request.content, error.response.request.content))


class _Split(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw
        self.closed = False

    def __iter__(self):
        for i in range(0, len(self.raw), 5):
            yield self.raw[i:i + 5]

    async def __aiter__(self):
        for fragment in self:
            await asyncio.sleep(0)
            yield fragment

    def close(self):
        self.closed = True

    async def aclose(self):
        self.close()


def _json(value):
    return json.dumps(value, ensure_ascii=False).encode()


def _call(call_id):
    return {"id": call_id, "type": "function", "function": {"name": "fixture", "arguments": "{}"}}


class ProviderWireTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests, self.streams = [], []

    def respond(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), body))
        base = {"id": "fixture", "model": MODEL, "created": 1}
        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        last = body["messages"][-1]
        marker = OTHER if last["content"] == "other" else HIDDEN
        if not body.get("stream"):
            response = {"role": "assistant", "content": "", "reasoning_content": marker,
                        "tool_calls": [_call("one"), _call("two")]}
            return httpx.Response(200, json={**base, "object": "chat.completion", "usage": usage,
                "choices": [{"index": 0, "message": response, "finish_reason": "tool_calls"}]})
        frames = [({"role": "assistant", "reasoning_content": marker[:5]}, None),
                  ({"reasoning_content": marker[5:], "tool_calls": [
                      {"index": index, **_call(call_id)} for index, call_id in enumerate(("one", "two"))]}, None)]
        if last["content"] != "truncate":
            frames.append(({}, "tool_calls"))
        if last["content"] == "late reasoning":
            frames.append(({"reasoning_content": marker}, None))
        root = {**base, "object": "chat.completion.chunk"}
        rows = [{**root, "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                for delta, reason in frames]
        rows.append({**root, "choices": [], "usage": usage})
        raw = b": fixture comment\r\n\r\n"
        for index, row in enumerate(rows):
            eol = b"\r\n" if index % 2 else b"\n"
            line = _json(row)
            split = line.index(b",") + 1
            raw += b"data: " + line[:split] + eol + b"data: " + line[split:] + eol * 2
        stream = _Split(gzip.compress(raw + b"data: [DONE]\n\n"))
        self.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
                              stream=stream)

    def model(self, *, base_url=BASE, **kwargs):
        return ChatOpenAI(model=MODEL, base_url=base_url, use_responses_api=False, api_key="fixture",
                          stream_usage=True, max_retries=0, cache=False, **kwargs)

    def assert_public(self, value):
        self.assertNotIn("mock-reasoning-", repr(value))
        self.assertNotIn("reasoning_content", repr(value))

    def test_sync_invoke_and_stream_callbacks_never_see_reasoning(self):
        with httpx.Client(transport=DeepSeekTransport(BASE, httpx.MockTransport(self.respond))) as client:
            model, cb = self.model(http_client=client), _Trace()
            messages = [HumanMessage("fixture")]
            with tracing_context(enabled=False, parent=False):
                with deepseek_attempt(model, messages) as evidence:
                    answer = model.invoke(messages, config={"callbacks": [cb]})
                self.assertEqual(evidence.parts, [HIDDEN])
                self.assertEqual(answer.usage_metadata["total_tokens"], 5)
                private = evidence.protect(answer)
                self.assertEqual(private.provider_protocol.reasoning, HIDDEN)
                self.assert_public([messages_to_dict([private]), private, cb.records])
                with deepseek_attempt(model, messages) as evidence:
                    chunks = list(model.stream(messages, config={"callbacks": [cb]}))
                self.assertEqual("".join(evidence.parts), HIDDEN)
                self.assertFalse(evidence.terminal_conflict)
                self.assertTrue(self.streams[-1].closed)
                self.assertGreater(cb.chunk_count, 0)
                self.assert_public([chunks, cb.records])

    async def test_fragmented_sse_events_terminal_usage_and_early_close(self):
        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(self.respond))) as client:
            model, cb = self.model(http_async_client=client), _Trace()
            with tracing_context(enabled=False, parent=False):
                for text, conflict, terminal in (("fixture", False, True), ("truncate", False, False),
                                                 ("late reasoning", True, True)):
                    messages = [HumanMessage(text)]
                    with self.subTest(text=text):
                        async with async_deepseek_attempt(model, messages) as evidence:
                            chunks = [chunk async for chunk in model.astream(messages, config={"callbacks": [cb]})]
                            self.assertEqual(evidence.terminal_seen, terminal)
                            self.assertEqual(evidence.terminal_conflict, conflict)
                            counts = [chunk.usage_metadata["total_tokens"] for chunk in chunks if chunk.usage_metadata]
                            self.assertEqual(counts, [5])
                            self.assert_public([chunks, cb.records])
                async with async_deepseek_attempt(model, messages) as evidence:
                    events = [event async for event in model.astream_events(messages, version="v2")]
                    self.assert_public(events)
                    self.assertGreater(len(events), 2)
                    self.assertTrue(evidence.parts)
                self.assertGreater(cb.chunk_count, 0)
                async with async_deepseek_attempt(model, messages):
                    stream = model.astream(messages)
                    await anext(stream)
                    await stream.aclose()
                got_chunk, hold = asyncio.Event(), asyncio.Event()

                async def cancelled_call():
                    async with async_deepseek_attempt(model, messages):
                        async for _ in model.astream(messages):
                            got_chunk.set()
                            await hold.wait()

                task = asyncio.create_task(cancelled_call())
                await asyncio.wait_for(got_chunk.wait(), timeout=3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(all(stream.closed for stream in self.streams))

    async def test_egress_only_for_exact_official_same_turn_pair(self):
        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(self.respond)),
                                     follow_redirects=False) as client:
            model = self.model(http_async_client=client)
            user = HumanMessage("fixture")
            a, b = ToolMessage("result", tool_call_id="one"), ToolMessage("result", tool_call_id="two")
            private = _DeepSeekMessage("", tool_calls=[{"name": "fixture", "args": {}, "id": i} for i in ("one", "two")],
                                       provider_protocol=_ProtocolState((BASE, MODEL), HIDDEN))
            pair = [user, private, a, b]
            cases = [
                ("paired", pair, BASE, {}, True), ("old turn", [*pair, HumanMessage("other")], BASE, {}, False),
                ("no user boundary", pair[1:], BASE, {}, False),
                ("missing result", pair[:-1], BASE, {}, False), ("wrong ID", [user, private, a, a], BASE, {}, False),
                ("duplicate", [*pair, private, a, b], BASE, {}, False),
                ("ambiguous old turn", [*pair, HumanMessage("again"), private, a, b], BASE, {}, False),
                ("model override", pair, BASE, {"model": "deepseek-other"}, False),
                ("disabled", pair, BASE, {"extra_body": {"thinking": {"type": "disabled"}}}, False),
                ("legacy disabled", pair, BASE, {"extra_body": {"enable_thinking": False}}, False),
                ("gateway", pair, "https://gateway.invalid/v1", {}, False),
                ("wrong path", pair, "https://api.deepseek.com/beta", {}, False),
                ("wrong port", pair, "https://api.deepseek.com:444/v1", {}, False),
                ("wrong scheme", pair, "http://api.deepseek.com/v1", {}, False),
            ]
            for title, messages, base_url, options, expected in cases:
                with self.subTest(case=title):
                    async with async_deepseek_attempt(model, messages):
                        target = model if base_url == BASE else self.model(base_url=base_url, http_async_client=client)
                        answer = await target.ainvoke(messages, **options)
                        self.assert_public(answer)
                        outgoing = self.requests[-1][1]["messages"]
                        self.assertEqual([(i, m["reasoning_content"]) for i, m in enumerate(outgoing) if "reasoning_content" in m],
                                         [(1, HIDDEN)] if expected else [])

    async def test_same_or_separate_models_with_shared_pool_do_not_cross_attempts(self):
        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(self.respond))) as client:
            first = self.model(http_async_client=client)

            async def invoke(model, text):
                messages = [HumanMessage(text)]
                async with async_deepseek_attempt(model, messages) as evidence:
                    chunks = [c async for c in model.astream(messages)]
                self.assert_public(chunks)
                return "".join(evidence.parts)

            for second in (first, self.model(http_async_client=client)):
                self.assertEqual(await asyncio.gather(invoke(first, "fixture"), invoke(second, "other")), [HIDDEN, OTHER])

    async def test_provider_error_keeps_classification_without_echo_or_rewritten_request(self):
        private = _DeepSeekMessage("", tool_calls=[{"name": "fixture", "args": {}, "id": "one"}],
                                   provider_protocol=_ProtocolState((BASE, MODEL), HIDDEN))
        messages = [HumanMessage("fixture"), private, ToolMessage("result", tool_call_id="one")]
        for fallback, parameter in ((True, "thinking"), (False, "reasoning_content")):
            def respond(request):
                payload = json.loads(request.content)
                self.assertEqual(payload["messages"][1]["reasoning_content"], HIDDEN)
                return httpx.Response(400, json={"error": {
                    "message": "Unsupported option: " + parameter + " " + HIDDEN,
                    "param": parameter, "code": "unsupported_parameter", "echo": payload,
                }}, headers={"x-fake-error-echo": HIDDEN.replace("证据", "")})

            async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(respond))) as client:
                model, cb = self.model(http_async_client=client), _Trace()
                async with async_deepseek_attempt(model, messages):
                    with self.assertRaises(BadRequestError) as caught:
                        await model.ainvoke(messages, config={"callbacks": [cb]})
                error = caught.exception
                self.assertTrue(any(isinstance(item, BadRequestError) for item in cb.records))
                self.assertEqual(thinking_rejected(error.status_code, error.body), fallback)
                self.assert_public([cb.records, str(error), error.response.text, error.response.headers,
                                    error.request.content, error.response.request.content])

    async def test_stream_error_event_is_sanitized_before_sdk_error_callback(self):
        def respond(request):
            raw = b"data: " + _json({"error": {"message": HIDDEN, "reasoning_content": HIDDEN}}) + b"\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Split(raw))

        async with httpx.AsyncClient(transport=AsyncDeepSeekTransport(BASE, httpx.MockTransport(respond))) as client:
            model, cb = self.model(http_async_client=client), _Trace()
            messages = [HumanMessage("fixture")]
            async with async_deepseek_attempt(model, messages):
                with self.assertRaises(APIError) as caught:
                    _ = [chunk async for chunk in model.astream(messages, config={"callbacks": [cb]})]
            self.assertTrue(any(isinstance(item, APIError) for item in cb.records))
            self.assert_public([cb.records, str(caught.exception), caught.exception.body])

    @pytest.mark.allow_hosts(["127.0.0.1"])
    async def test_default_route_honors_proxy_and_no_proxy_without_live_network(self):
        connects = []

        class Proxy(socketserver.StreamRequestHandler):
            def handle(self):
                connects.append(self.rfile.readline(256))
                self.wfile.write(b"HTTP/1.1 502 Fixture\r\nContent-Length: 0\r\n\r\n")
                self.wfile.flush()

        body = {"model": MODEL, "messages": [{"role": "user", "content": "fixture"}]}
        with socketserver.TCPServer(("127.0.0.1", 0), Proxy) as proxy:
            worker = threading.Thread(target=proxy.serve_forever, daemon=True)
            worker.start()
            address = f"http://127.0.0.1:{proxy.server_address[1]}"
            route = {"HTTPS_PROXY": address, "https_proxy": address, "ALL_PROXY": address, "all_proxy": address}
            bypass = {"NO_PROXY": "", "no_proxy": ""}

            async def send_both(error):
                model = _new_client(MODEL, "fixture", BASE, 0.0, 2, max_tokens=None)
                self.assertIs(type(model.http_client), httpx.Client)
                self.assertIs(type(model.http_async_client), httpx.AsyncClient)
                with model.http_client as sync_client:
                    async with model.http_async_client as async_client:
                        with self.assertRaises(error):
                            await asyncio.to_thread(sync_client.post, BASE + "/chat/completions", json=body, timeout=2)
                        with self.assertRaises(error):
                            await async_client.post(BASE + "/chat/completions", json=body, timeout=2)

            try:
                with patch.dict(os.environ, {**route, **bypass}):
                    await send_both(httpx.ProxyError)
                dns_calls = []

                def refuse_dns(host, *args, **kwargs):
                    dns_calls.append(host)
                    raise socket.gaierror("fixture does not permit network DNS")

                with patch.dict(os.environ, {**route, "NO_PROXY": "api.deepseek.com", "no_proxy": "api.deepseek.com"}), \
                        patch.object(socket, "getaddrinfo", refuse_dns):
                    await send_both(httpx.ConnectError)
            finally:
                await asyncio.to_thread(proxy.shutdown)
                worker.join(timeout=2)
        self.assertEqual(connects, [b"CONNECT api.deepseek.com:443 HTTP/1.1\r\n"] * 2)
        self.assertTrue(dns_calls)
        self.assertTrue(all(host in ("api.deepseek.com", b"api.deepseek.com") for host in dns_calls))

    async def test_production_pool_uses_public_sdk_with_matching_transport_carrier(self):
        extensions = []

        def seen(request):
            extensions.append(request.extensions["timeout"])
            return self.respond(request)

        def sync(base):
            return DeepSeekTransport(base, httpx.MockTransport(seen))

        def async_(base):
            return AsyncDeepSeekTransport(base, httpx.MockTransport(seen))

        with patch("src.core.provider_transport.DeepSeekTransport", sync), \
                patch("src.core.provider_transport.AsyncDeepSeekTransport", async_):
            model = _new_client(MODEL, "fixture", BASE, 0.0, 2, max_tokens=None)
        with model.http_client:
            async with model.http_async_client:
                self.assertIs(type(model), ChatOpenAI)
                self.assertIs(model.use_responses_api, False)
                messages = [HumanMessage("fixture")]
                sync_answer = await asyncio.to_thread(model.invoke, messages)
                async with async_deepseek_attempt(model, messages) as attempt:
                    async_answer = await model.ainvoke(messages)
                self.assertEqual(attempt.parts, [HIDDEN])
                self.assert_public([sync_answer, async_answer])
        self.assertEqual([current["read"] for current in extensions], [2, 2])
        self.assertEqual([url for url, _ in self.requests], [BASE + "/chat/completions"] * 2)

        gateway = _new_client(MODEL, "fixture", "https://gateway.invalid/v1", 0.0, 2, max_tokens=None)
        with gateway.http_client:
            async with gateway.http_async_client:
                self.assertIs(type(gateway.http_client), DefaultHttpxClient)
                self.assertIs(type(gateway.http_async_client), DefaultAsyncHttpxClient)
                self.assertIs(gateway.use_responses_api, False)
