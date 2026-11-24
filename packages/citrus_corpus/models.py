"""Offline CPU query and build models for the isolated corpus."""
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
from threading import Lock

from citrus_retrieval.models import OptimumReranker


class CpuE5:
    model_name = "intfloat/multilingual-e5-large"
    dim = 1024

    def __init__(self, cache_dir):
        from fastembed import __version__ as fastembed_version

        self.cache_dir = Path(cache_dir)
        repository = self.cache_dir / "models--qdrant--multilingual-e5-large-onnx"
        with (repository / "refs" / "main").open(encoding="ascii") as stream:
            revision = stream.read(64).strip()
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("corpus_embedding_revision_invalid")
        self.snapshot = repository / "snapshots" / revision
        profile = {"revision": revision, "fastembed": fastembed_version, "provider": "CPUExecutionProvider",
                   "model_data_bytes": (self.snapshot / "model.onnx_data").stat().st_size}
        for name in ("config.json", "model.onnx", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
            item = self.snapshot / name
            if item.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("corpus_embedding_artifact_too_large")
            with item.open("rb") as stream:
                profile[name] = file_digest(stream, "sha256").hexdigest()
        self.fingerprint = sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        self.model = None
        self._lock = Lock()

    def _embed(self, texts, prefix):
        with self._lock:
            if self.model is None:
                from fastembed import TextEmbedding

                self.model = TextEmbedding(self.model_name, cache_dir=str(self.cache_dir),
                                           providers=["CPUExecutionProvider"], threads=1,
                                           local_files_only=True, specific_model_path=str(self.snapshot))
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
