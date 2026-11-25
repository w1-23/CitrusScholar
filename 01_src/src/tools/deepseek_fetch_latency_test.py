"""Offline regressions for the foreground web fetch and one-paper deadline."""
from contextvars import ContextVar
import threading
from time import monotonic

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
    monkeypatch.setattr(fetch, "_queue_paper_registration", lambda *args: submissions.append(args))
    monkeypatch.setattr(web_failure_ledger, "record_fetch_failures", lambda *args, **kwargs: None)
    return dns, submissions


@pytest.mark.parametrize("status", [402, 404, 429, 500, 502, 503, 504])
def test_generic_fetch_returns_first_provider_status(monkeypatch, offline_fetch, status):
    posts = []

    def post(url, *, headers, json, timeout):
        posts.append(json["url"])
        assert url == "https://api.deepseek.com/v1/tools/fetch"
        assert headers == {"Authorization": "test-only"} and timeout == fetch._fetch_timeout()
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


def test_paper_does_not_post_when_dns_uses_remaining_budget(monkeypatch, offline_fetch):
    at = [100.0]
    monkeypatch.setattr(fetch.time, "monotonic", lambda: at[0])

    def resolve(host, *_args, **_kwargs):
        assert host == "example.org"
        at[0] = 101.0
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("POST after the deadline"))
    result = fetch._fetch_paper_candidate(PAGE, "search_result", 101.0)
    assert result["error"] == "race_deadline_exceeded"


def test_paper_gets_remaining_budget_and_does_not_repeat_504(monkeypatch, offline_fetch):
    at = [100.0]
    monkeypatch.setattr(fetch.time, "monotonic", lambda: at[0])
    monkeypatch.setattr(fetch, "_public_http_url_error", lambda _: at.__setitem__(0, 101.0))
    posts = []

    def post(_url, *, json, timeout, **_kwargs):
        posts.append((json["url"], timeout))
        return response(504)

    monkeypatch.setattr(requests, "post", post)
    result = fetch._fetch_paper_candidate(PAGE, "search_result", 101.5)
    assert "HTTP 504" in result["error"] and posts == [(PAGE, 0.5)]


def test_paper_all_blocked_keeps_no_provider_or_cloud_request(monkeypatch, offline_fetch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("POST for private target"))
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("GET for private target"))
    message, result = fetch.deepseek_fetch_paper.func(urls=["http://127.0.0.1/a"])
    assert message.startswith("[WEB_FETCH_BLOCKED]") and result["web_results"] == []
    assert not offline_fetch[0]


def test_explicit_no_network_blocks_both_fetches_before_dns_or_handoff(monkeypatch):
    for target, name in ((fetch.socket, "getaddrinfo"), (requests, "post"), (requests, "get"),
                         (fetch, "_queue_paper_registration")):
        monkeypatch.setattr(target, name, lambda *a, **k: pytest.fail("network or handoff with no permission"))
    token = tracing._web_search_enabled.set(True)
    try:
        with capability_policy.bind_policy(capability_policy.CapabilityPolicy(external_data_network=False)):
            for tool in (fetch.deepseek_fetch, fetch.deepseek_fetch_paper):
                args = {"url": PAGE} if tool is fetch.deepseek_fetch else {"doi": DOI, "urls": [PMC]}
                message, artifact = tool.func(**args)
                assert "DISABLED" in message and artifact["_tool_outcome"]["status"] == "not_run"
    finally:
        tracing._web_search_enabled.reset(token)


def test_paper_fallback_uses_remaining_time_and_keeps_handoff(monkeypatch, offline_fetch):
    posts = []

    def post(_url, *, json, **_kwargs):
        posts.append(json["url"])
        return response(404)

    scope = ContextVar("test_fetch_scope", default=False)
    scope.set(True)
    reader_calls = []

    def official(url, *, doi, pmid, max_bytes, deadline):
        assert scope.get() and url == PMC and doi == DOI and max_bytes > 4000
        reader_calls.append(deadline - monotonic())
        return ({"url": PMC + "body", "format": "pmc_cloud_text", "content": "Full text. " * 500,
                 "title": "A verified paper"}, "cc-by", "https://example.org/license")

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(pmc, "licensed_full_text", official)
    message, result = fetch.deepseek_fetch_paper.func(doi=DOI, urls=[PMC])
    assert "PMC Cloud fallback" in message and result["fetch_status"] == "full_text"
    assert result["fetch_stats"]["won"] == PMC + "body" and posts.count(PMC) == 1
    assert all("target_validated" not in row for row in result["fetch_attempts"])
    assert len(reader_calls) == 1 and 0 < reader_calls[0] <= fetch._RACE_DEADLINE_SECONDS
    assert offline_fetch[1][0][:2] == (DOI, "") and offline_fetch[1][0][2]["validity"] == "valid"


def test_paper_deadline_does_not_wait_for_running_cloud_fallback(monkeypatch, offline_fetch):
    entering, release, finished = (threading.Event() for _ in range(3))

    def rebuild(*_args, **_kwargs):
        entering.set()
        release.wait(2)
        finished.set()
        return None

    monkeypatch.setattr(fetch, "_RACE_DEADLINE_SECONDS", 0.4)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response(404))
    monkeypatch.setattr(pmc, "licensed_full_text", rebuild)
    started = monotonic()
    try:
        _, result = fetch.deepseek_fetch_paper.func(urls=[PMC])
        elapsed = monotonic() - started
        assert entering.is_set() and not finished.is_set()
        assert result["fetch_status"] == "failed" and elapsed < 1.2
        assert offline_fetch[1] == [("", "", None)]
        print(f"synthetic running fallback: foreground={elapsed:.3f}s, owned_deadline=0.400s")
    finally:
        release.set()
        if entering.is_set():
            assert finished.wait(1)


def test_paper_deadline_never_starts_cloud_fallback_after_source_timeout(monkeypatch, offline_fetch):
    release, completed = threading.Event(), threading.Event()

    def post(*_args, **_kwargs):
        release.wait(2)
        completed.set()
        return response(504)

    monkeypatch.setattr(fetch, "_RACE_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(pmc, "licensed_full_text", lambda *a, **k: pytest.fail("late PMC fallback"))
    try:
        _, result = fetch.deepseek_fetch_paper.func(urls=[PMC])
        assert result["fetch_status"] == "failed" and not completed.is_set()
        assert [row["error"] for row in result["fetch_attempts"]] == ["race_deadline_exceeded"]
    finally:
        release.set()
        assert completed.wait(1)
