"""Offline legacy E5 query preprocessing and unnormalized mean pooling.

The E5 prefix and cleanup adapt Citrus QA Agent engine/embedder.py (MIT).
Copyright (c) 2026 Citrus QA Agent contributors.
"""
from collections import OrderedDict
from hashlib import file_digest
from pathlib import Path
import re
from threading import Lock

from ..embeddings import OnnxEmbedding
from .inventory import LegacyUnavailable


_REVISION = '66076b8dc6e367337e3e90e6fb309fb0f3addaf6'
RAW_SIGNATURE = f'intfloat/multilingual-e5-large@{_REVISION}:legacy-fastembed-raw-mean-v1:512:1024'
_ASSETS = {
    'model.onnx': '1c09780c907c8a91a77a6ab1fd231f79e090d2907ca431223703dfebeed3d36c',
    'model.onnx_data': '0cf1883fee81c63819a44e2ba0efa51d4043d9759685a4ebebbde97e0623d15c',
    'tokenizer.json': 'f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af',
    'config.json': '1de8c3be1f344c0eefa4962480a006f8639f416dfafaa95a770e3cf4bceae6a4',
    'tokenizer_config.json': 'f90024142df07163e5e6c5b9a6ad7c8c68b22a9112af11e3db4559a9ff90f737',
}
_CONTROL = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')


def _prepare(text, prefix):
    text = text.encode('utf-8', errors='replace').decode('utf-8').strip()
    while text.casefold().startswith(prefix):
        text = text[len(prefix):].lstrip()
    return _CONTROL.sub('', f'{prefix} {text}'.rstrip()).replace('\ufffd', '').strip()


class LegacyE5(OnnxEmbedding):
    """Pinned independent E5. The old L2 flat tables require raw vector scale."""

    def __init__(self, model_dir, *, device='cuda', batch_size=16):
        model_dir = Path(model_dir).expanduser().resolve(strict=True)
        for name, expected in _ASSETS.items():
            path = model_dir / name
            if path.is_symlink():
                raise LegacyUnavailable('legacy_embedding_asset_symlink')
            with path.open('rb') as stream:
                if file_digest(stream, 'sha256').hexdigest() != expected:
                    raise LegacyUnavailable('legacy_embedding_artifact_mismatch')
        super().__init__(model_dir, device=device, batch_size=batch_size, revision=_REVISION)
        self._legacy_device = device
        self._legacy_cache = OrderedDict()
        self._legacy_lock = Lock()

    @property
    def device(self):
        return self._legacy_device

    @property
    def signature(self):
        return RAW_SIGNATURE

    def query_vector(self, query):
        key = _prepare(query, 'query:')
        with self._legacy_lock:
            if key in self._legacy_cache:
                self._legacy_cache.move_to_end(key)
                return self._legacy_cache[key]
        vector = tuple(self._embed_batch([key])[0])
        with self._legacy_lock:
            self._legacy_cache[key] = vector
            self._legacy_cache.move_to_end(key)
            if len(self._legacy_cache) > 128:
                self._legacy_cache.popitem(last=False)
        return vector

    def embed(self, texts, *, query=False):
        if query:
            return [list(self.query_vector(text)) for text in texts]
        prefixes = [_prepare(t, 'passage:') for t in texts]
        vectors = []
        for offset in range(0, len(prefixes), self._batch_size):
            vectors.extend(self._embed_batch(prefixes[offset:offset + self._batch_size]))
        return vectors

    def _embed_batch(self, batch):
        np = self._np
        encoded = self._tokenizer.encode_batch(batch)
        attention = np.asarray([row.attention_mask for row in encoded], dtype=np.int64)
        feed = {'input_ids': np.asarray([row.ids for row in encoded], dtype=np.int64),
                'attention_mask': attention}
        if 'token_type_ids' in self._inputs:
            feed['token_type_ids'] = np.asarray([row.type_ids for row in encoded], dtype=np.int64)
        hidden, = self._session.run(['last_hidden_state'], feed)
        # Legacy FastEmbed PooledEmbedding uses a float64 accumulation after
        # multiplication by the int64 attention mask. Do not L2-normalize.
        counts = attention.sum(axis=1, keepdims=True)
        if hidden.shape != (*attention.shape, self.dimension) or (counts <= 0).any():
            raise LegacyUnavailable('legacy_embedding_invalid')
        pooled = (hidden * attention[..., None]).sum(axis=1) / counts
        if not np.isfinite(pooled).all():
            raise LegacyUnavailable('legacy_embedding_invalid')
        return pooled.tolist()
