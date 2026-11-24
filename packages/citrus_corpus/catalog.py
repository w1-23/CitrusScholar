"""Version continuity, rights audit and live visibility for isolated works."""
from hashlib import sha256
import time

from . import chunks, prepare
from .contract import ISOLATED


class Catalog:
    def __init__(self, store):
        self.store = store

    def record(self, queue, job, raw_digest, source_format, markdown):
        identity = job["identity"]
        document_id = "doc_" + sha256(identity.encode("utf-8")).hexdigest()[:32]
        version_id = "v_" + raw_digest[:40]
        digest = sha256(markdown.encode("utf-8")).hexdigest()
        outline = chunks.section_outline(markdown)
        metadata = {
            "schema_version": chunks.SCHEMA_VERSION, "document_id": document_id, "version_id": version_id,
            "title": prepare.public_text(job["request"].get("title"), 512),
            "doi": identity.removeprefix("doi:") if identity.startswith("doi:") else "",
            "source_kind": job["request"]["kind"], "source_format": source_format,
            "source_sha256": raw_digest, "content_sha256": digest, "char_count": len(markdown),
            "prepared_at": prepare.utc_now(), "section_path": [row["section_name"] for row in outline
                                                              if row["section_level"]],
        }
        metadata["chunk_count"] = len(chunks.chunk_document(metadata, markdown, spec=ISOLATED))
        prepare.write_document(self.store.documents, document_id, version_id, metadata, markdown)
        with self.store.write(timeout=2) as conn:
            previous = conn.execute("SELECT version_id FROM versions WHERE identity=? ORDER BY seq DESC LIMIT 1",
                                    (identity,)).fetchone()
            conn.execute("""INSERT INTO versions (identity,document_id,version_id,raw_sha256,markdown_sha256,
                         chunk_count,previous_version,source_format,rights,created)
                         VALUES (?,?,?,?,?,?,?,?,'needs_review',?)""",
                         (identity, document_id, version_id, raw_digest, digest, metadata["chunk_count"],
                          previous[0] if previous else None, source_format, time.time()))
            queue.complete(conn, job, document=document_id, version=version_id, digest=raw_digest)

    def complete_duplicate(self, queue, job, raw_digest):
        with self.store.read() as conn:
            previous = conn.execute("SELECT document_id, version_id FROM versions WHERE identity=? AND raw_sha256=?",
                                    (job["identity"], raw_digest)).fetchone()
        if previous is None:
            return False
        with self.store.write(timeout=2) as conn:
            queue.complete(conn, job, document=previous["document_id"], version=previous["version_id"], digest=raw_digest)
        return True

    def decide(self, document_id, version_id, *, decision, actor, reason):
        actor, reason = prepare.public_text(actor, 128), prepare.public_text(reason, 500)
        if not actor or not reason or decision not in {"granted", "revoked"}:
            raise ValueError("rights_decision_invalid")
        with self.store.write(timeout=2) as conn:
            cursor = conn.execute("UPDATE versions SET rights=? WHERE document_id=? AND version_id=?",
                                  (decision, document_id, version_id))
            if cursor.rowcount != 1:
                raise KeyError("corpus_version_unknown")
            conn.execute("""INSERT INTO rights_events (document_id,version_id,decision,actor,reason,at)
                         VALUES(?,?,?,?,?,?)""", (document_id, version_id, decision, actor, reason, time.time()))

    def visible(self, document_id, version_id):
        with self.store.read() as conn:
            row = conn.execute("SELECT rights FROM versions WHERE document_id=? AND version_id=?",
                               (document_id, version_id)).fetchone()
        return bool(row and row["rights"] == "granted")

    def selected(self, conn, limit):
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
