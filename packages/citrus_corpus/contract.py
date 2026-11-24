"""Identity and retrieval settings for independent generation families."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CorpusSpec:
    corpus_id: str
    family_id: str
    require_encoder_fingerprint: bool = False


LEGACY = CorpusSpec("shared_agent_literature", "agent_literature")
ISOLATED = CorpusSpec(LEGACY.corpus_id, LEGACY.family_id, require_encoder_fingerprint=True)


@dataclass(frozen=True)
class RetrievalParameters:
    top_vector: int = 40
    top_bm25: int = 40
    window: int = 20
    rrf_k: int = 60
    dense_weight: float = 1.0
    bm25_weight: float = 1.0
