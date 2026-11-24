"""Separate opt-in switches for legacy, growth and retrieval."""
import os


def legacy_enabled():
    return os.getenv("CITRUS_CORPUS_MODE", "legacy") == "legacy"


def growth_enabled():
    return (os.getenv("CITRUS_CORPUS_MODE") == "isolated"
            and os.getenv("CITRUS_CORPUS_FETCH_GROWTH") == "1")


def reader_root():
    if os.getenv("CITRUS_CORPUS_READ") == "1":
        return os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    return None
