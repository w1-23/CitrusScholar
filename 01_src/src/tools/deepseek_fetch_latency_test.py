"""Offline regressions for the foreground web fetch and one-paper deadline."""
import asyncio
from contextvars import ContextVar
from hashlib import md5
import json
from time import monotonic

import httpx

import pytest
import requests

from src.core import capability_policy, tracing, web_failure_ledger
from src.shared_corpus import pmc
from src.tools import deepseek_search_fetch as fetch

PAGE = "https://example.org/article"
PMC = "https://pmc.ncbi.nlm.nih.gov/articles/PMC100/"
DOI = "10.5555/fetch-fixture"


def response(status: int) -> requests.Response:
    reply = requests.Response()
    reply.status_code = status
    reply._content = b'{"error":{"message":"synthetic gateway response"}}'
    return reply


@pytest.fixture
def offline_fetch(monkeypatch):
    monkeypatch.setattr(fetch, "_enabled", lambda _: True)
    monkeypatch.setattr(fetch, "_headers", lambda: {"Authorization": "test-only"})
    monkeypatch.setattr(fetch, "_legacy_corpus_enabled", lambda: False)
    monkeypatch.setattr(fetch, "_web_citation_ref", lambda: "W1")
    dns = []
    submissions = []

    def resolve(host, *_args, **_kwargs):
        dns.append(host)
        assert host in {"example.org", "pmc.ncbi.nlm.nih.gov", "doi.org", "www.ebi.ac.uk"}
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch, "_queue_paper_registration", lambda *args, **kwargs: submissions.append((args, kwargs)))
    monkeypatch.setattr(web_failure_ledger, "record_fetch_failures", lambda *args, **kwargs: None)
    return dns, submissions


@pytest.mark.parametrize("status", [307, 402, 404, 429, 500, 502, 503, 504])
def test_generic_fetch_returns_first_provider_status(monkeypatch, offline_fetch, status):
    posts = []

    def post(url, *, headers, json, timeout, allow_redirects):
        posts.append(json["url"])
        assert url == "https://api.deepseek.com/v1/tools/fetch"
        assert headers == {"Authorization": "test-only"} and timeout == fetch._fetch_timeout()
        assert allow_redirects is False
        return response(status)

    monkeypatch.setattr(requests, "post", post)
    message, result = fetch.deepseek_fetch.func(url=PAGE)
    assert f"HTTP {status}" in message and result["web_results"] == []
    assert posts == [PAGE] and offline_fetch[0] == ["example.org"]


@pytest.mark.parametrize("error", [requests.ReadTimeout, requests.ConnectionError])
def test_generic_fetch_does_not_repeat_network_exceptions(monkeypatch, offline_fetch, error):
    posts = []

    def post(_url, *, json, **_kwargs):
        posts.append(json["url"])
        raise error("synthetic transport error")

    monkeypatch.setattr(requests, "post", post)
    message, result = fetch.deepseek_fetch.func(url=PAGE)
    assert "[WEB_TOOL_ERROR]" in message and result["web_results"] == []
    assert posts == [PAGE]


def run_paper(**kwargs):
    return asyncio.run(fetch.deepseek_fetch_paper.coroutine(**kwargs))


def bind_async_http(monkeypatch, handler):
    original = httpx.AsyncClient
    def make_client(**kwargs):
        limits = kwargs.get("limits")
        assert limits.max_connections == fetch._RACE_PARALLEL == 2
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(fetch.httpx, "AsyncClient", make_client)


def paper_body(lead=""):
    return lead + "\n# Introduction\n\n" + "Verified experimental results. " * 80 + "\n# Results\n\nEnd."


def test_paper_does_not_post_when_dns_uses_remaining_budget(monkeypatch, offline_fetch):
    at = [100.0]
    monkeypatch.setattr(fetch.time, "monotonic", lambda: at[0])

    def resolve(host, *_args, **_kwargs):
        assert host == "example.org"
        at[0] = 101.0
        return [(None, None, None, None, ("8.8.8.8", 443))]

    class NoProvider:
        async def post(self, *args, **kwargs):
            pytest.fail("POST after the deadline")

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)
    result = asyncio.run(fetch._fetch_paper_candidate(NoProvider(), PAGE, "search_result", 101.0))
    assert result["error"] == "race_deadline_exceeded"


def test_paper_gets_remaining_budget_and_does_not_repeat_504(monkeypatch, offline_fetch):
    at = [100.0]
    monkeypatch.setattr(fetch.time, "monotonic", lambda: at[0])
    monkeypatch.setattr(fetch, "_public_http_url_error", lambda _: at.__setitem__(0, 101.0))
    posts = []

    class Provider:
        async def post(self, _url, *, json, timeout, follow_redirects, **_kwargs):
            posts.append((json["url"], timeout))
            assert not follow_redirects
            return httpx.Response(504, json={"error": {"message": "synthetic failure"}})

    result = asyncio.run(fetch._fetch_paper_candidate(Provider(), PAGE, "search_result", 101.5))
    assert "HTTP 504" in result["error"] and posts == [(PAGE, 0.5)]


def test_paper_all_blocked_keeps_no_provider_or_cloud_request(monkeypatch, offline_fetch):
    bind_async_http(monkeypatch, lambda *_: pytest.fail("HTTP for private target"))
    message, result = run_paper(urls=["http://127.0.0.1/a"])
    assert message.startswith("[WEB_FETCH_BLOCKED]") and result["web_results"] == []
    assert not offline_fetch[0] and not offline_fetch[1]


def test_explicit_no_network_blocks_both_fetches_before_dns_or_handoff(monkeypatch):
    for target, name in ((fetch.socket, "getaddrinfo"), (requests, "post"),
                         (httpx, "AsyncClient"), (fetch, "_queue_paper_registration")):
        monkeypatch.setattr(target, name, lambda *a, **k: pytest.fail("network or handoff with no permission"))
    token = tracing._web_search_enabled.set(True)
    try:
        with capability_policy.bind_policy(capability_policy.CapabilityPolicy(external_data_network=False)):
            for invoke, args in ((fetch.deepseek_fetch.func, {"url": PAGE}), (run_paper, {"doi": DOI})):
                message, artifact = invoke(**args)
                assert "DISABLED" in message and artifact["_tool_outcome"]["status"] == "not_run"
    finally:
        tracing._web_search_enabled.reset(token)


def test_paper_valid_winner_cancels_running_loser_and_skips_queue(monkeypatch, offline_fetch):
    calls = []
    peak, active = 0, 0
    entered = cancelled = None

    async def reply(request):
        nonlocal peak, active
        assert str(request.url) == fetch._DEEPSEEK_FETCH_URL and request.method == "POST"
        target = json.loads(request.content)["url"]
        calls.append(target)
        active += 1
        peak = max(peak, active)
        try:
            if target.endswith("/loser"):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            await entered.wait()
            return httpx.Response(200, json={"title": "Article", "content": paper_body("Winner only")})
        finally:
            active -= 1

    async def run():
        nonlocal entered, cancelled
        entered, cancelled = asyncio.Event(), asyncio.Event()
        message, result = await fetch.deepseek_fetch_paper.coroutine(urls=[PAGE + "/loser", PAGE, PAGE + "/queued"])
        assert cancelled.is_set(), "in-flight HTTP must actually be cancelled before return"
        assert calls == [PAGE + "/loser", PAGE] or calls == [PAGE, PAGE + "/loser"]
        assert peak == 2 and active == 0
        assert "Winner only" in message and len(result["web_results"]) == 1
        assert {row.get("error") for row in result["fetch_attempts"]} >= {
            "cancelled_after_winner", "not_started_after_winner"}

    bind_async_http(monkeypatch, reply)
    asyncio.run(run())


def test_paper_does_not_expand_mismatched_or_ambiguous_metadata(monkeypatch, offline_fetch):
    calls = []
    url = "https://www.ebi.ac.uk/"
    async def respond(request):
        target = json.loads(request.content)["url"]
        calls.append(target)
        if target.startswith(url):
            records = [dict(pmcid="PMC1", doi="10.5555/wrong", pmid="7", title="Same title"),
                       dict(pmcid="PMC2", doi=DOI, pmid="8", title="Same title")]
            return httpx.Response(200, json={"content": json.dumps({"resultList": {"result": records}})})
        return httpx.Response(404)
    bind_async_http(monkeypatch, respond)
    for kwargs in (dict(doi=DOI, pmid="7"), dict(title="Same title")):
        _, result = run_paper(**kwargs)
        assert result["fetch_status"] == "failed"
    assert len(calls) == 3
    assert not any("fullTextXML" in target or "/articles/PMC" in target for target in calls)


def test_paper_verified_pmc_body_passes_exact_bytes_to_handoff_only(monkeypatch, offline_fetch):
    raw = paper_body(DOI).encode() + b"\n"
    checksum = md5(raw, usedforsecurity=False).hexdigest()
    meta = dict(pmcid="PMC100", version=1, doi=DOI, pmid=7, title="A verified paper",
                is_retracted=False, is_pmc_openaccess=True, license_code="CC BY",
                text_url=f"s3://pmc-oa-opendata/PMC100.1/PMC100.1.txt?md5={checksum}")
    metadata = json.dumps(meta).encode()
    scope = ContextVar("test_fetch_scope", default=False)
    scope.set(True)
    cloud, post = [], []

    async def respond(request):
        assert scope.get()
        if request.method == "POST":
            post.append(json.loads(request.content)["url"])
            return httpx.Response(404)
        assert str(request.url).startswith(pmc._BASE + "/")
        budget = request.extensions["timeout"]["read"]
        assert 0 < budget <= min(3, fetch._RACE_DEADLINE_SECONDS)
        cloud.append(request.url.path)
        if request.url.path.endswith(".json"):
            return httpx.Response(200, content=metadata)
        assert request.url.path == "/PMC100.1/PMC100.1.txt"
        return httpx.Response(200, content=raw)

    bind_async_http(monkeypatch, respond)
    token = tracing._web_search_enabled.set(True)
    try:
        message, result = run_paper(doi=DOI, urls=[PMC.rstrip("/") + ".1/"])
    finally:
        tracing._web_search_enabled.reset(token)
    assert "Verified PMC Cloud source" in message and result["fetch_status"] == "full_text"
    assert result["fetch_stats"]["won"] == pmc._BASE + "/PMC100.1/PMC100.1.txt"
    assert len(result["web_results"]) == 1 and len(post) == len(set(post))
    assert cloud == ["/metadata/PMC100.1.json", "/PMC100.1/PMC100.1.txt"]
    assert "raw_bytes" not in str(result) and "metadata_raw" not in str(result)
    args, kwargs = offline_fetch[1][0]
    assert args[:2] == (DOI, "7") and args[2]["validity"] == "valid"
    assert kwargs["official"][0]["raw_bytes"] == raw and kwargs["official"][0]["metadata_raw"] == metadata


@pytest.mark.parametrize("abandon", ["timeout", "host_cancel"])
def test_paper_stops_cloud_request_on_shared_timeout_or_host_cancel(monkeypatch, offline_fetch, abandon):
    entering = stopped = None
    paths = []

    async def respond(request):
        if request.method == "POST":
            return httpx.Response(404)
        paths.append(request.url.path)
        entering.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def run():
        nonlocal entering, stopped
        entering, stopped = asyncio.Event(), asyncio.Event()
        started = monotonic()
        task = asyncio.create_task(fetch.deepseek_fetch_paper.coroutine(urls=[PMC.rstrip("/") + ".1/"]))
        await asyncio.wait_for(entering.wait(), timeout=0.6)
        if abandon == "timeout":
            _, result = await task
            assert result["fetch_status"] == "failed" and monotonic() - started < 0.9
            assert [item["error"] for item in result["fetch_attempts"]][-1] == "race_deadline_exceeded"
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not offline_fetch[1]
        assert stopped.is_set() and paths == ["/metadata/PMC100.1.json"]

    monkeypatch.setattr(fetch, "_RACE_DEADLINE_SECONDS", 0.35)
    bind_async_http(monkeypatch, respond)
    token = tracing._web_search_enabled.set(True)
    try:
        asyncio.run(run())
    finally:
        tracing._web_search_enabled.reset(token)


def test_registry_token_actually_cancels_waiting_provider_socket(monkeypatch, offline_fetch):
    from contextvars import copy_context
    from src.core.cancel import CancelToken, ToolCancelled, bound_token
    from src.tools.registry import _invoke_tool_with_ctx

    started = stopped = None

    async def respond(request):
        assert request.method == "POST"
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def run():
        nonlocal started, stopped
        started, stopped = asyncio.Event(), asyncio.Event()
        token = CancelToken()
        with bound_token(token):
            call = {"type": "tool_call", "name": fetch.deepseek_fetch_paper.name,
                    "id": "fixture-paper", "args": {"urls": [PAGE]}}
            task = asyncio.create_task(_invoke_tool_with_ctx(copy_context(), fetch.deepseek_fetch_paper, call))
        await asyncio.wait_for(started.wait(), 1)
        token.cancel()
        with pytest.raises(ToolCancelled):
            await asyncio.wait_for(task, 1)
        assert stopped.is_set() and not offline_fetch[1]

    bind_async_http(monkeypatch, respond)
    asyncio.run(run())


def test_late_dns_never_posts_even_after_cancellation(monkeypatch, offline_fetch):
    import threading

    entered, release, finished = (threading.Event() for _ in range(3))
    def resolve(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        finished.set()
        return [(None, None, None, None, ("8.8.8.8", 443))]

    def post(*_args):
        pytest.fail("a cancelled DNS candidate must never send a provider request")

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch, "_RACE_DEADLINE_SECONDS", 0.2)
    bind_async_http(monkeypatch, post)
    try:
        _, result = run_paper(urls=[PAGE])
        assert entered.is_set() and not finished.is_set()
        assert result["fetch_status"] == "failed"
    finally:
        release.set()
        assert finished.wait(1)
