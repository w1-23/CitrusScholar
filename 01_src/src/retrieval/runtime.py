"""Process-wide retriever instance used by the local hybrid-search tool."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import threading

logger = logging.getLogger(__name__)
_RAG_INSTANCE = None
_RAG_LOCK = threading.Lock()


class RetrievalUnavailable(RuntimeError):
    """Only local retrieval is unavailable; importing other tools stays safe."""

    def __init__(self, reason_code: str, *, empty_allowed: bool):
        self.reason_code, self.empty_allowed = reason_code, empty_allowed
        if reason_code == "base_index_reload_required":
            guidance = ": reload all local retrieval readers for the verified active generation"
        elif reason_code == "base_index_pointer_invalid":
            guidance = ": validate or restore the active index pointer and registry"
        else:
            guidance = (": local retrieval is unavailable in explicit empty-data mode" if empty_allowed else
                        ": configure corpus data or CITRUSQA_ALLOW_EMPTY_DATA=1")
        super().__init__(reason_code + guidance)


def _require_data() -> None:
    from src.config import settings
    from src.runtime_paths import data_capability, require_retrieval_data
    root = Path(settings.DATA_DIR)
    allow_empty = os.getenv("CITRUSQA_ALLOW_EMPTY_DATA", "").strip() == "1"
    capability = data_capability(root, allow_empty=allow_empty)
    # Explicit legacy Qdrant stores have no active_index.json. They still cross
    # the registry/schema allow-list checks in MultiBatchRetriever before use.
    if (capability.reason_code == "index_missing"
            and str(settings.RETRIEVAL_BACKEND).casefold() in {"qdrant", "auto"}
            and any(path.is_dir() for path in root.glob("*/qdrant_data"))):
        return
    try:
        require_retrieval_data(root, allow_empty=allow_empty)
    except RuntimeError as exc:
        raise RetrievalUnavailable(capability.reason_code, empty_allowed=allow_empty) from exc
    if not capability.available:
        raise RetrievalUnavailable(capability.reason_code, empty_allowed=allow_empty)


def require_current_rag(rag) -> None:
    """Reject a stale process; never combine its index with a newer registry."""
    from src.retrieval.corpus_registry import active_index_pointer, load_corpus_registry

    try:
        pointer = active_index_pointer(rag.data_dir)
        matches = (pointer == rag.index_pointer
                   and load_corpus_registry(rag.data_dir, pointer=pointer) == rag.registry
                   and active_index_pointer(rag.data_dir) == pointer)
    except (OSError, ValueError) as exc:
        raise RetrievalUnavailable("base_index_pointer_invalid", empty_allowed=False) from exc
    if not matches:
        raise RetrievalUnavailable("base_index_reload_required", empty_allowed=False)


def get_rag():
    """Load the shared corpus/index/models once per service process."""
    global _RAG_INSTANCE
    if _RAG_INSTANCE is None:
        with _RAG_LOCK:
            if _RAG_INSTANCE is None:
                _require_data()
                from src.retrieval.multi_retriever import MultiBatchRetriever
                from src.retrieval.corpus_registry import CorpusMetadataError
                try:
                    _RAG_INSTANCE = MultiBatchRetriever()
                except CorpusMetadataError as exc:
                    raise RetrievalUnavailable("base_index_pointer_invalid", empty_allowed=False) from exc
                logger.info("Local retriever singleton initialized")
    require_current_rag(_RAG_INSTANCE)
    return _RAG_INSTANCE
