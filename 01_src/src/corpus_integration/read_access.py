"""Host citation access with independent sixth-corpus retrieval policy."""
from functools import lru_cache
import os
import sqlite3

from citrus_corpus.contract import ISOLATED

from .settings import legacy_enabled, reader_root


@lru_cache(maxsize=2)
def _embedder(cache):
    from citrus_corpus.models import CpuE5
    return CpuE5(cache)


class IsolatedSearch:
    def __init__(self, root, cache):
        self.root = root
        self.cache = cache
        self.unavailable = False
        from citrus_corpus.store import Store
        from citrus_corpus.pointers import read_pointer
        self.generation = read_pointer(Store(root).root)

    def search(self, query, *, limit):
        from citrus_corpus.models import FusedRank
        from citrus_corpus.reader import Reader
        from citrus_corpus.store import Store
        from src.core.capability_policy import allows

        if reader_root() != self.root or not allows("local_rag"):
            return []
        with Reader(Store(self.root)).pin(embedder=_embedder(self.cache), reranker=FusedRank()) as reader:
            self.generation = reader.generation
            rows = reader.search(query, limit)
        if reader_root() != self.root or not allows("local_rag"):
            return []
        fields = ("source_family", "index_generation", "document_id", "version_id", "chunk_id",
                  "title", "section_name", "read_ref")
        return [{**{field: row[field] for field in fields}, "excerpt": row["text"]} for row in rows]


def isolated_search():
    from src.auth import user_by_id
    from src.core.tracing import get_user_id

    root, cache = reader_root(), os.getenv("CITRUS_ISOLATED_CORPUS_EMBEDDING_CACHE")
    owner = get_user_id()
    if not root or not cache or not owner:
        return None
    user = user_by_id(int(owner)) or {}
    if not user.get("active") or user.get("approval_status") != "approved":
        return None
    try:
        return IsolatedSearch(root, cache)
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
    if ref.get("source_family") == ISOLATED.family_id:
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


def reference_visible(ref):
    if ref.get("source_family") != ISOLATED.family_id:
        runtime = legacy_reader()
        if runtime is None:
            return False
        if hasattr(runtime, "reference_visible"):
            return runtime.reference_visible(ref)
    try:
        return read_reference_page(ref, length=12000)["end"] == ref["end"]
    except (sqlite3.Error, LookupError, OSError, ValueError):
        return False
