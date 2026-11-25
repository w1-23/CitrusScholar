from hashlib import sha256
import json
import sqlite3
import time

import pytest

from citrus_corpus.auto_intake import AutoIntake
from citrus_corpus.catalog import Catalog
from citrus_corpus.intake import Intake
from citrus_corpus.models import FusedRank
from citrus_corpus.publication import Publisher
from citrus_corpus.queue import IntakeQueue, LEASE_SECONDS
from citrus_corpus.reader import Reader
from citrus_corpus.store import Store
from citrus_corpus.tests.corpus_test import FixtureEmbedder


DOI = "10.1234/greengrove"
PMID = "17218991"
PMCID = "PMC1766372"
PUBLIC = f"https://pmc.ncbi.nlm.nih.gov/articles/{PMCID}/"
CLOUD = "https://pmc-oa-opendata.s3.amazonaws.com"
TITLE = "A grove nutrient study"
FIRST = b"Fruit trees absorbed more soil phosphate after irrigation."
SECOND = b"Fruit trees absorbed less soil phosphate after introducing mulch."


@pytest.fixture
def store(tmp_path):
    return Store.initialize(tmp_path / "isolated")


def source(revision=1):
    version = f"{PMCID}.{revision}"
    return {"text_url": f"{CLOUD}/{version}/{version}.txt",
            "evidence_url": f"{CLOUD}/metadata/{version}.json"}


def observe(adapter, raw=FIRST, revision=1, **overrides):
    values = {"pmcid": PMCID, "title": TITLE, "source_url": PUBLIC,
              "license_id": "cc-by", "raw": raw, **source(revision), **overrides}
    return adapter.register(**values)


def events(store):
    with store.read() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM rights_events ORDER BY seq")]


def test_metadata_only_durable_aliases_conflicts_and_promotion(store):
    adapter = AutoIntake(store)
    assert adapter.lookup(pmid=PMID) is None
    first = adapter.register(pmid=PMID, title=TITLE, source_url=f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/")
    assert first["identity"] == f"pmid:{PMID}" and first["state"] == "metadata_only"
    adapter.register(pmcid=PMCID, title=TITLE, source_url=PUBLIC)
    merged = adapter.register(doi=f"https://doi.org/{DOI.upper()}", pmid=PMID, pmcid=PMCID.lower(),
                              title=TITLE, source_url=f"https://doi.org/{DOI}")
    assert merged["identity"] == f"doi:{DOI}"
    adapter.register(pmcid=PMCID, title="Minor alternate title", source_url=PUBLIC)
    reloaded = AutoIntake(Store(store.root))
    for alias in ({"pmid": PMID}, {"pmcid": PMCID}, {"doi": DOI}):
        record = reloaded.lookup(**alias)
        assert record["identity"] == f"doi:{DOI}"
        assert record["title"] == TITLE
        assert record["source_url"] == f"https://doi.org/{DOI}"
    with pytest.raises(ValueError, match="bibliography_identity_conflict"):
        adapter.register(doi="10.1234/unrelated", pmid=PMID, title="Another work", source_url=PUBLIC)
    assert reloaded.lookup(doi="10.1234/unrelated") is None
    assert not list(store.originals.iterdir()) and not list(store.documents.iterdir())
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM corpus_bibliography").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 0
    assert Catalog(store).versions() == [] and events(store) == []


@pytest.mark.parametrize("overrides,code", [
    ({"license_id": "cc-by-nc"}, "rights_license_inadmissible"),
    ({"license_id": "https://creative.example/licenses/by/4.0"}, "rights_license_inadmissible"),
    ({"license_id": "https://evil.example/creativecommons.org/licenses/by/4.0"}, "rights_license_inadmissible"),
    ({"evidence_url": source()["evidence_url"] + "?something=1"}, "automatic_pmc_evidence_invalid"),
    ({"evidence_url": source()["evidence_url"].replace(CLOUD, "https://provider.example")},
     "automatic_pmc_evidence_invalid"),
    ({"text_url": source(2)["text_url"]}, "automatic_pmc_version_mismatch"),
    ({"pmcid": "PMC991"}, "automatic_pmc_version_mismatch"),
    ({"pmcid": "", "doi": DOI}, "automatic_pmc_version_mismatch"),
    ({"raw": b""}, "source_size_invalid"),
])
def test_untrusted_or_mismatched_raw_is_not_persisted(store, overrides, code):
    adapter = AutoIntake(store)
    with pytest.raises(ValueError, match=code):
        observe(adapter, **overrides)
    assert not list(store.originals.iterdir()) and not list(store.documents.iterdir())
    assert adapter.lookup(pmcid=PMCID) is None


def test_exact_version_events_idempotency_revocation_and_different_version(store):
    adapter, worker = AutoIntake(store), Intake(store)
    first = observe(adapter)
    assert first["state"] == "queued"
    assert observe(adapter, pmid=PMID)["id"] == first["id"]
    assert events(store) == [] and worker.catalog.versions() == []
    prepared = worker.process_one()
    doc, ver = prepared["document_id"], prepared["version_id"]
    assert prepared["state"] == "prepared" and worker.catalog.visible(doc, ver)
    worker.catalog.decide(doc, ver, decision="revoked", actor="operator", reason="Exclude this version")
    assert observe(adapter, doi=DOI, pmid=PMID)["id"] == first["id"]
    assert observe(adapter, revision=2)["id"] == first["id"]  # Same bytes, no phantom version or regrant.
    assert worker.process_one() is None and not worker.catalog.visible(doc, ver)
    revision = observe(adapter, raw=SECOND, revision=2)
    prepared2 = worker.process_one()
    assert prepared2["id"] == revision["id"] and prepared2["document_id"] == doc
    assert prepared2["version_id"] != ver and worker.catalog.visible(doc, prepared2["version_id"])
    assert not worker.catalog.visible(doc, ver)
    assert worker.catalog.versions()[0]["previous_version"] == ver
    audit = events(store)
    assert [event["decision"] for event in audit] == ["granted", "revoked", "granted"]
    assert audit[0]["basis"] == "ncbi_pmc_cloud" and audit[0]["raw_sha256"] == sha256(FIRST).hexdigest()
    assert json.loads(audit[0]["evidence"]) == {"locator": source()["evidence_url"], "license": "cc-by",
                                               "text_url": source()["text_url"], "pmc_version": f"{PMCID}.1"}
    assert json.loads(audit[2]["evidence"])["pmc_version"] == f"{PMCID}.2"


def test_worker_grant_and_version_are_one_transaction(store):
    adapter = AutoIntake(store)
    clock = [time.time() + 1]
    worker = Intake(store, queue=IntakeQueue(store, now=lambda: clock[0]))
    submitted = observe(adapter)
    with store.write() as conn:
        conn.execute("""CREATE TRIGGER fail_grant BEFORE INSERT ON rights_events BEGIN
                        SELECT RAISE(ABORT, 'simulated grant journal failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated grant journal failure"):
        worker.process_one()
    assert worker.catalog.versions() == [] and events(store) == []
    assert worker.queue.status(submitted["id"])["state"] == "leased"
    with store.write() as conn:
        conn.execute("DROP TRIGGER fail_grant")
    clock[0] += LEASE_SECONDS + 1
    completed = worker.process_one()
    assert completed["state"] == "prepared" and completed["attempts"] == 2
    assert len(events(store)) == 1 and worker.catalog.visible(completed["document_id"], completed["version_id"])


def _saved(worker, name, raw, **identifier):
    (worker.store.inbox / name).write_bytes(raw)
    worker.submit_local(name, actor="operator", expected_sha256=sha256(raw).hexdigest(), **identifier)
    return worker.process_one()


def test_automatic_evidence_grants_an_existing_review_only_exact_version(store):
    worker = Intake(store)
    manual = _saved(worker, "manual.txt", FIRST, doi=DOI)
    doc, version = manual["document_id"], manual["version_id"]
    assert not worker.catalog.visible(doc, version)
    auto = observe(AutoIntake(store), doi=DOI, license_id="https://creativecommons.org/licenses/by/4.0/")
    completed = worker.process_one()
    assert completed["id"] == auto["id"] and completed["id"] != manual["id"]
    assert (completed["document_id"], completed["version_id"]) == (doc, version)
    assert len(worker.catalog.versions()) == 1 and worker.catalog.visible(doc, version)


def test_legacy_and_previously_separate_histories_retain_physical_references(store):
    adapter, worker = AutoIntake(store), Intake(store)
    from_url = _saved(worker, "older.txt", FIRST, url=PUBLIC)
    from_doi = _saved(worker, "newer.txt", SECOND, doi=DOI)
    for row in (from_url, from_doi):
        worker.catalog.decide(row["document_id"], row["version_id"], decision="granted", actor="operator",
                              reason="Review", evidence="fixture:license")
    adapter.register(pmcid=PMCID, title=TITLE, source_url=PUBLIC)
    adapter.register(doi=DOI, title=TITLE, source_url=f"https://doi.org/{DOI}")
    assert from_url["document_id"] != from_doi["document_id"]
    adapter.register(doi=DOI, pmcid=PMCID, title=TITLE, source_url=f"https://doi.org/{DOI}")
    with store.read() as conn:
        selected = worker.catalog.selected(conn, 100)
    assert [row["document_id"] for row in selected] == [from_doi["document_id"]]
    for row, raw in ((from_url, FIRST), (from_doi, SECOND)):
        assert worker.catalog.document(row["document_id"], row["version_id"])[1].startswith(raw.decode())
    observe(adapter, raw=SECOND, doi=DOI, revision=2)
    duplicate = worker.process_one()
    assert duplicate["document_id"] == from_doi["document_id"]
    assert len(worker.catalog.versions()) == 2 and len(events(store)) == 2
    observe(adapter, raw=b"Third physical revision with a longer observation of nutrient availability.", doi=DOI, revision=3)
    latest = worker.process_one()
    assert latest["document_id"] == from_url["document_id"]
    assert worker.catalog.versions()[0]["previous_version"] == from_url["version_id"]
    assert worker.catalog.document(from_doi["document_id"], from_doi["version_id"])[1].startswith(SECOND.decode())


def test_legacy_operator_revoke_blocks_first_automatic_observation(store):
    adapter, worker = AutoIntake(store), Intake(store)
    manual = _saved(worker, "withdrawn.txt", FIRST, url=PUBLIC)
    doc, version = manual["document_id"], manual["version_id"]
    worker.catalog.decide(doc, version, decision="revoked", actor="operator", reason="Exclude this version")
    observe(adapter, doi=DOI, pmid=PMID)
    completed = worker.process_one()
    assert completed["state"] == "prepared"
    assert (completed["document_id"], completed["version_id"]) == (doc, version)
    assert [event["decision"] for event in events(store)] == ["revoked"]
    assert not worker.catalog.visible(doc, version)


def test_worker_then_hot_publication_preserves_pinned_readers(store):
    pytest.importorskip("lancedb")
    adapter, worker, reader = AutoIntake(store), Intake(store), Reader(store)
    idle, clock = [False], [1000]
    models = {"embedder": FixtureEmbedder(), "reranker": FusedRank()}
    publisher = Publisher(store, **models, idle=lambda: idle[0], now=lambda: clock[0])
    observe(adapter)
    prepared = worker.process_one()
    assert publisher.publish()["state"] == "waiting_for_idle"
    idle[0] = True
    first_gen = publisher.publish()["generation"]
    with reader.pin(**models) as pinned:
        old = pinned.search("phosphate")[0]
        assert old["version_id"] == prepared["version_id"]
        assert reader.reference(old["read_ref"])["markdown"] == old["text"]
        observe(adapter, raw=SECOND, revision=2, doi=DOI)
        latest = worker.process_one()
        assert publisher.publish()["state"] == "waiting_for_budget"
        clock[0] += 301
        assert publisher.publish()["generation"] != first_gen
        assert pinned.search("phosphate")[0]["version_id"] == prepared["version_id"]
        with reader.pin(**models) as hot:
            assert hot.search("mulch")[0]["version_id"] == latest["version_id"]
        worker.catalog.decide(prepared["document_id"], prepared["version_id"], decision="revoked",
                              actor="operator", reason="Exclude this version")
        assert pinned.search("phosphate") == []
        with pytest.raises(LookupError):
            reader.reference(old["read_ref"])
