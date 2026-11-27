from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from src.corpus_integration import evidence_client as client


def reply():
    text = "芽变见于该研究。完整内容尚有更多。"
    return {"schema_version": 1, "status": "hit",
            "index_snapshot": {"snapshot_ref": "snapshot_test", "sources": [
                {"source_id": "cqa_literature", "generations": [{"generation_ref": "base-g"}]},
                {"source_id": "evolution", "generations": [{"scope_ref": "personal:19", "generation_ref": "personal-g"}]}]},
            "source_outcomes": [
                {"source_id": source, "status": status, "reason_codes": [], "candidate_count": count,
                 "returned_count": count}
                for source, status, count in (("cqa_literature", "hit", 1), ("evolution", "empty", 0))],
            "budget_used": {"candidates": 1, "results": 1, "chars": len(text)},
            "results": [{"source_id": "cqa_literature", "generation_ref": "base-g", "document_ref": "doc",
                         "version_ref": "v1", "passage_ref": "chunk", "evidence_ref": "ev1_test",
                         "title": "Example", "excerpt": text, "readback": "exact",
                         "passage_hash": hashlib.sha256(text.encode()).hexdigest()}],
            "limitations": [], "coverage": []}


def test_one_search_and_scoped_topics(monkeypatch):
    calls = []

    def transport(*args):
        calls.append(args)
        return 19, reply()

    monkeypatch.setattr(client, "_transport", transport)
    received = client.search("芽变", ["cqa_literature", "evolution"], 5, ["育种"])
    assert len(calls) == 1
    route, request, user = calls[0]
    assert route == client.SEARCH_PATH and user is None
    assert request["query"] == "芽变"
    assert request["sources"] == [{"source_id": "cqa_literature", "topics": ["育种"]}, {"source_id": "evolution"}]
    assert request["evolution_scope"] == {"personal": True, "shared": True, "projects": []}
    assert request["budget"] == {"max_candidates": 240, "max_results": 5, "max_chars": 24000, "timeout_ms": 4000}
    assert received["results"][0]["readback"] == "exact"


@pytest.mark.parametrize("tamper", ["wrong_generation", "foreign_scope", "missing_hash", "budget", "count", "no_results"])
def test_rejects_invalid_contract(monkeypatch, tamper):
    data = reply()
    if tamper == "wrong_generation":
        data["results"][0]["generation_ref"] = "oops"
    elif tamper == "foreign_scope":
        data["index_snapshot"]["sources"][1]["generations"][0]["scope_ref"] = "personal:20"
    elif tamper == "missing_hash":
        del data["results"][0]["passage_hash"]
    elif tamper == "budget":
        data["budget_used"]["chars"] = 24_001
    elif tamper == "count":
        data["source_outcomes"][0]["returned_count"] = 0
    else:
        data["status"] = "empty"
    monkeypatch.setattr(client, "_transport", lambda *args: (19, data))
    with pytest.raises(client.SearchUnavailable, match="corpus_invalid_response"):
        client.search("芽变", ["cqa_literature", "evolution"], 5)


def test_evidence_encoding_and_bounded_exact_read(monkeypatch):
    row = reply()["results"][0]
    data = {key: row[key] for key in (*client.IDENTITY, "evidence_ref")}
    text = row["excerpt"]
    data.update(schema_version=1, offset=1, text=text[1:3], next_offset=3, total_chars=len(text))
    calls = []

    def transport(*args):
        calls.append(args)
        return 19, deepcopy(data)

    monkeypatch.setattr(client, "_transport", transport)
    assert client.read_exact(row, 1, 2, user={"user_id": 19}) == (text[1:3], len(text), 3)
    assert calls == [(client.READ_PATH, {"evidence_ref": "ev1_test", "offset": 1, "max_chars": 2}, {"user_id": 19})]
    data["version_ref"] = "changed"
    with pytest.raises(client.SearchUnavailable):
        client.read_exact(row, 1, 2, user={"user_id": 19})


def test_unknown_or_duplicate_selection_never_calls_network(monkeypatch):
    monkeypatch.setattr(client, "_transport", lambda *args: pytest.fail("unexpected transport"))
    for sources in (["evolution", "evolution"], ["project:competitor"], ["invalid"]):
        with pytest.raises(client.SearchUnavailable, match="corpus_invalid_source"):
            client.search("芽变", sources, 5)


def test_unified_mode_hides_alternative_search_and_routes_once(monkeypatch):
    from src.graph.tool_availability import missing_requirements, runtime_env
    from src.shared_corpus.tool_adapter import search_agent_corpus
    from src.tools import candidate_tools, deepseek_search_fetch as fetch

    monkeypatch.setenv("CITRUS_CORPUS_UNIFIED_RETRIEVAL", "1")
    monkeypatch.setenv("CITRUS_SELFCORPUS_RETRIEVAL", "1")
    env = runtime_env(retrieval=True, source_scope="local_only")
    assert {"search_candidates", "search_agent_corpus"} <= env.denied
    assert "local_corpus" not in missing_requirements("local_hybrid_search", env)
    assert candidate_tools.search_candidates.func("芽变")[1]["_tool_outcome"]["reason_code"] == "corpus_use_unified"
    assert "corpus_use_unified" in search_agent_corpus.func("芽变")
    monkeypatch.setattr(fetch, "_base_hybrid_search", lambda *a, **k: pytest.fail("legacy fallback"))
    calls = []

    def transport(*args):
        calls.append(args)
        data = reply()
        data["results"][0]["readback"] = "excerpt_only"
        return 19, data

    monkeypatch.setattr(client, "_transport", transport)
    _, result = fetch.local_hybrid_search.func("芽变", ["cqa_literature", "evolution"])
    assert len(calls) == 1
    assert result["local_status"] == "hit" and result["selected_result_count"] == 1
    assert result["main_results"] == []


def test_legacy_reference_identity_is_untouched():
    from src.core.turn_source_ledger import local_read_reference, reference_key

    old = {"ref": "ev:" + "a" * 32, "turn_id": "turn", "generation": "generation", "start": 0, "end": 15}
    assert local_read_reference(old) == old
    assert reference_key("cite", old) == reference_key("cite", local_read_reference(old))
    new = {**old, "ref": "ce1:" + "b" * 64}
    assert local_read_reference(new) == new
    assert local_read_reference({**old, "ref": "ev1_opaque_corpus_reference"}) is None


def test_candidate_corpus_wire_in_process(tmp_path, monkeypatch):
    pytest.importorskip("corpus.unified", reason="requires the candidate Corpus v1 source")
    from corpus.api import _dispatch
    from corpus.auth import verify_signed_actor
    from corpus.embeddings import HashEmbedding
    from corpus.service import App
    from corpus.storage import Database, digest

    text = "芽变😀证据段落"
    secret = b"offline fixture, no network or service"

    class Legacy:
        generation, fingerprint = "generation-base", "readonly-fixture"

        def pin(self):
            return self

        def check_current(self):
            pass

        def search(self, source, query, topics, filters, caps, check):
            return [{"source_id": source, "generation_ref": self.generation, "document_ref": "doc",
                     "version_ref": "version", "passage_ref": "passage", "title": "Source", "text": text,
                     "passage_hash": digest(text), "locator": {"ref": "passage"}}], [], 1

        def read(self, source, generation, locator):
            assert (source, generation, locator) == ("cqa_literature", self.generation, {"ref": "passage"})
            return {"text": text, "span": {"start": 10, "end": 10 + len(text)}}

    app = App(Database(tmp_path / "new-corpus.sqlite3"), HashEmbedding(), legacy=Legacy())
    calls = []

    async def post(origin, raw, headers, *, route):
        assert origin == "https://unused.invalid"
        actor = verify_signed_actor(secret, "POST", route, raw, headers)
        assert actor.subject == "19" and actor.base_sources == tuple(sorted(client.BASE_SOURCES))
        assert actor.shared and not actor.governor and not actor.projects
        calls.append(route)
        return _dispatch(app, actor, "POST", tuple(route.split("/")[3:]), raw)

    monkeypatch.setenv("CITRUS_CORPUS_UNIFIED_RETRIEVAL", "1")
    monkeypatch.setattr(client, "_config", lambda: ("https://unused.invalid", secret))
    monkeypatch.setattr(client, "_owner", lambda: 19)
    monkeypatch.setattr(client, "_post", post)
    result = client.search("芽变", ["cqa_literature", "evolution"], 5)
    assert result["status"] == "hit"
    assert result["results"][0]["excerpt"] == text
    assert client.read_exact(result["results"][0], 2, 1) == ("😀", len(text), 3)
    assert calls == [client.SEARCH_PATH, client.READ_PATH]


def test_unified_startup_does_not_open_host_index(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from src.api import main

    monkeypatch.setenv("CITRUS_CORPUS_UNIFIED_RETRIEVAL", "1")
    monkeypatch.setattr(main, "_warm_retrieval", lambda: pytest.fail("legacy warmup"))
    monkeypatch.setattr(client, "service_available", lambda: False)

    async def observe(*args):
        pass

    monkeypatch.setattr(main, "observe_runtime", observe)
    state = SimpleNamespace(retrieval_ready=False, retrieval_error=None, rag_runtime=None,
                            model_evidence=SimpleNamespace(publish=lambda value: None))
    asyncio.run(main._serve_model_runtime(state))
    assert state.rag_runtime is None and not state.retrieval_ready
    assert state.retrieval_error == ("corpus_unavailable", False)
