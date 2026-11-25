"""Pin an immutable generation and resolve exact spans with current rights."""
from contextlib import contextmanager
from hashlib import sha256
import json

from .catalog import Catalog
from .chunks import chunk_document
from .contract import ISOLATED
from .generations import GenerationError, load_generation, valid_generation
from .search import GenerationContractError, GenerationIntegrityError, GenerationNotFound
from .pointers import read_pointer
from .store import publication_hold


def _fallback_reason(error):
    if isinstance(error, GenerationIntegrityError):
        return "active_generation_integrity_invalid"
    if isinstance(error, GenerationContractError):
        return "active_generation_contract_invalid"
    if isinstance(error, (GenerationNotFound, LookupError)):
        return "active_generation_unavailable"
    return "active_generation_open_failed"


class Reader:
    def __init__(self, store):
        self.store = store
        self.catalog = Catalog(store)

    def _membership(self, generation):
        with self.store.read() as conn:
            row = conn.execute("SELECT state,selection,published FROM publications WHERE generation=?",
                               (generation,)).fetchone()
        if not row or (row["published"] is None and
                       (row["state"] != "ready" or read_pointer(self.store.root) != generation)):
            raise LookupError("corpus_generation_unknown")
        return {(item["document_id"], item["version_id"]): item["markdown_sha256"]
                for item in json.loads(row["selection"])}

    @contextmanager
    def pin(self, *, embedder, reranker, generation=None, allow_fallback=False):
        """An explicit generation is strict unless the caller pinned the active pointer itself."""
        fixed = generation is not None and not allow_fallback
        if generation is not None and not valid_generation(generation):
            raise ValueError("corpus_generation_invalid")
        if generation is None:
            generation = read_pointer(self.store.root)
        if not generation:
            raise LookupError("corpus_generation_unavailable")
        fallback_reason = None
        try:
            handle, membership = self._open(generation, embedder, reranker)
        except (GenerationError, LookupError) as exc:
            if fixed:
                raise
            with self.store.read() as conn:
                # Do not silently serve a newer, explicitly rolled-back generation
                # if the held target becomes unreadable after an operator rollback.
                conn.execute("BEGIN")
                if publication_hold(conn) is not None:
                    raise
                prior = conn.execute("""SELECT generation FROM publications WHERE published IS NOT NULL
                                     AND generation<>? ORDER BY published DESC, seq DESC LIMIT 1""",
                                     (generation,)).fetchone()
            if prior is None:
                raise
            handle, membership = self._open(prior["generation"], embedder, reranker)
            fallback_reason = _fallback_reason(exc)
        with handle:
            yield PinnedReader(self.catalog, handle, membership, generation, fallback_reason)

    def _open(self, generation, embedder, reranker):
        membership = self._membership(generation)
        handle = load_generation(self.store.root, generation, embedder=embedder, reranker=reranker, spec=ISOLATED)
        manifest_members = {(row["document_id"], row["version_id"]) for row in handle.manifest["documents"]}
        if manifest_members != set(membership):
            handle.close()
            raise GenerationError("corpus_generation_membership_invalid")
        return handle, membership

    def reference(self, reference, *, full=False):
        if reference.get("source_family") != ISOLATED.family_id:
            raise ValueError("corpus_reference_invalid")
        generation = reference["index_generation"]
        pair = (reference["document_id"], reference["version_id"])
        digest = self._membership(generation).get(pair)
        if not digest or digest != reference.get("md_sha256"):
            raise LookupError("corpus_reference_unavailable")
        start, end = reference["start"], reference["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end or end - start > 12000:
            raise ValueError("corpus_reference_invalid")
        metadata, text = self.catalog.document(*pair)
        if end > len(text) or sha256(text.encode()).hexdigest() != digest:
            raise LookupError("corpus_reference_unavailable")
        if not any(chunk["chunk_id"] == reference.get("chunk_id")
                   and (chunk["md_start"], chunk["md_end"]) == (start, end)
                   for chunk in chunk_document(metadata, text, spec=ISOLATED)):
            raise LookupError("corpus_reference_unavailable")
        if not self.catalog.visible(*pair):
            raise LookupError("corpus_reference_unavailable")
        return {**reference, "title": metadata.get("title", ""),
                "markdown": text if full else text[start:end]}


class PinnedReader:
    def __init__(self, catalog, handle, membership, requested_generation, fallback_reason):
        self.catalog = catalog
        self.handle = handle
        self.membership = membership
        self.generation = handle.generation
        self.requested_generation = requested_generation
        self.fallback_reason = fallback_reason

    def search(self, query, limit=6):
        def eligible(row):
            return self.catalog.visible(row["document_id"], row["version_id"])

        rows = self.handle.search(query, limit=limit, eligible=eligible)
        result = []
        for row in rows:
            if not eligible(row):
                continue
            reference = {"source_family": ISOLATED.family_id, "index_generation": self.generation,
                         "document_id": row["document_id"], "version_id": row["version_id"],
                         "md_sha256": self.membership[(row["document_id"], row["version_id"])],
                         "start": row["md_start"], "end": row["md_end"], "chunk_id": row["chunk_id"]}
            result.append({**row, "source_family": ISOLATED.family_id, "index_generation": self.generation,
                           "read_ref": reference})
        return result
