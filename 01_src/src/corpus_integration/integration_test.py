from hashlib import sha256
import json
import sqlite3

import pytest

from citrus_corpus.intake import Intake
from citrus_corpus.store import Store
from src.corpus_integration.legacy_read import LegacyReader
from src.corpus_integration.migration import dry_run
from src.shared_corpus import catalog, journal, prepare


def test_fetch_paper_handoff_persists_only_public_identity_inline(tmp_path, monkeypatch):
    from src.core import capability_policy, tracing
    from src.corpus_integration import automatic_worker, paper_source
    from src.tools import deepseek_search_fetch as fetch

    root = Store.initialize(tmp_path / "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(root.root))
    monkeypatch.setattr(tracing, "get_user_id", lambda: 7)
    monkeypatch.setattr(capability_policy, "allows", lambda name: name in {"web_fetch", "bibliographic_lookup"})
    monkeypatch.setattr(automatic_worker, "account_epoch", lambda owner: 1 if owner == 7 else None)
    monkeypatch.setattr(paper_source.requests, "get", lambda *a, **k: pytest.fail("inline provider access"))

    winner = {"url": "https://a.example/study", "content": "untrusted bytes", "validity": "valid"}
    before = winner.copy()
    assert fetch._retain_paper(winner, doi="10.1234/work") is None
    fetch._queue_paper_registration("10.1234/work", "", winner)
    fetch._queue_paper_registration("10.1234/another", "", winner)
    assert winner == before and list(root.originals.iterdir()) == []
    with root.read() as conn:
        rows = conn.execute("SELECT state,expected_sha256,request FROM intake ORDER BY identity").fetchall()
    assert [json.loads(r[2])["doi"] for r in rows] == ["10.1234/another", "10.1234/work"]
    assert all(r[0] == "automatic_queued" and r[1] is None and "untrusted" not in r[2] for r in rows)
    assert fetch._retain_missing_paper(doi="", pmid="", title="", url="https://a.example") is None

    monkeypatch.setattr(capability_policy, "allows", lambda name: name == "web_fetch")
    fetch._queue_paper_registration("10.1234/denied", "", winner)
    assert len(Intake(root).queue.recent()) == 2


def test_legacy_citation_and_consistent_migration_dry_run(tmp_path):
    legacy = tmp_path / "legacy"
    old_catalog = catalog.Catalog(legacy)
    old_journal = journal.Journal(legacy)
    raw = "# Methods\n\nLegacy citrus trial methods.\n"
    digest = sha256(raw.encode()).hexdigest()
    metadata = {"document_id": "old", "version_id": "v1", "schema_version": 2, "title": "Legacy methods",
                "content_sha256": digest, "char_count": len(raw), "chunk_count": 1}
    prepare.write_document(old_catalog.documents, "old", "v1", metadata, raw)
    with old_journal.connection() as conn:
        conn.execute("""INSERT INTO shared_documents(document_id,version_id,candidate_id,proposal_id,content_sha256,
                     char_count,chunk_count,schema_version,prepared_at) VALUES ('old','v1',1,'p_1',?,?,1,2,0)""",
                     (digest, len(raw)))
        conn.execute("INSERT INTO shared_rights VALUES('old','v1','granted','admin_grant',1,'grant',0,0)")
        conn.execute("""INSERT INTO shared_builds (id,owner,fingerprint,documents,state,generation,created,updated)
                     VALUES ('build',1,'hash',?,'active','old_generation',0,0)""", (json.dumps([["old", "v1"]]),))
    journal.write_pointer(legacy, "old_generation")
    base = [tmp_path / f"base_{index}.json" for index in range(5)]
    for index, path in enumerate(base):
        path.write_text(str(index))
    export = dry_run(legacy, base)
    assert export["legacy_active_generation"] == "old_generation"
    assert export["copied_catalog_counts"]["shared_documents"] == 1
    assert export["sample"][0]["markdown_sha256"] == digest
    assert export["base_sha256"] == [sha256(path.read_bytes()).hexdigest() for path in base]
    assert journal.read_pointer(legacy) == "old_generation"
    reader = LegacyReader(legacy)
    assert reader.preview("old", generation="old_generation", version="v1", digest=digest,
                          start=11, length=12)["markdown"] == raw[11:23]
    with old_journal.connection() as conn:
        conn.execute("UPDATE shared_rights SET status='revoked'")
    with pytest.raises(LookupError):
        reader.preview("old", generation="old_generation", version="v1", digest=digest)


def test_disabled_sixth_corpus_preserves_base_search(monkeypatch):
    from src.tools import deepseek_search_fetch as fetch

    monkeypatch.delenv("CITRUS_CORPUS_READ", raising=False)
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setattr(fetch, "_enabled", lambda capability: capability == "local_rag")
    calls = []

    def base(query, families, limit, topics, **kwargs):
        calls.append((families, topics))
        return "", {"query": query, "main_results": [{"text": "base"}], "local_status": "hit"}

    monkeypatch.setattr(fetch, "_base_hybrid_search", base)
    _, result = fetch._local_hybrid_search_impl("test", ["literature", "agent_literature"],
                                               render=False, progress=False)
    assert calls == [(["literature"], None)] and result["main_results"] == [{"text": "base"}]
    assert result["shared_search_status"] == "unavailable"
    _, result = fetch._local_hybrid_search_impl("test", ["literature", "agent_literature"], 6,
                                               ["agriculture/ecology"], render=False, progress=False)
    assert calls[-1] == (["literature"], ["agriculture/ecology"])
    assert result["main_results"] == [{"text": "base"}]



def test_admin_explicit_intake_returns_durable_receipt(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.core import capability_policy
    from src.corpus_integration import api_router

    store = Store.initialize(tmp_path / "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    monkeypatch.setattr(api_router, "require_admin", lambda user: user["role"] == "admin")
    monkeypatch.setattr(capability_policy, "allows", lambda name: False)
    app = FastAPI()
    app.include_router(api_router.router)

    @app.middleware("http")
    async def identify(request, call_next):
        request.state.user = {"active": True, "approval_status": "approved", "user_id": 7,
                              "role": request.headers.get("x-test-role", "regular")}
        return await call_next(request)

    with TestClient(app) as client:
        path = "/api/v2/isolated-corpus/admin/intake"
        assert client.post(path, json={"doi": "10.1234/work"}).status_code == 403
        admin = {"x-test-role": "admin"}
        assert client.post(path, json={"doi": "10.1234/work"}, headers=admin).status_code == 403
        response = client.post(path, headers=admin, json={"saved_path": "work.md", "sha256": "0" * 64})
        assert response.status_code == 202 and response.json()["accepted"] is True
        job_id = response.json()["job"]["id"]
        assert client.get(path + "/" + job_id, headers=admin).json()["state"] == "queued"
        assert Intake(store).process_one()["error"] == "source_missing"
        assert client.get(path + "/" + job_id, headers=admin).json()["state"] == "failed"

        # Exact original admission belongs to the supported admin route, not to Fetch.
        original = b"# Admin fixture\n\nAn explicitly staged full text.\n"
        (store.inbox / "verified.md").write_bytes(original)
        response = client.post(path, headers=admin,
                               json={"saved_path": "verified.md", "sha256": sha256(original).hexdigest()})
        assert response.status_code == 202
        job_id = response.json()["job"]["id"]
        assert Intake(store).process_one()["state"] == "prepared"
        assert client.get(path + "/" + job_id, headers=admin).json()["raw_sha256"] == sha256(original).hexdigest()



def test_admin_public_paper_contract_and_private_status(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.core import capability_policy
    from src.corpus_integration import api_router, automatic_worker

    store = Store.initialize(tmp_path / "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    monkeypatch.setattr(capability_policy, "allows", lambda name: True)
    monkeypatch.setattr(automatic_worker, "account_epoch", lambda owner, admin: 1 if owner == 7 and admin else None)
    app = FastAPI()
    app.include_router(api_router.router)

    @app.middleware("http")
    async def identify(request, call_next):
        request.state.user = {"active": True, "approval_status": "approved", "user_id": 7,
                              "role": request.headers.get("x-test-role", "regular")}
        return await call_next(request)

    with TestClient(app) as client:
        base = "/api/v2/isolated-corpus/admin/"
        admin = {"x-test-role": "admin"}
        assert client.get(base + "intake").status_code == 403
        assert client.post(base + "public-paper", headers=admin, json={"pmcid": "PMC123"}).status_code == 409
        monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "1")
        for bad in ({"public_title": "A publication title"}, {"doi": "10.1234/paper", "body": "private"},
                    {"public_title": "A publication title", "title_is_public": "true"}):
            assert client.post(base + "public-paper", headers=admin, json=bad).status_code == 422
        admitted = client.post(base + "public-paper", headers=admin,
                               json={"public_title": "A publication title", "title_is_public": True})
        assert admitted.status_code == 202
        job = admitted.json()["job"]
        status = client.get(base + "intake", headers=admin)
        assert status.status_code == 200 and status.headers["Cache-Control"] == "private, no-store"
        assert status.json() == {"automatic_enabled": True, "jobs": [{**job,
            "bibliography_verified": False, "original_sha256": None, "source_rights": None,
            "rights_obtained": None, "rights": None, "prepared": False, "encoded_generation": None,
            "indexed_generation": None, "published_generation": None, "active_generation": None,
            "visible_generation": None, "published": False}]}
        assert "publication title" not in status.text and "account-7" not in status.text


def test_standalone_worker_cli_can_start_on_fixture_without_web(tmp_path, monkeypatch):
    import subprocess
    import sys

    store = Store.initialize(tmp_path / "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_FETCH_GROWTH", "1")
    monkeypatch.setenv("CITRUS_CORPUS_DURABLE_INTAKE", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    marker = tmp_path / "disabled-marker"
    command = [sys.executable, "-m", "citrus_corpus", "--root", str(store.root), "worker", "--allow-automatic"]
    denied = subprocess.run(command, text=True, capture_output=True, timeout=10)
    assert denied.returncode == 1 and "corpus_automatic_network_required" in denied.stderr
    result = subprocess.run([*command, "--allow-network", "--network-marker", str(marker)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout) == {"state": "idle"}
    marker.touch()
    ready = subprocess.run([*command, "--allow-network", "--network-marker", str(marker)],
                           text=True, capture_output=True, timeout=10)
    assert ready.returncode == 0 and json.loads(ready.stdout) == {"state": "idle"}


def test_launcher_durable_intake_boolean_is_independent_and_default_off(tmp_path):
    from pathlib import Path
    from runpy import run_path

    launch = run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "apptainer_launch.py"))
    isolated = launch["isolated_corpus"]
    options = {"root": str(tmp_path), "growth": True}
    def env(**overrides):
        return isolated({"isolated_corpus": {**options, **overrides}}, [])[1]
    assert "CITRUS_CORPUS_DURABLE_INTAKE=0" in env()
    assert "CITRUS_CORPUS_DURABLE_INTAKE=1" in env(durable_intake=True)
    with pytest.raises(ValueError, match="boolean"):
        env(durable_intake="true")
    with pytest.raises(ValueError, match="requires"):
        env(growth=False, durable_intake=True)


def test_literature_handoff_keeps_private_query_out_of_payload(monkeypatch):
    from src.corpus_integration import fetch_submit, settings
    from src.research import literature

    monkeypatch.setattr(settings, "durable_intake_enabled", lambda: True)
    writes = []
    monkeypatch.setattr(fetch_submit, "_submit", lambda owner, **payload: writes.append(payload))
    row = {"title": "A study of grove nutrients", "source": "PubMed", "identifier_match": False,
           "field_conflicts": [], "pmid": "123"}
    literature._submit_public_match([row], query="private lab notes", doi="", arxiv_id="", claimed_title="")
    assert not writes
    literature._submit_public_match([row, dict(row)], query=row["title"], doi="", arxiv_id="", claimed_title="")
    assert not writes
    literature._submit_public_match([row], query=row["title"], doi="", arxiv_id="", claimed_title="")
    assert writes == [{"producer": "literature", "doi": "", "pmid": "123", "pmcid": "", "title": ""}]


def test_public_family_and_citation_dispatch_follow_runtime_mode(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from citrus_corpus.models import FusedRank
    from citrus_corpus.publication import Publisher
    from src import auth
    from src.core import capability_policy, tracing
    from src.corpus_integration import read_access
    from src.graph.tool_availability import runtime_env
    from src.retrieval.corpus_registry import load_corpus_registry
    from src.shared_corpus import references, runtime, tool_adapter
    from src.tools import deepseek_search_fetch as fetch

    class Model:
        model_name = "fixture-static-vectors"
        dim = 1024
        fingerprint = sha256(b"fixture-static-vectors/1").hexdigest()

        def embed_query(self, query):
            return [1.0] + [0.0] * 1023

        def embed_passages(self, texts):
            return [self.embed_query(text) for text in texts]

    store = Store.initialize(tmp_path / "isolated")
    intake = Intake(store)
    raw = b"# Methods\n\nCitrus soil organic matter was recorded."
    (store.inbox / "paper.md").write_bytes(raw)
    intake.submit_local("paper.md", expected_sha256=sha256(raw).hexdigest(), actor="fixture")
    prepared = intake.process_one()
    intake.catalog.decide(prepared["document_id"], prepared["version_id"], decision="granted", actor="operator",
                          reason="Fixture license reviewed", evidence="fixture:rights")
    model = Model()
    now = [1000]
    publisher = Publisher(store, embedder=model, reranker=FusedRank(), now=lambda: now[0])
    first_generation = publisher.publish()["generation"]
    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setenv("CITRUS_CORPUS_READ", "1")
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_ROOT", str(store.root))
    monkeypatch.setenv("CITRUS_ISOLATED_CORPUS_EMBEDDING_CACHE", str(tmp_path))
    monkeypatch.setattr(runtime, "_runtime", None)
    monkeypatch.setattr(tracing, "get_user_id", lambda: 7)
    monkeypatch.setattr(auth, "user_by_id", lambda owner: {"user_id": owner, "active": True,
                        "approval_status": "approved", "role": "user"})
    monkeypatch.setattr(capability_policy, "allows", lambda capability: capability == "local_rag")
    monkeypatch.setattr(read_access, "_embedder", lambda cache: model)
    monkeypatch.setattr(fetch, "_enabled", capability_policy.allows)
    env = runtime_env(script_sandbox=False)
    assert env.has("shared_corpus") and "prepare_document" in env.denied
    registry = load_corpus_registry()
    assert "`agent_literature`" in registry.describe_families()
    assert "agent_literature" in registry.search_family_ids
    assert "agent_literature" not in registry.family_ids
    assert len(registry.registered_corpus_ids) == 5

    _, output = fetch._local_hybrid_search_impl("citrus soil", ["agent_literature"], render=False, progress=False)
    row = output["shared_results"][0]
    from src.graph.retrieval_router import decide_retrieval, required_route_groups
    from src.tools.outcome import normalize_tool_outcome, safe_tool_step

    # Exercise the public tool function, exact evidence parser, status adapter,
    # public tool-step summary, and the final route using the same result.
    content, artifact = fetch.local_hybrid_search.func("citrus soil", ["agent_literature"])
    view = json.loads(content)
    assert view["count"] == len(view["candidates"]) == artifact["selected_result_count"] == 1
    assert artifact["main_results"] == [] and len(artifact["shared_results"]) == 1
    assert view["candidates"][0]["source_ref"].startswith("shared:")
    assert list(references.evidence_rows("local_hybrid_search", content))[0][1] == row["read_ref"]
    outcome = normalize_tool_outcome("local_hybrid_search", content, artifact)
    assert outcome.status == "complete" and outcome.coverage == "1 条本地候选"
    assert safe_tool_step("local_hybrid_search", content, artifact)["summary"] == "找到 1 条候选资料"
    query = "请解释柑橘土壤有机质对根系吸收的影响和机制"
    assert not required_route_groups(decide_retrieval(query))

    direct = json.loads(tool_adapter.search_agent_corpus.invoke({"query": "citrus soil"}))
    assert direct["results"][0]["source_family"] == "agent_literature"
    assert direct["_tool_outcome"]["coverage"].startswith("shared_agent_literature:")
    ref = references.read_reference(row)
    assert ref["source_family"] == "agent_literature" and ref["chunk_id"] == row["chunk_id"]
    assert read_access.read_reference_page(ref)["markdown"] == row["excerpt"]
    assert references.read_reference({"read_ref": {**ref, "chunk_id": "invalid"}}) is None

    raw = b"# Methods\n\nUpdated citrus soil measurements used four replicates."
    (store.inbox / "update.md").write_bytes(raw)
    intake.submit_local("update.md", expected_sha256=sha256(raw).hexdigest(), actor="fixture")
    update = intake.process_one()
    intake.catalog.decide(update["document_id"], update["version_id"], decision="granted", actor="operator",
                          reason="Fixture license reviewed", evidence="fixture:rights")
    now[0] += 301
    next_generation = publisher.publish()["generation"]
    assert next_generation != first_generation
    path = store.root / "generations" / next_generation / "chunks.jsonl"
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    content, fallback = fetch.local_hybrid_search.func("citrus soil", ["agent_literature"])
    shown = json.loads(content)
    assert shown["shared_requested_generation"] == next_generation
    assert shown["shared_generation"] == first_generation
    assert shown["shared_degradation_reason"] == "active_generation_integrity_invalid"
    assert fallback["shared_results"][0]["read_ref"]["index_generation"] == first_generation
    assert normalize_tool_outcome("local_hybrid_search", content, fallback).status == "partial"
    direct_fallback = json.loads(tool_adapter.search_agent_corpus.invoke({"query": "citrus soil"}))
    assert direct_fallback["shared_generation"] == first_generation
    assert direct_fallback["_tool_outcome"]["reason_code"] == "active_generation_integrity_invalid"
    with monkeypatch.context() as patch:
        patch.setenv("CITRUS_CORPUS_READ_GENERATION", next_generation)
        _, fixed = fetch.local_hybrid_search.func("citrus soil", ["agent_literature"])
        assert fixed["shared_generation"] is None and not fixed["shared_results"]
    path.write_bytes(original)
    assert publisher.rollback(first_generation)["state"] == "publication_held"
    _, restored = fetch.local_hybrid_search.func("citrus soil", ["agent_literature"])
    assert restored["shared_requested_generation"] == restored["shared_generation"] == first_generation
    assert restored["shared_degradation_reason"] is None
    held_path = store.root / "generations" / first_generation / "chunks.jsonl"
    held_original = held_path.read_bytes()
    held_path.write_bytes(held_original + b"\n")
    _, unavailable = fetch.local_hybrid_search.func("citrus soil", ["agent_literature"])
    assert unavailable["shared_requested_generation"] == first_generation
    assert unavailable["shared_generation"] is None and not unavailable["shared_results"]
    held_path.write_bytes(held_original)

    old = {key: value for key, value in ref.items() if key not in ("chunk_id", "source_family")}
    assert references.read_reference({"read_ref": old}) == old
    history = []

    class HistoricalReader:
        def reference_visible(self, reference):
            history.append(reference)
            return True

    monkeypatch.setattr(read_access, "legacy_reader", HistoricalReader)
    assert read_access.reference_visible(old) and history == [old]
    monkeypatch.delenv("CITRUS_CORPUS_READ")
    assert not read_access.reference_visible(ref)
    assert read_access.reference_visible(old)

    monkeypatch.setenv("CITRUS_CORPUS_MODE", "legacy")

    class LegacyRuntime:
        unavailable = False
        generation = "legacy_generation"

        def search(self, query, *, limit):
            return [{**row, "read_ref": old, "index_generation": self.generation}]

    monkeypatch.setattr(runtime, "_runtime", LegacyRuntime())
    monkeypatch.setattr(runtime, "retrieval_reader", nullcontext)
    _, legacy = fetch._local_hybrid_search_impl("citrus soil", ["agent_literature"], render=False, progress=False)
    assert legacy["shared_generation"] == "legacy_generation"


def test_deployment_isolates_the_corpus_root_and_pins_experiment(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    launcher = Path(__file__).resolve().parents[2] / "scripts" / "apptainer_launch.py"
    spec = spec_from_file_location("corpus_launch_fixture", launcher)
    launch = module_from_spec(spec)
    spec.loader.exec_module(launch)
    base = tmp_path / "base"
    base.mkdir()
    cache = tmp_path / "encoder"
    cache.mkdir()
    isolated = Store.initialize(tmp_path / "independent")
    generation = "g" + "7" * 30
    options = {"root": str(isolated.root), "read": True, "growth": False,
               "embedding_cache": str(cache), "read_generation": generation}
    existing = [(str(base), "ro")]
    assert launch.isolated_corpus({}, existing) == ([], [])
    mounts, env = launch.isolated_corpus({"isolated_corpus": options}, existing)
    assert mounts == [(str(isolated.root), "rw"), (str(cache), "ro")]
    assert "CITRUS_CORPUS_MODE=isolated" in env
    assert "CITRUS_CORPUS_FETCH_GROWTH=0" in env
    assert f"CITRUS_CORPUS_READ_GENERATION={generation}" in env
    with pytest.raises(ValueError, match="conflicting"):
        launch.isolated_corpus({"isolated_corpus": options, "shared_literature_single_web": True}, existing)
    with pytest.raises(ValueError, match="overlaps"):
        launch.isolated_corpus({"isolated_corpus": {**options, "root": str(base)}}, existing)


def test_isolated_mode_blocks_legacy_candidate_write_entry_points(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from src.api import main
    from src.core import corpus_review

    monkeypatch.setenv("CITRUS_CORPUS_MODE", "isolated")
    monkeypatch.setattr(main, "require_admin", lambda request: {"user_id": 7, "id": 7})

    def unexpected(*args, **kwargs):
        pytest.fail("isolated mode must not touch the legacy review write path")

    for name in ("supplement_fulltext", "decide", "consent_enabled", "archive_upload"):
        monkeypatch.setattr(corpus_review, name, unexpected)
    for handler in (main.admin_corpus_fulltext, main.admin_corpus_approve,
                    main.admin_corpus_reject, main.admin_corpus_reopen):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(handler(1, None))
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "legacy_corpus_read_only"
    token = main._REVIEW_UPLOAD_SCOPE.set((7, 1))
    try:
        main._capture_upload_candidate("fixture.pdf", "fixture", b"fixture")
    finally:
        main._REVIEW_UPLOAD_SCOPE.reset(token)
