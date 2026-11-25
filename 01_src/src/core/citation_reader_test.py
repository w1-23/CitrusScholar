from hashlib import sha256
import json
import sqlite3

import pytest

from citrus_retrieval.evidence import EvidenceStore
from src import config
from src.core import corpus_source, jobs, turn_source_ledger, turn_sources
from src.retrieval import evidence_adapter
from src.retrieval.corpus_registry import default_registry_payload, deterministic_id


@pytest.fixture
def cited_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    data = tmp_path / "data"
    monkeypatch.setattr(corpus_source, "DATA_DIR", data)
    monkeypatch.setenv("CITRUSQA_CORPUS_REGISTRY", "")
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "sessions.db") as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT, owner_user_id INT, deleted_at TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('session', 17, NULL)")
    monkeypatch.setattr(jobs, "get_job", lambda turn: ({"owner_user_id": 17, "session_id": "session"}
                                                       if turn == "turn" else None))
    generation = "g20261125T110000Z_abcdef01"
    corpus, paper = "cqa_literature", "paper_fixture_repeat"
    genroot = data / "corpus_generations" / generation
    chunks = genroot / "corpora" / corpus / "chunks.jsonl"
    chunks.parent.mkdir(parents=True)
    registry_path = genroot / "corpus_registry.json"
    registry = default_registry_payload()
    registry["corpora"][corpus]["chunks_path"] = chunks.relative_to(data).as_posix()
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    active = data / "active_index.json"
    active.write_text(json.dumps({"generation": generation, "registry_path": registry_path.relative_to(data).as_posix()}))
    repeated = "🍊**同一段** with UTF-8 and markup."
    rows = []
    for i, text in enumerate([repeated, repeated, "结尾内容"]):
        rows.append({"corpus_id": corpus, "paper_id": paper, "document_id": paper,
                     "source_file": "nowhere.md", "section_name": "Methods", "chunk_index": i,
                     "chunk_id": deterministic_id(corpus, paper, i), "text": text,
                     "prev_chunk_id": f"{paper}::{i-1:04d}" if i else "",
                     "next_chunk_id": f"{paper}::{i+1:04d}" if i < 2 else ""})

    def write_chunks():
        chunks.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    write_chunks()
    store = EvidenceStore(evidence_adapter._session_root(17, "session") / evidence_adapter._component("turn") /
                          evidence_adapter._component(generation), owner=17, session_id="session",
                          turn_id="turn", generation=generation)
    hit = rows[1]
    locator = {**{key: hit[key] for key in ("corpus_id", "paper_id", "chunk_index", "chunk_id")},
               "index_generation": generation, "input_sha256": sha256(repeated.encode()).hexdigest()}
    saved = store.put({**hit, "locator": locator}, query_ref="q")
    ref = {"ref": saved["ref"], "turn_id": "turn", "generation": generation, "start": 1, "end": 9}
    key = turn_source_ledger.reference_key("cite", ref)
    ledger = {"kind": "local", "source_key": key, "read_ref": json.dumps(ref), "title": "Fixture"}
    monkeypatch.setattr(turn_source_ledger, "fetch_rows", lambda sid, rid: [ledger] if (sid, rid) == ("session", "request") else [])
    monkeypatch.setattr(turn_source_ledger, "find_local_citation", lambda key: {**ledger, "session_id": "session"})
    user = {"user_id": 17, "role": "user", "active": True, "approval_status": "approved"}
    item = {"kind": "local", "record_key": key, "ref_id": "L4"}

    def read(**kwargs):
        return turn_sources.read_cited_source(item, kwargs.pop("user", user),
            session_id=kwargs.pop("session_id", "session"), request_id=kwargs.pop("request_id", "request"), **kwargs)

    return read, item, user, repeated, rows, write_chunks, active, registry_path


def test_complete_legacy_chain_and_exact_range_contract(cited_legacy):
    read, item, user, repeated, *_ = cited_legacy
    exact = turn_sources.read_local_citation(item["record_key"], user)
    assert exact["kind"] == "stored_evidence"
    assert exact["markdown"] == repeated[1:9]
    full = read()
    assert full["kind"] == "reconstructed_markdown"
    assert full["offset_unit"] == "unicode_code_point"
    markdown = full["markdown"]
    assert markdown.count(repeated) == 2 and markdown.endswith("结尾内容")
    assert full["hit_start"] == markdown.rindex(repeated)
    assert markdown[full["hit_start"]:full["hit_end"]] == repeated
    assert full["md_sha256"] == sha256(markdown.encode()).hexdigest()
    page = read(length=15)
    assert page["markdown"] == repeated
    assert read(length=15, offset=page["previous_start"])["next_start"] == page["start"]
    assert read(length=15, offset=page["next_start"])["previous_start"] == page["start"]
    assert read(offset=len(markdown)) is None
    assert read(request_id="wrong-turn") is None
    assert read(user={**user, "user_id": 18, "role": "admin"}) is None
    assert read(session_id="wrong-session") is None


def test_legacy_mismatch_falls_back_and_live_revocation(cited_legacy, monkeypatch):
    read, _, _, repeated, rows, write_chunks, _, registry_path = cited_legacy
    rows[2]["prev_chunk_id"] = "forged-link"
    write_chunks()
    fallback = read()
    assert fallback["coverage"] == "chunk" and fallback["markdown"] == repeated
    rows[2]["prev_chunk_id"] = f'{rows[2]["paper_id"]}::0001'
    rows[1]["text"] = "modified bytes in the chunk file"
    write_chunks()
    assert read()["coverage"] == "chunk"
    from src.core.capability_policy import CapabilityPolicy, bind_policy
    with bind_policy(CapabilityPolicy(local_rag=False)):
        excerpt = read()
        assert excerpt["coverage"] == "excerpt" and excerpt["markdown"] == repeated[1:9]
    registry = json.loads(registry_path.read_text())
    registry["corpora"].pop("cqa_literature")
    registry["corpus_access"].pop("cqa_literature", None)
    registry_path.write_text(json.dumps(registry))
    # Point the evidence adapter at the same real registry for its live access check.
    monkeypatch.setenv("CITRUSQA_CORPUS_REGISTRY", str(registry_path))
    assert read() is None


def test_shared_selector_is_exact_chunk_specific():
    ref = {"source_family": "agent_curated_literature", "index_generation": "fixture_generation",
           "document_id": "fixture_document", "version_id": "fixture_version", "md_sha256": "a" * 64,
           "start": 1, "end": 11, "chunk_id": "b" * 32}
    from citrus_corpus.contract import ISOLATED
    ref["source_family"] = ISOLATED.family_id
    value = {"kind": "shared", "read_ref": ref, "ref_id": "L1"}
    key = turn_sources.citation_bubble_id(value)
    assert key == turn_sources.citation_bubble_id({**value, "ref_id": "L99"})
    assert key != turn_sources.citation_bubble_id({**value, "read_ref": {**ref, "start": 2}})
    from src.core.public_delivery import project_citation_bubbles
    assert project_citation_bubbles([{**value, "reader_id": key}])[0]["reader_id"] == key
