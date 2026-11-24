"""Offline CPU query and build models for the isolated corpus."""
from pathlib import Path
from threading import Lock

from citrus_retrieval.models import OptimumReranker


class CpuE5:
    model_name = "intfloat/multilingual-e5-large"
    dim = 1024

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        if not self.cache_dir.is_dir():
            raise ValueError("corpus_embedding_cache_missing")
        self.model = None
        self._lock = Lock()

    def _embed(self, texts, prefix):
        with self._lock:
            if self.model is None:
                from fastembed import TextEmbedding

                self.model = TextEmbedding(self.model_name, cache_dir=str(self.cache_dir),
                                           providers=["CPUExecutionProvider"], threads=1,
                                           local_files_only=True)
            return [row.tolist() for row in self.model.embed(
                [f"{prefix} {text}" for text in texts], batch_size=8)]

    def embed_passages(self, texts):
        return self._embed(texts, "passage:")

    def embed_query(self, text):
        return self._embed([text], "query:")[0]


class FusedRank:
    """Return the existing dense/BM25 rank when no local reranker is selected."""
    def rerank(self, query, chunks, *, top_k):
        return chunks[:top_k]


class CpuRerank(OptimumReranker):
    def __init__(self, directory):
        super().__init__(directory, check=lambda: None, provider="CPUExecutionProvider", batch_size=4)

    def rerank(self, query, chunks, *, top_k):
        return self(query, chunks, top_k=top_k)
