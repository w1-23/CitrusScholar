"""Separate opt-in switches for legacy, growth and retrieval."""
import os


def selfcorpus_enabled():
    return os.getenv("CITRUS_SELFCORPUS_RETRIEVAL") == "1"


def unified_enabled():
    return os.getenv("CITRUS_CORPUS_UNIFIED_RETRIEVAL") == "1"


def legacy_enabled():
    return not unified_enabled() and not selfcorpus_enabled() and os.getenv("CITRUS_CORPUS_MODE", "legacy") == "legacy"


def isolated_enabled():
    return os.getenv("CITRUS_CORPUS_MODE") == "isolated"


def growth_enabled():
    return not unified_enabled() and not selfcorpus_enabled() and isolated_enabled() and os.getenv("CITRUS_CORPUS_FETCH_GROWTH") == "1"


def durable_intake_enabled():
    return growth_enabled() and os.getenv("CITRUS_CORPUS_DURABLE_INTAKE") == "1"


def reader_root():
    if os.getenv("CITRUS_CORPUS_READ") == "1":
        return os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    return None


def isolated_search_configured():
    return bool(isolated_enabled() and reader_root() and os.getenv("CITRUS_ISOLATED_CORPUS_EMBEDDING_CACHE"))
