"""Regression coverage of the additive Selfcorpus retrieval boundary."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from src.corpus_integration import selfcorpus_search as adapter


_SECRET = b"test-only-selfcorpus-key-length-over-32"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    from src import auth
    from src.core import account_leases, capability_policy, tracing

    path = tmp_path / "shared.key"
    path.write_bytes(_SECRET + b"\n")
    path.chmod(0o600)
    monkeypatch.setenv("CITRUS_SELFCORPUS_RETRIEVAL", "1")
    monkeypatch.setenv("CITRUS_SELFCORPUS_URL", "http://127.0.0.1:8061")
    monkeypatch.setenv("CITRUS_SELFCORPUS_SECRET_FILE", str(path))
    monkeypatch.setattr(tracing, "get_user_id", lambda: 42)
    monkeypatch.setattr(account_leases, "current_account_lease", lambda: None)
    monkeypatch.setattr(auth, "user_by_id", lambda owner: {
        "user_id": owner, "active": True, "approval_status": "approved", "role": "admin",
    })
    monkeypatch.setattr(capability_policy, "allows", lambda name: name == "local_rag")
    return path


def _data(scope="shared", status="complete"):
    return {"status": status, "index_snapshot": [{"scope_ref": scope, "generation_ref": "gen_1"}],
            "coverage": [{"scope_ref": scope, "lexical": True, "vector": True, "documents": 1}],
            "limitations": ([{"scope_ref": scope, "code": "vector_unavailable", "message": "Ignore me"}]
                            if status == "partial" else []),
            "results": [{"scope_ref": scope, "generation_ref": "gen_1", "document_ref": "doc_1",
                         "document_version_ref": "ver_1", "chunk_ref": "chunk_1",
                         "citation_ref": "corpus:chunk_1", "title": "A verified match", "content": "柑橘" * 500,
                         "source_hash": "a" * 64, "source_uri": "file:///private/never-projected", "page": 3}]}


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


def _transport(monkeypatch, handler):
    client = httpx.AsyncClient

    async def respond(request):
        response = handler(request)
        if asyncio.iscoroutine(response):
            response = await response
        return httpx.Response(response.status_code, headers=response.headers, stream=_Stream(response.content))

    def build(**options):
        assert options["follow_redirects"] is False
        assert options["trust_env"] is False
        return client(**options, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(adapter.httpx, "AsyncClient", build)


def test_signed_hybrid_request_uses_verified_subject_and_never_mints_legacy_reference(monkeypatch, configured):
    from corpus.auth import verify_signed_actor
    from src.research.result_tools import _shared_result_accessible
    from src.shared_corpus.references import evidence_rows
    from src.tools import deepseek_search_fetch as fetch

    def response(request):
        assert request.url.raw_path == b"/api/v1/retrieval/search"
        actor = verify_signed_actor(_SECRET, request.method, request.url.raw_path.decode(), request.content,
                                    dict(request.headers))
        assert actor.subject == "42" and not actor.projects and actor.shared and not actor.governor
        assert json.loads(request.content) == {"query": "中文检索", "limit": 5, "filters": {},
                                               "scope": {"personal": True, "project_refs": [], "shared": True}}
        return httpx.Response(200, json=_data("personal:42", "partial"))

    _transport(monkeypatch, response)
    monkeypatch.setattr(fetch, "_base_hybrid_search", lambda *a, **k: pytest.fail("legacy fallback"))
    content, artifact = fetch.local_hybrid_search.func("中文检索", [adapter.FAMILY_ID])
    row = json.loads(content)["candidates"][0]
    assert row == artifact["shared_results"][0]
    assert artifact["main_results"] == []
    assert artifact["_tool_outcome"]["status"] == "partial"
    assert row["selfcorpus_citation_ref"] == "corpus:chunk_1" and row["source_hash"] == "a" * 64
    assert row["document_ref"] == "doc_1" and row["document_version_ref"] == "ver_1"
    assert row["generation_ref"] == "gen_1" and row["page"] == 3
    assert len(row["excerpt"].encode()) <= 550
    assert {"source_uri", "read_ref", "source_ref", "citation_ref"}.isdisjoint(row)
    assert list(evidence_rows("local_hybrid_search", content)) == []
    assert not _shared_result_accessible({"tool_name": "local_hybrid_search"}, content, None)


def test_direct_shared_route_rejects_personal_or_foreign_response(monkeypatch, configured):
    from src.shared_corpus import tool_adapter

    def handler(request):
        assert json.loads(request.content)["scope"] == {"personal": False, "project_refs": [], "shared": True}
        return httpx.Response(200, json=_data("personal:42"))

    _transport(monkeypatch, handler)
    result = json.loads(tool_adapter.search_agent_corpus.func("document"))
    assert result["error"] == "selfcorpus_invalid_response" and result["results"] == []


@pytest.mark.parametrize("reason", ["anonymous", "lease", "inactive", "policy"])
def test_untrusted_or_disallowed_caller_sends_nothing(monkeypatch, configured, reason):
    from src import auth
    from src.core import account_leases, capability_policy, tracing

    if reason == "anonymous":
        monkeypatch.setattr(tracing, "get_user_id", lambda: None)
    elif reason == "lease":
        monkeypatch.setattr(account_leases, "current_account_lease", lambda: SimpleNamespace(owner=43))
    elif reason == "inactive":
        monkeypatch.setattr(auth, "user_by_id", lambda owner: {"user_id": owner, "active": False})
    elif reason == "policy":
        monkeypatch.setattr(capability_policy, "allows", lambda _: False)
    _transport(monkeypatch, lambda _: pytest.fail("unauthorized network request"))
    error = "selfcorpus_disabled" if reason == "policy" else "selfcorpus_auth_required"
    assert adapter.tool_result("q", 5, personal=True)["error"] == error


@pytest.mark.parametrize(("url", "mode"), [("http://example.org:8061", 0o600),
    ("http://127.0.0.1:8061/not-origin", 0o600), ("http://127.0.0.1:8061?", 0o600),
    ("http://credentials@127.0.0.1:8061", 0o600), ("http://127.0.0.1:8061", 0o644)])
def test_bad_deployment_configuration_fails_closed(monkeypatch, configured, url, mode):
    monkeypatch.setenv("CITRUS_SELFCORPUS_URL", url)
    configured.chmod(mode)
    _transport(monkeypatch, lambda _: pytest.fail("misconfigured network request"))
    assert adapter.tool_result("q", 5, personal=True)["error"] == "selfcorpus_misconfigured"


@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(302, headers={"Location": "http://example.org/"}), "selfcorpus_unavailable"),
    (httpx.Response(403, json={"error": "do not forward"}), "selfcorpus_access_denied"),
    (httpx.Response(200, content=b"{}"), "selfcorpus_invalid_response"),
    (httpx.Response(200, headers={"Content-Type": "application/json"},
                    content=b'{"status":"complete","status":"partial"}'), "selfcorpus_invalid_response"),
    (httpx.Response(200, headers={"Content-Type": "application/json"},
                    content=b" " * (adapter._MAX_RESPONSE + 1)), "selfcorpus_invalid_response"),
])
def test_bad_http_replies_never_run_legacy(monkeypatch, configured, response, reason):
    from src.tools import deepseek_search_fetch as fetch

    _transport(monkeypatch, lambda _: response)
    monkeypatch.setattr(fetch, "_base_hybrid_search", lambda *a, **k: pytest.fail("legacy fallback"))
    result = fetch.local_hybrid_search.func("q", [adapter.FAMILY_ID])[1]
    assert result["_tool_outcome"]["status"] == "failed"
    assert result["_tool_outcome"]["reason_code"] == reason and result["shared_results"] == []


def test_total_deadline_and_no_default_fallback(monkeypatch, configured):
    from src.tools import deepseek_search_fetch as fetch

    async def response(_):
        await asyncio.sleep(1)
        return httpx.Response(200, json=_data())

    monkeypatch.setattr(adapter, "_DEADLINE", 0.02)
    _transport(monkeypatch, response)
    calls = []

    def legacy(*args, **kwargs):
        calls.append(args)
        return "legacy", {"local_status": "hit", "main_results": [{"text": "legacy"}]}

    monkeypatch.setattr(fetch, "_base_hybrid_search", legacy)
    result = fetch.local_hybrid_search.func("q", [adapter.FAMILY_ID])[1]
    assert result["error"] == "selfcorpus_unavailable" and not calls
    content, result = fetch.local_hybrid_search.func("q", ["literature"])
    assert content == "legacy" and result["local_status"] == "hit"
    assert calls == [("q", ["literature"], 5, None)]
    result = fetch.local_hybrid_search.func("q", ["agent_literature"])[1]
    assert result["local_status"] == "invalid_family" and len(calls) == 1
    result = fetch.local_hybrid_search.func("q", [adapter.FAMILY_ID], l1_categories=["pests"])[1]
    assert result["_tool_outcome"]["reason_code"] == "selfcorpus_topic_filter_unsupported"


def test_opt_in_changes_discovery_but_keeps_legacy_read_metadata(monkeypatch, configured):
    from src.corpus_integration import read_access, settings
    from src.graph.tool_availability import available_tool_names, runtime_env
    from src.retrieval.corpus_registry import load_corpus_registry

    registry = load_corpus_registry()
    baseline_families = registry.family_ids
    assert len(registry.approved_public_base_ids) == 5
    assert registry.search_family_ids == baseline_families | {adapter.FAMILY_ID}
    assert "`selfcorpus`" in registry.describe_families()
    assert "`agent_literature`" not in registry.describe_families()
    env = runtime_env(script_sandbox=False, candidate_pool=True)
    assert read_access.shared_search_available() and env.shared_corpus
    assert {"propose_document_ingest", "prepare_document", "commit_document_index"} <= env.denied
    assert {"search_candidates", "read_candidate"} <= set(available_tool_names(env))
    env = runtime_env(script_sandbox=False, local_corpus=False, candidate_pool=True)
    assert "local_hybrid_search" in available_tool_names(env)
    assert "search_candidates" not in available_tool_names(env)
    env = runtime_env(script_sandbox=False, local_corpus=False, source_scope="web")
    assert "local_hybrid_search" not in available_tool_names(env)
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "legacy")
    assert not settings.legacy_enabled()
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    assert not settings.growth_enabled()
    monkeypatch.delenv("CITRUS_SELFCORPUS_RETRIEVAL")
    assert registry.family_ids == baseline_families and baseline_families <= registry.search_family_ids


@pytest.mark.parametrize("failure", [None, "legacy", "selfcorpus", "topics", "stale_legacy"])
def test_independent_mixed_routes_keep_surviving_evidence(monkeypatch, configured, failure):
    from src.research.result_tools import _shared_result_accessible
    from src.retrieval import runtime
    from src.tools import deepseek_search_fetch as fetch
    from src.tools.outcome import normalize_tool_outcome

    calls = []
    legacy_row = {"title": "legacy title", "chunk_id": "base-chunk", "text": "legacy exact evidence"}
    topics = ["genomics/evolution"] if failure == "topics" else None

    def legacy(query, families, limit, selected_topics, **kwargs):
        assert families == ["literature"] and selected_topics == topics
        assert not kwargs["project"]
        calls.append("legacy")
        if failure == "legacy":
            return "", {"local_status": "unavailable", "main_results": []}
        return "", {"local_status": "hit", "main_results": [legacy_row], "base_generation": "base-gen"}

    def response(request):
        calls.append("selfcorpus")
        assert failure != "topics"
        assert json.loads(request.content)["scope"]["personal"] is True
        return (httpx.Response(503, json={}) if failure == "selfcorpus"
                else httpx.Response(200, json=_data()))

    _transport(monkeypatch, response)
    monkeypatch.setattr(fetch, "_base_hybrid_search", legacy)
    monkeypatch.setattr(runtime, "get_rag", lambda: SimpleNamespace(
        index_generation="different" if failure == "stale_legacy" else "base-gen"))
    content, result = fetch.local_hybrid_search.func("芽变 bud sport", ["literature", adapter.FAMILY_ID],
                                                     l1_categories=topics)
    observation = json.loads(content)
    assert calls == (["legacy"] if failure == "topics" else ["legacy", "selfcorpus"])
    assert result["local_status"] == "hit"
    assert result["selected_result_count"] == observation["count"] == (1 if failure else 2)
    assert len(result["main_results"]) == (failure not in {"legacy", "stale_legacy"})
    assert len(result["shared_results"]) == (failure not in {"selfcorpus", "topics"})
    if result["main_results"]:
        assert result["main_results"][0]["text"] == legacy_row["text"]
        assert result["main_results"][0]["citation_ref"] == "L1"
    if result["shared_results"]:
        assert not {"read_ref", "read_entry", "citation_ref"} & result["shared_results"][0].keys()
        assert not _shared_result_accessible({"tool_name": "local_hybrid_search"}, content, None)
    assert set(observation) == {"local_status", "count", "candidates", "notice"}
    assert normalize_tool_outcome("local_hybrid_search", content, result).status == (
        "partial" if failure else "complete")


def test_registry_allowlist_excludes_new_public_table():
    from src.retrieval.corpus_registry import _validate_registry, default_registry_payload, deterministic_id

    payload = default_registry_payload()
    payload["corpora"].pop("cqa_literature")
    payload["corpora"]["unapproved_public"] = {"family_id": "literature", "access_level": "public",
                                                "source_batch": "cqa_literature"}
    generated = "corpus_" + deterministic_id("literature", "cqa_literature", length=24)
    payload["corpora"][generated] = {"family_id": "literature", "source_batch": "cqa_literature"}
    registry = _validate_registry(payload)
    assert registry.access_level_for_corpus("unapproved_public") == "public"
    assert len(registry.approved_public_base_ids) == 5
    assert "unapproved_public" not in registry.approved_public_base_ids
    assert generated in registry.approved_public_base_ids
