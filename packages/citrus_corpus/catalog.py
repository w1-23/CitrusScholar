"""Version continuity, rights audit and live visibility for isolated works."""
from hashlib import sha256
import json
import time

from . import bibliography, chunks, prepare, rights
from .contract import ISOLATED
from .extract import PARSER_VERSIONS


class Catalog:
    def __init__(self, store):
        self.store = store

    def record(self, queue, job, raw_digest, source_format, markdown, *, allowed=lambda: True):
        with self.store.write(timeout=2) as conn:
            if not allowed():
                raise prepare.PreparationError("network_scope_denied")
            storage, canonical, aliases = bibliography.version_group(conn, job["identity"])
            # Metadata registrations can connect aliases between the early
            # duplicate check and extraction. Recheck inside the final write.
            if self._complete_duplicate(conn, queue, job, raw_digest, aliases):
                return
            document_id = "doc_" + sha256(storage.encode("utf-8")).hexdigest()[:32]
            version_id = "v_" + raw_digest[:40]
            metadata = self._prepare_document(job, canonical, document_id, version_id, raw_digest,
                                              source_format, markdown)
            previous = conn.execute("SELECT version_id FROM versions WHERE document_id=? ORDER BY seq DESC LIMIT 1",
                                    (document_id,)).fetchone()
            conn.execute("""INSERT INTO versions (identity,document_id,version_id,raw_sha256,markdown_sha256,
                         chunk_count,previous_version,source_format,rights,created)
                         VALUES (?,?,?,?,?,?,?,?,'needs_review',?)""",
                         (storage, document_id, version_id, raw_digest, metadata["content_sha256"],
                          metadata["chunk_count"], previous[0] if previous else None, source_format, time.time()))
            if not allowed():
                raise prepare.PreparationError("network_scope_denied")
            self._automatic_grant(conn, job, document_id, version_id, raw_digest)
            queue.complete(conn, job, document=document_id, version=version_id, digest=raw_digest)

    def _prepare_document(self, job, canonical, document_id, version_id, raw_digest, source_format, markdown):
        outline = chunks.section_outline(markdown)
        metadata = {
            "schema_version": chunks.SCHEMA_VERSION, "document_id": document_id, "version_id": version_id,
            "title": prepare.public_text(job["request"].get("title"), 512),
            "doi": canonical[4:] if canonical.startswith("doi:") else "",
            "source_kind": job["request"]["kind"], "source_format": source_format,
            "source_parser": PARSER_VERSIONS[source_format],
            "source_sha256": raw_digest, "content_sha256": sha256(markdown.encode("utf-8")).hexdigest(),
            "char_count": len(markdown), "prepared_at": prepare.utc_now(),
            "section_path": [row["section_name"] for row in outline if row["section_level"]],
        }
        metadata["chunk_count"] = len(chunks.chunk_document(metadata, markdown, spec=ISOLATED))
        prepare.write_document(self.store.documents, document_id, version_id, metadata, markdown)
        return metadata

    def complete_duplicate(self, queue, job, raw_digest, *, allowed=lambda: True):
        with self.store.write(timeout=2) as conn:
            if not allowed():
                raise prepare.PreparationError("network_scope_denied")
            _, _, aliases = bibliography.version_group(conn, job["identity"])
            return self._complete_duplicate(conn, queue, job, raw_digest, aliases)

    def _complete_duplicate(self, conn, queue, job, raw_digest, aliases):
        marks = ",".join("?" for _ in aliases)
        versions = conn.execute(f"""SELECT document_id,version_id,rights,EXISTS (
                                  SELECT 1 FROM rights_events AS event WHERE event.document_id=v.document_id
                                  AND event.version_id=v.version_id AND event.decision='revoked') AS revoked
                                  FROM versions AS v WHERE identity IN ({marks}) AND raw_sha256=?
                                  ORDER BY seq DESC""", (*aliases, raw_digest)).fetchall()
        if not versions:
            return False
        previous = versions[0]
        if previous["rights"] == "needs_review" and not any(v["revoked"] or v["rights"] == "revoked" for v in versions):
            self._automatic_grant(conn, job, previous["document_id"], previous["version_id"], raw_digest)
        queue.complete(conn, job, document=previous["document_id"], version=previous["version_id"], digest=raw_digest)
        return True

    def _automatic_grant(self, conn, job, document_id, version_id, raw_digest):
        request = job["request"]
        if request.get("rights_basis") != rights.PMC_CLOUD_EVIDENCE:
            return
        _, evidence = rights.automatic_evidence(request["pmcid"], request["license"], request["evidence_url"],
                                               request["text_url"])
        self._record_rights(conn, document_id, version_id, "granted", rights.PMC_CLOUD_EVIDENCE,
                            "Verified PMC Cloud license for the exact original", rights.PMC_CLOUD_EVIDENCE,
                            evidence, raw_digest)

    def decide(self, document_id, version_id, *, decision, actor, reason, evidence="", license_id=""):
        actor, reason = prepare.public_text(actor, 128), prepare.public_text(reason, 500)
        if not actor or not reason or decision not in {"granted", "revoked"}:
            raise ValueError("rights_decision_invalid")
        basis, license_token, locator = rights.decision_evidence(decision, evidence, license_id)
        evidence_record = {"locator": locator, "license": license_token}
        with self.store.write(timeout=2) as conn:
            version = conn.execute("SELECT raw_sha256 FROM versions WHERE document_id=? AND version_id=?",
                                   (document_id, version_id)).fetchone()
            if version is None:
                raise KeyError("corpus_version_unknown")
            self._record_rights(conn, document_id, version_id, decision, actor, reason, basis,
                                evidence_record, version[0])

    @staticmethod
    def _record_rights(conn, document_id, version_id, decision, actor, reason, basis, evidence, raw_digest):
        conn.execute("UPDATE versions SET rights=? WHERE document_id=? AND version_id=?",
                     (decision, document_id, version_id))
        conn.execute("""INSERT INTO rights_events
                     (document_id,version_id,decision,actor,reason,at,basis,scope,evidence,raw_sha256)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""", (document_id, version_id, decision, actor, reason, time.time(),
                     basis, "shared_corpus_publication", json.dumps(evidence, ensure_ascii=False), raw_digest))

    def visible(self, document_id, version_id):
        with self.store.read() as conn:
            row = conn.execute("SELECT rights FROM versions WHERE document_id=? AND version_id=?",
                               (document_id, version_id)).fetchone()
        return bool(row and row["rights"] == "granted")

    def selected(self, conn, limit):
        if bibliography.available(conn):
            rows = conn.execute("""WITH scoped AS (
                                    SELECT v.*, coalesce('biblio:' || a.bibliography_id, v.identity) AS work
                                    FROM versions AS v LEFT JOIN corpus_bibliography_aliases AS a
                                    ON v.identity=a.identity
                                ) SELECT document_id,version_id,markdown_sha256,chunk_count FROM scoped AS v
                                WHERE rights='granted' AND NOT EXISTS (SELECT 1 FROM scoped AS newer
                                WHERE newer.work=v.work AND newer.seq>v.seq)
                                ORDER BY document_id LIMIT ?""", (limit + 1,)).fetchall()
        else:
            rows = conn.execute("""SELECT document_id,version_id,markdown_sha256,chunk_count FROM versions AS v
                                WHERE rights='granted' AND NOT EXISTS (SELECT 1 FROM versions AS newer
                                WHERE newer.identity=v.identity AND newer.seq>v.seq)
                                ORDER BY document_id LIMIT ?""", (limit + 1,)).fetchall()
        if len(rows) > limit:
            raise ValueError("corpus_document_budget")
        return [dict(row) for row in rows]

    def document(self, document_id, version_id):
        if not self.visible(document_id, version_id):
            raise LookupError("corpus_version_unavailable")
        metadata, text = prepare.read_document_files(self.store.documents, document_id, version_id)
        if not self.visible(document_id, version_id):
            raise LookupError("corpus_version_unavailable")
        return metadata, text

    def versions(self, limit=50):
        if not 1 <= limit <= 100:
            raise ValueError("limit_out_of_range")
        with self.store.read() as conn:
            rows = conn.execute("""SELECT document_id,version_id,raw_sha256,markdown_sha256,chunk_count,
                                previous_version,source_format,rights FROM versions ORDER BY seq DESC LIMIT ?""",
                                (limit,)).fetchall()
        return [dict(row) for row in rows]
