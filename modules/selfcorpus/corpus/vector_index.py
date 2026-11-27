from collections.abc import Iterable, Sequence
from heapq import nlargest
from math import hypot, isfinite, sumprod
from struct import pack, unpack
from typing import Protocol


def encode_vector(values: Sequence[float], dimension: int) -> bytes:
    if len(values) != dimension:
        raise ValueError('incorrect embedding dimension')
    norm = hypot(*values)
    if not isfinite(norm) or norm == 0:
        raise ValueError('non-finite or zero embedding')
    return pack(f'<{dimension}f', *(x / norm for x in values))


class VectorIndex(Protocol):
    def rank(self, rows: Iterable, query: bytes, dimension: int, limit: int) -> list: ...


class ExactVectorIndex:
    """Ranks normalized float32 candidates already filtered by ACL and metadata."""

    def rank(self, rows, query, dimension, limit):
        shape = f'<{dimension}f'
        needle = unpack(shape, query)
        candidates = ((sumprod(needle, unpack(shape, row['vector'])), row['chunk_ref'], row) for row in rows)
        return [row for score, _, row in nlargest(limit, candidates, key=lambda x: (x[0], x[1])) if score > 0]
