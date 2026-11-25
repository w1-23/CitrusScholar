from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from citrus_corpus.acquisition import ExplicitAcquirer
from citrus_corpus.extract import extract_markdown
from citrus_corpus.intake import Intake
from citrus_corpus.models import FusedRank
from citrus_corpus.pointers import read_pointer
from citrus_corpus.prepare import PreparationError
from citrus_corpus.publication import Publisher
from citrus_corpus.queue import IntakeQueue, LEASE_SECONDS
from citrus_corpus.reader import Reader
from citrus_corpus.store import Store


class FixtureEmbedder:
    model_name = "test/local-token-vector"
    fingerprint = sha256(b"local-token-vector version 1").hexdigest()
    dim = 1024

    def __init__(self):
        self.embedded = 0
        self.fail = False

    def embed_query(self, text):
        vector = [0.0] * self.dim
        vector[0] = 1.0
        for word in text.lower().split():
            vector[1 + int(sha256(word.encode()).hexdigest(), 16) % (self.dim - 1)] += 1.0
        return vector

    def embed_passages(self, texts):
        if self.fail:
            raise RuntimeError("model_unavailable")
        self.embedded += len(texts)
        return [self.embed_query(text) for text in texts]


@pytest.fixture
def store(tmp_path):
    return Store.initialize(tmp_path / "sixth")


def saved(intake, name, raw, actor="owner", doi="10.1234/work"):
    (intake.store.inbox / name).write_bytes(raw)
    return intake.submit_local(name, expected_sha256=sha256(raw).hexdigest(), actor=actor, doi=doi)


def grant(catalog, result, decision="granted"):
    catalog.decide(result["document_id"], result["version_id"], decision=decision,
                   actor="reviewer", reason="Original rights checked", evidence="fixture:open-rights")


def test_extraction_and_scoped_acquisition(store):
    intake = Intake(store)
    calls = []
    raw = b"<html><body><article><h1>Field study</h1><p>Measurement methods.</p><p>References <a href='https://b.example/work'>B</a>.</p></article></body></html>"

    def transport(url, remaining):
        calls.append(url)
        return None, raw, "text/html"

    acquire = ExplicitAcquirer(transport=transport)
    assert intake.process_one(acquire=acquire) is None
    assert calls == []
    job = intake.submit_explicit(actor="user", url="https://a.example/work")
    assert intake.process_one() is None
    assert intake.queue.status(job["id"])["state"] == "queued"
    result = intake.process_one(acquire=acquire)
    assert result["state"] == "prepared"
    assert ["https://a.example/work"] == calls
    assert result["raw_sha256"] == sha256(raw).hexdigest()
    assert not intake.catalog.visible(result["document_id"], result["version_id"])
    assert intake.submit_explicit(actor="user2", url="https://a.example/work#methods")["id"] == job["id"]
    assert intake.process_one(acquire=acquire) is None
    assert len(calls) == 1
    denied = ExplicitAcquirer(transport=lambda url, remaining: ("https://private.example/a", None, None))
    denied_job = intake.submit_explicit(actor="user", doi="10.1234/other")
    assert intake.process_one(acquire=denied)["id"] == denied_job["id"]
    assert intake.queue.status(denied_job["id"])["error"] == "source_redirect_denied"
    xml = b"<article><front><article-title>River Study</article-title></front><body><sec><title>Methods</title><p>Measurements and sampling.</p></sec></body></article>"
    assert "## Methods" in extract_markdown(xml, "xml")
    jats = b'<!DOCTYPE article PUBLIC "-//NLM//DTD Journal Publishing DTD v3.0" "http://dtd.nlm.nih.gov/publishing/3.0/journalpublishing3.dtd"'
    assert "## Methods" in extract_markdown(jats + b">" + xml, "xml")
    with pytest.raises(PreparationError, match="invalid_xml"):
        extract_markdown(jats + b' [<!ENTITY custom "extra">]>' + xml, "xml")
    with pytest.raises(PreparationError, match="invalid_xml"):
        extract_markdown(b'<!DOCTYPE article SYSTEM "https://b.example/x"><article/>', "xml")
    pdf = pytest.importorskip("fitz")
    with pdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 60), "River sampling studies quantify nutrient concentrations.")
        data = document.tobytes()
    assert "nutrient concentrations" in extract_markdown(data, "pdf")
    with pytest.raises(PreparationError, match="invalid_pdf"):
        extract_markdown(b"invalid", "pdf")


def test_concurrent_intake_lease_and_queue_bounds(store, monkeypatch):
    now = [1000]
    queue = IntakeQueue(store, now=lambda: now[0])
    intake = Intake(store, queue=queue)
    raw = b"# Methods\n\nSoil conditions were measured.\n"
    (store.inbox / "paper.md").write_bytes(raw)

    def submit(actor):
        return intake.submit_local("paper.md", expected_sha256=sha256(raw).hexdigest(),
                                   actor=actor, doi="HTTPS://doi.org/10.1234/Work")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(submit, ["one", "two"])
    assert first["id"] == second["id"]
    expired = queue.claim()
    assert intake.process_one() is None
    now[0] += LEASE_SECONDS + 1
    result = intake.process_one()
    assert result["attempts"] == 2 and result["state"] == "prepared"
    with pytest.raises(ValueError, match="lease_lost"):
        queue.fail(expired, "worker_interrupted")
    with store.writer(), pytest.raises(ValueError, match="writer_busy"):
        intake.process_one()
    monkeypatch.setattr("citrus_corpus.queue.MAX_PENDING", 1)
    intake.submit_explicit(actor="operator", doi="10.1234/test")
    with pytest.raises(ValueError, match="queue_full"):
        intake.submit_explicit(actor="operator", doi="10.1234/test2")


def test_publish_pinned_reads_rights_and_recovery(store, monkeypatch):
    pytest.importorskip("lancedb")
    intake, reader = Intake(store), Reader(store)
    now = [1000]
    idle = [False]
    model = FixtureEmbedder()
    models = {"embedder": model, "reranker": FusedRank()}
    publisher = Publisher(store, idle=lambda: idle[0], now=lambda: now[0], **models)
    raw = b"# Methods\n\nField plots were sampled for leaf nutrient analysis.\n"
    saved(intake, "first.md", raw)
    initial = intake.process_one()
    grant(intake.catalog, initial)
    assert publisher.publish()["state"] == "waiting_for_idle"
    assert read_pointer(store.root) is None
    idle[0] = True
    generation0 = publisher.publish()["generation"]
    assert publisher.publish()["generation"] == generation0
    assert model.embedded == 1
    saved(intake, "revised.txt", b"Revised plot sampling used randomized blocks and moisture sensors.")
    revised = intake.process_one()
    assert revised["document_id"] == initial["document_id"] and revised["version_id"] != initial["version_id"]
    assert intake.catalog.versions()[0]["previous_version"] == initial["version_id"]
    grant(intake.catalog, revised)
    assert publisher.publish()["state"] == "waiting_for_budget"
    now[0] += 301
    with reader.pin(**models) as pinned:
        row0 = pinned.search("leaf nutrient")[0]
        assert reader.reference(row0["read_ref"])["markdown"] == row0["text"]
        full = reader.reference(row0["read_ref"], full=True)
        assert full["markdown"].startswith("# Methods\n\n")
        assert full["markdown"][full["start"]:full["end"]] == row0["text"]
        assert sha256(full["markdown"].encode()).hexdigest() == full["md_sha256"]
        with pytest.raises(LookupError):
            reader.reference({**row0["read_ref"], "md_sha256": "0" * 64}, full=True)
        with pytest.raises(LookupError):
            reader.reference({**row0["read_ref"], "start": row0["read_ref"]["start"] + 1})
        model.fail = True
        with pytest.raises(ValueError, match="embedding_failed"):
            publisher.publish()
        assert read_pointer(store.root) == generation0
        model.fail = False
        now[0] += 301
        original_mark = Publisher._mark_published

        def broken_mark(*args):
            raise OSError("simulated lost ack")

        monkeypatch.setattr(Publisher, "_mark_published", broken_mark)
        with pytest.raises(OSError):
            publisher.publish()
        generation1 = read_pointer(store.root)
        assert generation1 != generation0
        assert pinned.search("leaf")[0]["index_generation"] == generation0
        with Reader(Store(store.root)).pin(**models) as changed:
            assert changed.search("randomized blocks")[0]["version_id"] == revised["version_id"]
        with reader.pin(**models, generation=generation0) as experiment:
            assert experiment.search("leaf")[0]["index_generation"] == generation0
        with pytest.raises(LookupError, match="corpus_generation_unknown"):
            with reader.pin(**models, generation="g" + "0" * 30):
                pytest.fail("Unknown experiment generation must not fall back")
        monkeypatch.setattr(Publisher, "_mark_published", original_mark)
        assert publisher.publish()["generation"] == generation1
        chunks_path = store.root / "generations" / generation1 / "chunks.jsonl"
        original_chunks = chunks_path.read_bytes()
        chunks_path.write_bytes(original_chunks + b"\n")
        with reader.pin(**models) as fallback:
            assert fallback.generation == generation0
        chunks_path.write_bytes(original_chunks)
        grant(intake.catalog, initial, "revoked")
        assert pinned.search("leaf") == []
        with reader.pin(**models, generation=generation0) as experiment:
            assert experiment.search("leaf") == []
        with pytest.raises(LookupError):
            reader.reference(row0["read_ref"])
        with pytest.raises(LookupError):
            reader.reference(row0["read_ref"], full=True)
    assert publisher.rollback(generation0)["generation"] == generation0
    with reader.pin(**models) as restored:
        assert restored.search("leaf") == []
    assert len(intake.catalog.versions()) == 2


def test_publication_window_reacts_to_load_disk_and_marker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from citrus_corpus import __main__ as cli

    marker = tmp_path / "idle"
    load = [6.0]
    free = [32 * 1024 ** 2]
    monkeypatch.setattr(cli.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda root: SimpleNamespace(free=free[0]))
    available = cli._publication_window(tmp_path, marker, 0.75, 16)
    assert not available()
    marker.touch()
    assert not available()
    load[0] = 2.0
    assert available()
    free[0] = 1
    assert not available()
    with pytest.raises(ValueError, match="corpus_publication_budget_invalid"):
        cli._publication_window(tmp_path, marker, float("nan"), 16)
