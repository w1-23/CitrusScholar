"""Offline E5 inference and an explicit lexical fixture for development."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Protocol

from .errors import CorpusError


class Embedder(Protocol):
    @property
    def signature(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


_PREFIX = re.compile(r"^(?:query|passage):\s*", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+")
_HAN = re.compile(r"[\u3400-\u9fff]+")
_PROVIDERS = {"cuda": "CUDAExecutionProvider", "cpu": "CPUExecutionProvider"}
_MODEL = "intfloat/multilingual-e5-large"
_PIPELINE = "e5-onnx-mean-l2-v1"


def _body(text: str) -> str:
    return _PREFIX.sub("", text.strip(), count=1)


def _check_texts(texts: list[str]) -> None:
    if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        raise CorpusError("embedding_input_invalid", "Embedding inputs must be a list of strings.")


class HashEmbedding:
    """Deterministic lexical features for tests and explicit development use."""

    def __init__(self, dimension: int = 256):
        if type(dimension) is not int or dimension < 1:
            raise CorpusError("embedding_config_invalid", "Hash embedding dimension must be positive.")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def signature(self) -> str:
        return f"hash-lexical-v1:{self.dimension}"

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        _check_texts(texts)
        return [self._vector(_body(text).casefold()) for text in texts]

    def _vector(self, body: str) -> list[float]:
        features = Counter(f"word:{word}" for word in _WORD.findall(body))
        for run in _HAN.findall(body):
            features.update(f"han:{char}" for char in run)
            features.update(f"han2:{a}{b}" for a, b in zip(run, run[1:]))
        if not features:
            features[f"literal:{body}"] = 1
        vector = [0.0] * self.dimension
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "little") % self.dimension] += 1.0 + math.log(count)
        norm = math.hypot(*vector)
        return [value / norm for value in vector]


def _load_runtime():
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except (ImportError, OSError) as exc:
        raise CorpusError(
            "embedding_dependency_unavailable",
            "E5 needs NumPy, tokenizers, and ONNX Runtime for the configured device.", 503,
        ) from exc
    return np, ort, Tokenizer


def _read_snapshot(model_dir: str | Path) -> tuple[Path, int, int]:
    try:
        snapshot = Path(model_dir).expanduser().resolve(strict=True)
        for name in ("model.onnx", "model.onnx_data", "tokenizer.json"):
            asset = snapshot / name
            if not asset.is_file() or not asset.stat().st_size:
                raise ValueError("missing model asset")
        model_config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
        tokenizer_config = json.loads((snapshot / "tokenizer_config.json").read_text(encoding="utf-8"))
        dimension = model_config["hidden_size"]
        max_length = tokenizer_config["model_max_length"]
        if (
            model_config["_name_or_path"] != _MODEL
            or type(dimension) is not int or dimension != 1024
            or type(max_length) is not int or not 2 <= max_length <= 512
        ):
            raise ValueError("not a supported E5 snapshot")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise CorpusError(
            "embedding_model_invalid", "A readable local multilingual E5 ONNX snapshot is required.", 503,
        ) from exc
    return snapshot, dimension, max_length


def _open_session(ort, model_file: Path, provider: str):
    try:
        if provider == _PROVIDERS["cuda"] and hasattr(ort, "preload_dlls"):
            from importlib.util import find_spec

            if find_spec("nvidia") is not None:
                ort.preload_dlls(directory="")
        if provider not in ort.get_available_providers():
            raise RuntimeError("requested provider is unavailable")
        session = ort.InferenceSession(str(model_file), providers=[provider])
        if session.get_providers()[:1] != [provider]:
            raise RuntimeError("runtime fell back during initialization")
        # ORT may place shape-only operators on CPU. Disable retry of a failed
        # inference on an all-CPU session, and reject startup on an all-CPU session.
        session.disable_fallback()
    except Exception as exc:
        device = "CUDA" if provider == _PROVIDERS["cuda"] else "CPU"
        raise CorpusError(
            "embedding_provider_unavailable", f"ONNX Runtime could not start the configured {device} provider.", 503,
        ) from exc
    return session


def _pool(np, hidden, attention, dimension: int) -> list[list[float]]:
    if hidden.shape != (*attention.shape, dimension) or hidden.dtype.kind != "f":
        raise ValueError("unexpected ONNX output shape or type")
    counts = attention.sum(axis=1, keepdims=True)
    if (counts <= 0).any():
        raise ValueError("input has no active tokens")
    masked = np.where(attention[..., None] != 0, hidden, 0)
    pooled = masked.sum(axis=1, dtype=np.float64) / counts
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or (norms <= 0).any():
        raise ValueError("embedding has zero or nonfinite norm")
    return (pooled / norms).tolist()


class OnnxEmbedding:
    """Run multilingual E5 from a local snapshot. CUDA is the default."""

    def __init__(
        self, model_dir: str | Path, *, device: str = "cuda", batch_size: int = 16,
        revision: str | None = None,
    ):
        if not isinstance(device, str) or device not in _PROVIDERS or type(batch_size) is not int or batch_size < 1:
            raise CorpusError("embedding_config_invalid", "Use device cuda or cpu and a positive batch size.")
        snapshot, self._dimension, max_length = _read_snapshot(model_dir)
        model_revision = snapshot.name if revision is None else revision
        if not isinstance(model_revision, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", model_revision):
            raise CorpusError("embedding_config_invalid", "Specify a stable model snapshot revision.")
        self._signature = f"{_MODEL}@{model_revision}:{_PIPELINE}:{max_length}:{self.dimension}"
        self._batch_size = batch_size
        self._np, ort, tokenizer_class = _load_runtime()
        try:
            self._tokenizer = tokenizer_class.from_file(str(snapshot / "tokenizer.json"))
            pad_id = self._tokenizer.token_to_id("<pad>")
            if pad_id != 1:
                raise ValueError("unexpected E5 padding token")
            self._tokenizer.enable_truncation(max_length=max_length, direction="right")
            self._tokenizer.enable_padding(pad_id=pad_id, pad_token="<pad>", direction="right")
        except Exception as exc:
            raise CorpusError("embedding_model_invalid", "The local E5 tokenizer is invalid.", 503) from exc
        self._session = _open_session(ort, snapshot / "model.onnx", _PROVIDERS[device])
        self._inputs = {item.name for item in self._session.get_inputs()}
        output_names = {item.name for item in self._session.get_outputs()}
        if (not {"input_ids", "attention_mask"} <= self._inputs
                or self._inputs - {"input_ids", "attention_mask", "token_type_ids"}
                or "last_hidden_state" not in output_names):
            raise CorpusError("embedding_model_invalid", "The local E5 ONNX input/output contract is invalid.", 503)

    @property
    def signature(self) -> str:
        return self._signature

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        _check_texts(texts)
        prefix = "query" if query else "passage"
        vectors: list[list[float]] = []
        try:
            for offset in range(0, len(texts), self._batch_size):
                batch = [f"{prefix}: {_body(text)}" for text in texts[offset:offset + self._batch_size]]
                vectors.extend(self._embed_batch(batch))
        except Exception as exc:
            raise CorpusError("embedding_inference_failed", "E5 embedding inference failed.", 503) from exc
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        np = self._np
        encoded = self._tokenizer.encode_batch(batch)
        if len(encoded) != len(batch):
            raise ValueError("tokenizer returned the wrong batch size")
        attention = np.asarray([row.attention_mask for row in encoded], dtype=np.int64)
        feed = {
            "input_ids": np.asarray([row.ids for row in encoded], dtype=np.int64),
            "attention_mask": attention,
        }
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.asarray([row.type_ids for row in encoded], dtype=np.int64)
        hidden, = self._session.run(["last_hidden_state"], feed)
        return _pool(np, hidden, attention, self.dimension)
