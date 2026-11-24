"""Build, validate, publish and recover independent immutable generations."""
import json
import time

from .build import build_generation
from .catalog import Catalog
from .chunks import chunk_document
from .contract import ISOLATED
from .generations import load_generation, read_generation_manifest
from .pointers import read_pointer, write_pointer
from .prepare import read_document_files
from .search import HEALTH_QUERY

MAX_DOCUMENTS = 100
MAX_CHUNKS = 5000
MIN_BUILD_INTERVAL = 300


def _event(conn, generation, state, now, detail=None):
    conn.execute("INSERT INTO publication_events(generation,state,at,detail) VALUES(?,?,?,?)",
                 (generation, state, now, detail))


def _selection_payload(rows):
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


class Publisher:
    def __init__(self, store, *, embedder, reranker, idle=lambda: True, now=time.time):
        self.store = store
        self.catalog = Catalog(store)
        self.embedder = embedder
        self.reranker = reranker
        self.idle = idle
        self.now = now

    def publish(self):
        with self.store.writer():
            self._recover()
            if not self.idle():
                return {"state": "waiting_for_idle"}
            generation = self._ready()
            if generation:
                return self._activate(generation, verify=True)
            with self.store.read() as conn:
                selection = self.catalog.selected(conn, MAX_DOCUMENTS)
                last_build = conn.execute("SELECT last_build FROM corpus_state WHERE id=1").fetchone()[0]
                active = read_pointer(self.store.root)
                current = conn.execute("SELECT selection FROM publications WHERE generation=?", (active,)).fetchone()
            if not selection:
                return {"state": "no_granted_documents"}
            payload = _selection_payload(selection)
            if current and current["selection"] == payload:
                active_manifest = read_generation_manifest(self.store.root, active)
                if active_manifest["embedding_model"] == self.embedder.model_name:
                    return {"state": "active", "generation": active}
            now = self.now()
            if last_build and now - last_build < MIN_BUILD_INTERVAL:
                return {"state": "waiting_for_budget"}
            with self.store.write(timeout=2) as conn:
                conn.execute("UPDATE corpus_state SET last_build=? WHERE id=1", (now,))
                _event(conn, None, "building", now)
            try:
                generation = self._build(selection)
                with self.store.write(timeout=2) as conn:
                    conn.execute("""INSERT INTO publications (generation,selection,state,prior,at)
                                 VALUES (?,?,'ready',?,?) ON CONFLICT(generation) DO UPDATE SET
                                 state=CASE WHEN publications.published IS NULL THEN 'ready' ELSE 'published' END,
                                 prior=excluded.prior, at=excluded.at""", (generation, payload, active, now))
                    _event(conn, generation, "ready", self.now())
            except Exception as exc:
                with self.store.write(timeout=2) as conn:
                    _event(conn, None, "failed", self.now(), type(exc).__name__)
                raise
            if not self.idle():
                return {"state": "waiting_for_idle", "generation": generation}
            return self._activate(generation)

    def _build(self, selection):
        if sum(row["chunk_count"] for row in selection) > MAX_CHUNKS:
            raise ValueError("corpus_generation_chunk_budget")
        records = []
        for row in selection:
            metadata, markdown = read_document_files(self.store.documents, row["document_id"], row["version_id"])
            document_chunks = chunk_document(metadata, markdown, spec=ISOLATED)
            if metadata["content_sha256"] != row["markdown_sha256"] or len(document_chunks) != row["chunk_count"]:
                raise ValueError("corpus_document_integrity_invalid")
            records.extend(document_chunks)
        generation, _ = build_generation(self.store.root, records, embedder=self.embedder, spec=ISOLATED)
        self._health(generation)
        return generation

    def _health(self, generation):
        with load_generation(self.store.root, generation, embedder=self.embedder,
                             reranker=self.reranker, spec=ISOLATED) as candidate:
            if not candidate.search(HEALTH_QUERY, limit=1):
                raise ValueError("corpus_health_query_empty")

    def _ready(self):
        with self.store.read() as conn:
            row = conn.execute("SELECT generation FROM publications WHERE state='ready' ORDER BY seq LIMIT 1").fetchone()
        return row["generation"] if row else None

    def _activate(self, generation, *, verify=False):
        if verify:
            try:
                self._health(generation)
            except Exception as exc:
                with self.store.write(timeout=2) as conn:
                    conn.execute("UPDATE publications SET state='invalid' WHERE generation=? AND state='ready'",
                                 (generation,))
                    _event(conn, generation, "invalid", self.now(), type(exc).__name__)
                raise
        if not self.idle():
            return {"state": "waiting_for_idle", "generation": generation}
        with self.store.write(timeout=2) as conn:
            current = _selection_payload(self.catalog.selected(conn, MAX_DOCUMENTS))
            candidate = conn.execute("SELECT selection,state,published FROM publications WHERE generation=?",
                                     (generation,)).fetchone()
            if not candidate or candidate["selection"] != current:
                if candidate and candidate["published"] is None:
                    conn.execute("UPDATE publications SET state='obsolete' WHERE generation=?", (generation,))
                _event(conn, generation, "obsolete", self.now())
                return {"state": "selection_changed"}
            write_pointer(self.store.root, generation)
            self._mark_published(conn, generation)
        return {"state": "active", "generation": generation}

    def _recover(self):
        generation = read_pointer(self.store.root)
        if not generation:
            return
        with self.store.write(timeout=2) as conn:
            row = conn.execute("SELECT state FROM publications WHERE generation=?", (generation,)).fetchone()
            if not row or row["state"] not in {"ready", "published"}:
                raise ValueError("corpus_pointer_unjournaled")
            if row["state"] == "ready":
                self._mark_published(conn, generation)

    def _mark_published(self, conn, generation):
        now = self.now()
        conn.execute("UPDATE publications SET state='published',published=coalesce(published,?) WHERE generation=?",
                     (now, generation))
        _event(conn, generation, "published", now)

    def rollback(self, generation):
        """Pin a known published generation. Current rights still govern readers."""
        with self.store.writer():
            self._recover()
            with self.store.read() as conn:
                row = conn.execute("SELECT published FROM publications WHERE generation=?", (generation,)).fetchone()
                if not row or row["published"] is None:
                    raise LookupError("corpus_generation_unknown")
            self._health(generation)
            with self.store.write(timeout=2) as conn:
                _event(conn, generation, "rollback_requested", self.now())
            write_pointer(self.store.root, generation)
            with self.store.write(timeout=2) as conn:
                _event(conn, generation, "rolled_back", self.now())
        return {"state": "active", "generation": generation}
