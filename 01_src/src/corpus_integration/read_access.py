"""Host citation access with independent sixth-corpus retrieval policy."""
from functools import lru_cache
import os
import sqlite3

from .settings import isolated_enabled, isolated_search_configured, legacy_enabled, reader_root


def shared_search_available():
    from .settings import unified_enabled

    if unified_enabled():
        return False
    from .selfcorpus_search import enabled

    if enabled():
        # The shared-only tool addresses Selfcorpus when enabled. Its failure
        # never substitutes the historical shared reader.
        return True
    if isolated_enabled():
        return isolated_search_configured()
    from src.shared_corpus.runtime import current_runtime
    return current_runtime() is not None


@lru_cache(maxsize=2)
def _embedder(cache):
    from citrus_corpus.models import CpuE5
    return CpuE5(cache)


class IsolatedSearch:
    def __init__(self, root, cache, *, generation=None):
        self.root = root
        self.cache = cache
        self.unavailable = False
        self.fixed_generation = generation
        self.served_generation = None
        self.fallback_reason = None
        from citrus_corpus.generations import valid_generation
        from citrus_corpus.store import Store
        from citrus_corpus.pointers import read_pointer

        store = Store(root)
        if generation is not None and not valid_generation(generation):
            raise ValueError("corpus_generation_invalid")
        self.requested_generation = generation if generation is not None else read_pointer(store.root)
        self.generation = self.requested_generation

    def search(self, query, *, limit):
        from citrus_corpus.models import FusedRank
        from citrus_corpus.reader import Reader
        from citrus_corpus.store import Store
        from src.core.capability_policy import allows

        if self.requested_generation is None:
            raise LookupError("corpus_generation_unavailable")
        if reader_root() != self.root or not allows("local_rag"):
            raise LookupError("corpus_read_unavailable")
        with Reader(Store(self.root)).pin(embedder=_embedder(self.cache), reranker=FusedRank(),
                                          generation=self.requested_generation,
                                          allow_fallback=self.fixed_generation is None) as reader:
            rows = reader.search(query, limit)
            if reader_root() != self.root or not allows("local_rag"):
                raise LookupError("corpus_read_unavailable")
            self.generation = self.served_generation = reader.generation
            self.fallback_reason = reader.fallback_reason
        fields = ("source_family", "index_generation", "document_id", "version_id", "chunk_id",
                  "title", "section_name", "read_ref")
        return [{**{field: row[field] for field in fields}, "excerpt": row["text"]} for row in rows]


def isolated_search():
    from src.auth import user_by_id
    from src.core.tracing import get_user_id

    root, cache = reader_root(), os.getenv("CITRUS_ISOLATED_CORPUS_EMBEDDING_CACHE")
    owner = get_user_id()
    if not isolated_enabled() or not root or not cache or not owner:
        return None
    user = user_by_id(int(owner)) or {}
    if not user.get("active") or user.get("approval_status") != "approved":
        return None
    try:
        return IsolatedSearch(root, cache, generation=os.getenv("CITRUS_CORPUS_READ_GENERATION") or None)
    except (sqlite3.Error, OSError, ValueError):
        return None


def legacy_reader():
    from src.shared_corpus.runtime import current_runtime
    from .legacy_read import archived_reader

    runtime = current_runtime()
    return runtime if runtime is not None or legacy_enabled() else archived_reader()


def read_reference_page(ref, *, offset=0, length=6000):
    if not 0 <= offset < ref["end"] - ref["start"]:
        raise LookupError("shared_document_unavailable")
    start = ref["start"] + offset
    length = min(length, ref["end"] - start)
    if "chunk_id" in ref:
        from citrus_corpus.reader import Reader
        from citrus_corpus.store import Store
        from src.core.capability_policy import allows

        root = reader_root()
        if not root or not allows("local_rag"):
            raise LookupError("shared_document_unavailable")
        result = Reader(Store(root)).reference(ref)
        if reader_root() != root or not allows("local_rag"):
            raise LookupError("shared_document_unavailable")
        end = start + length
        return {**result, "start": start, "end": end, "total_chars": ref["end"],
                "next_start": end if end < ref["end"] else None, "kind": "stored_markdown",
                "markdown": result["markdown"][offset:offset + length]}
    runtime = legacy_reader()
    if runtime is None:
        raise LookupError("shared_document_unavailable")
    width = ref["end"] - ref["start"]
    if width > 12000:
        raise LookupError("shared_document_unavailable")
    page = runtime.preview(ref["document_id"], generation=ref["index_generation"], version=ref["version_id"],
                           start=ref["start"], length=width, digest=ref["md_sha256"])
    if page["end"] != ref["end"]:
        raise LookupError("shared_document_unavailable")
    end = start + length
    return {**page, "start": start, "end": end, "next_start": end if end < ref["end"] else None,
            "markdown": page["markdown"][offset:offset + length]}


def read_reference_document(ref):
    """Return stored Markdown only for an authorized, pinned citation reference."""
    from src.core.capability_policy import allows

    if not allows("local_rag") or ref["end"] - ref["start"] > 12000:
        raise LookupError("shared_document_unavailable")
    if "chunk_id" in ref:
        from citrus_corpus.reader import Reader
        from citrus_corpus.store import Store

        root = reader_root()
        if not root:
            raise LookupError("shared_document_unavailable")
        result = Reader(Store(root)).reference(ref, full=True)
        if reader_root() != root:
            raise LookupError("shared_document_unavailable")
    else:
        runtime = legacy_reader()
        if runtime is None:
            raise LookupError("shared_document_unavailable")
        result = runtime.preview(ref["document_id"], generation=ref["index_generation"],
                                 version=ref["version_id"], start=ref["start"],
                                 length=ref["end"] - ref["start"], digest=ref["md_sha256"], full=True)
        if (result["end"] != ref["end"] or result["md_sha256"] != ref["md_sha256"]
                or result["version_id"] != ref["version_id"]
                or result["index_generation"] != ref["index_generation"]):
            raise LookupError("shared_document_unavailable")
    if not allows("local_rag"):
        raise LookupError("shared_document_unavailable")
    return {"title": result["title"], "markdown": result["markdown"],
            "hit_start": ref["start"], "hit_end": ref["end"], "md_sha256": ref["md_sha256"],
            "index_generation": ref["index_generation"], "version_id": ref["version_id"]}


def reference_visible(ref):
    if "chunk_id" not in ref:
        runtime = legacy_reader()
        if runtime is None:
            return False
        if hasattr(runtime, "reference_visible"):
            return runtime.reference_visible(ref)
    try:
        return read_reference_page(ref, length=12000)["end"] == ref["end"]
    except (sqlite3.Error, LookupError, OSError, ValueError):
        return False
