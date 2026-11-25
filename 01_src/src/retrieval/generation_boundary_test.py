import json

import pytest

from src.retrieval import corpus_admin, corpus_registry, multi_retriever, query_cache, runtime


class Embedder:
    dim = 4

    def embed_query(self, query):
        return [1.0, 0.0, 0.0, 0.0]

    def embed_passages(self, texts):
        return [self.embed_query(text) for text in texts]


class Reranker:
    def __init__(self):
        self.before_score = lambda: None
        self.calls = 0

    def rerank(self, query, rows, *, top_k):
        self.calls += 1
        self.before_score()
        return [{**row, "rerank_score": 0.9} for row in rows[:top_k]]


@pytest.fixture
def base_fixture(tmp_path, monkeypatch):
    import lancedb
    from src.config import settings
    from src.core import capability_policy, tracing

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_DIR", data)
    monkeypatch.setattr(settings, "RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(settings, "RAG_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_CACHE_TTL_HOURS", 24)
    monkeypatch.delenv("CITRUSQA_CORPUS_REGISTRY", raising=False)
    monkeypatch.setattr(corpus_admin, "DATA_DIR", data)
    monkeypatch.setattr(multi_retriever, "_BM25_CACHE_DIR", tmp_path / "bm25")
    monkeypatch.setattr(multi_retriever.MultiBatchRetriever, "_instance", None)
    monkeypatch.setattr(runtime, "_RAG_INSTANCE", None)
    monkeypatch.setattr(query_cache.get_query_cache, "_cache", None, raising=False)
    monkeypatch.setattr(query_cache.get_query_cache, "_config", None, raising=False)
    monkeypatch.setattr(capability_policy, "allows", lambda capability: capability == "local_rag")
    monkeypatch.setattr(tracing, "get_user_id", lambda: 5)
    monkeypatch.setattr(tracing, "get_user_role", lambda: "user")
    reranker = Reranker()
    monkeypatch.setattr(multi_retriever, "Embedder", Embedder)
    monkeypatch.setattr(multi_retriever, "Reranker", lambda: reranker)

    generation = "g20260101T000000Z_abcdef00"
    root = data / "corpus_generations" / generation
    root.mkdir(parents=True)
    lance_root = data / "lancedb_generations" / generation
    lance_root.parent.mkdir()
    ldb = lancedb.connect(str(lance_root))
    families = ["literature", "methods", "translated", "deng_book", "varieties"]
    registry = {"schema_version": 1, "registry_version": generation, "families": {}, "corpora": {}}
    for family in families:
        cid = f"fixture_{family}"
        chunk = {"schema_version": 2, "corpus_id": cid, "corpus_family_id": family,
                 "document_id": cid, "chunk_index": 0, "chunk_id": corpus_registry.deterministic_id(cid, cid, 0),
                 "title": f"Fixture {family}", "source": "fixture", "text": "Citrus soil sample methods",
                 "l1_category": "citrus-topic"}
        chunks = root / (cid + ".jsonl")
        chunks.write_text(json.dumps(chunk) + "\n")
        registry["families"][family] = {"display_name": family, "topics": ["citrus-topic"]}
        registry["corpora"][cid] = {"family_id": family, "chunk_count": 1,
            "chunks_path": str(chunks.relative_to(data)), "table_name": cid, "access_level": "public"}
        corpus_admin._make_index_table(ldb, cid, chunks, Embedder(), 1)
    registry_file = root / "corpus_registry.json"
    corpus_admin._write_json_atomic(registry_file, registry)
    pointer = {"generation": generation, "registry_path": str(registry_file.relative_to(data)),
        "lancedb_path": str(lance_root.relative_to(data)), "registry_version": generation,
        "index_schema_version": corpus_admin.INDEX_SCHEMA_VERSION, "embedding_model": settings.EMBEDDING_MODEL,
        "dimension": Embedder.dim, "document_prefix": corpus_admin.DOCUMENT_PREFIX,
        "query_prefix": corpus_admin.QUERY_PREFIX, "chunk_count": 5, "table_count": 5}
    corpus_admin._write_json_atomic(root / "index_manifest.json", pointer)
    assert corpus_admin.activate_main(["--generation", generation]) == 0
    return data, pointer, reranker


def test_registry_only_switch_discards_inflight_results_and_verified_rollback(base_fixture, monkeypatch):
    from src.tools.deepseek_search_fetch import _base_hybrid_search
    from src.tools.outcome import normalize_tool_outcome

    data, pointer, reranker = base_fixture
    old_reader = runtime.get_rag()
    assert old_reader.index_generation == pointer["generation"]
    assert len(old_reader.available_corpus_families) == len(old_reader.lance_tables) == 5
    assert not old_reader.failed_batches
    # The reranker is reached only after BM25 and Dense, so this creates a
    # registry-only switch while a real candidate request is already in flight.
    def registry_change():
        assert corpus_admin.registry_main(["register-topic", "--family-id", "literature",
                                           "--topic", "new-topic"]) == 0

    reranker.before_score = registry_change
    content, rejected = _base_hybrid_search("citrus soil", ["literature"], render=True, progress=False)
    outcome = normalize_tool_outcome("local_hybrid_search", content, rejected)
    assert rejected["main_results"] == [] and outcome.reason_code == "base_index_reload_required"
    assert outcome.status == "not_run"
    with pytest.raises(runtime.RetrievalUnavailable, match="base_index_reload_required"):
        runtime.get_rag()
    new_pointer = corpus_registry.active_index_pointer(data)
    assert new_pointer["generation"] != pointer["generation"]
    assert new_pointer["lancedb_path"] == pointer["lancedb_path"]
    # New process state pins the registry-only generation, with the same tables.
    monkeypatch.setattr(multi_retriever.MultiBatchRetriever, "_instance", None)
    monkeypatch.setattr(runtime, "_RAG_INSTANCE", None)
    reranker.before_score = lambda: None
    _, result = _base_hybrid_search("citrus soil", ["literature"], progress=False)
    assert result["base_generation"] == new_pointer["generation"] and result["local_status"] == "hit"
    assert result["base_degradation_reason"] is None

    previous_file = data / "active_index.previous.json"
    corpus_admin._write_json_atomic(previous_file, {**pointer, "dimension": 8})
    assert corpus_admin.rollback_main([]) == 2
    assert corpus_registry.active_index_pointer(data) == new_pointer
    corpus_admin._write_json_atomic(previous_file, pointer)
    assert corpus_admin.rollback_main([]) == 0
    assert corpus_registry.active_index_pointer(data) == pointer
    with pytest.raises(runtime.RetrievalUnavailable, match="base_index_reload_required"):
        runtime.get_rag()
    runtime.require_current_rag(old_reader)
    # Corrupt active identity must fail closed, including on a cacheable query.
    (data / "active_index.json").write_text("{bad-json")
    _, unavailable = _base_hybrid_search("citrus soil", ["literature"], progress=False)
    assert unavailable["reason_code"] == "base_index_pointer_invalid"


def test_real_reranker_fallback_cache_expires_before_healthy_results(base_fixture, monkeypatch):
    from src.tools.deepseek_search_fetch import _base_hybrid_search
    from src.tools.outcome import normalize_tool_outcome

    _, pointer, reranker = base_fixture
    monotonic = [1000]
    # Only the cache clock is fake. Replacing time.monotonic on the stdlib
    # module would also alter the real thread/executor deadline logic.
    from types import SimpleNamespace
    monkeypatch.setattr(query_cache, "time", SimpleNamespace(monotonic=lambda: monotonic[0]))

    def fail():
        raise RuntimeError("fixture reranker unavailable")

    reranker.before_score = fail
    def lookup():
        return _base_hybrid_search("citrus soil", ["literature"], progress=False)

    content, degraded = lookup()
    assert json.loads(content)["base_generation"] == pointer["generation"]
    assert degraded["rag_cache_status"] == "miss" and normalize_tool_outcome("local_hybrid_search", content, degraded).status == "partial"
    assert normalize_tool_outcome("local_hybrid_search", content, degraded).reason_code == "reranker_fallback"
    reranker.before_score = lambda: None
    monotonic[0] += 59
    _, cached = lookup()
    assert cached["rag_cache_status"] == "hit" and cached["reranker_fallback"]
    assert reranker.calls == 1
    monotonic[0] += 1
    content, recovered = lookup()
    assert recovered["rag_cache_status"] == "miss" and recovered["base_degradation_reason"] is None
    assert normalize_tool_outcome("local_hybrid_search", content, recovered).status == "complete"
    assert reranker.calls == 2
    monotonic[0] += 3600
    assert lookup()[1]["rag_cache_status"] == "hit"
    from src.engine import hardware
    generation = [hardware.runtime_model_generation() + 1]
    monkeypatch.setattr(hardware, "runtime_model_generation", lambda: generation[0])
    assert lookup()[1]["rag_cache_status"] == "miss"
    assert reranker.calls == 3
    assert lookup()[1]["rag_cache_status"] == "hit"
    generation[0] += 1
    def change_model_during_rerank():
        generation[0] += 1

    reranker.before_score = change_model_during_rerank
    assert lookup()[1]["base_degradation_reason"] == "retrieval_model_changed"
    reranker.before_score = lambda: None
    assert lookup()[1]["rag_cache_status"] == "miss"
    assert lookup()[1]["rag_cache_status"] == "hit"



def test_mixed_search_filters_duplicates_and_never_projects_stale_base(base_fixture):
    from src.tools import deepseek_search_fetch as fetch
    from src.tools.outcome import normalize_tool_outcome

    _, pointer, _ = base_fixture
    family_id = "agent_literature"
    ref = {"source_family": family_id, "document_id": "shared_paper", "version_id": "version_1",
           "start": 0, "end": 15, "chunk_id": "a" * 32, "md_sha256": "b" * 64,
           "index_generation": "generation_1"}
    hit = {"source_family": family_id, "excerpt": "Shared soil data", "title": "Shared study", "read_ref": ref}

    class Shared:
        unavailable = False
        generation = "generation_1"
        change_base = False

        def search(self, query, *, limit):
            if self.change_base:
                assert corpus_admin.registry_main(["register-topic", "--family-id", "literature",
                                                   "--topic", "updated-topic"]) == 0
            return [hit, hit, {**hit, "read_ref": {}}, {**hit, "excerpt": ""}]

    shared = Shared()
    def lookup():
        return fetch._search_base_and_shared(shared, "citrus soil", ["literature", family_id], 5, None,
                                             family_id=family_id, render=True, progress=False)

    content, artifact = lookup()
    assert artifact["base_generation"] == pointer["generation"]
    assert artifact["selected_result_count"] == json.loads(content)["count"] == 2
    assert len(artifact["main_results"]) == len(artifact["shared_results"]) == 1
    assert normalize_tool_outcome("local_hybrid_search", content, artifact).status == "complete"
    shared.change_base = True
    content, artifact = lookup()
    assert artifact["selected_result_count"] == json.loads(content)["count"] == 1
    assert not artifact["main_results"] and len(artifact["shared_results"]) == 1
    outcome = normalize_tool_outcome("local_hybrid_search", content, artifact)
    assert (outcome.status, outcome.reason_code) == ("partial", "base_index_reload_required")
