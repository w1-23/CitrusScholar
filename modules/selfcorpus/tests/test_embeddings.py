import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from corpus.embeddings import HashEmbedding, OnnxEmbedding
from corpus.errors import CorpusError


CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"


class HashEmbeddingTests(unittest.TestCase):
    def test_deterministic_lexical_retrieval_including_chinese(self):
        fixture = HashEmbedding(4096)
        text = "柑橘黄龙病 boron zinc"
        query = fixture.embed([f"  QUERY: {text.replace('boron', 'BORON')} "], query=True)[0]
        self.assertEqual(query, HashEmbedding(4096).embed([text])[0])
        matching, unrelated, blank = fixture.embed(["柑橘黄龙病 boron treatment", "rice flowering", ""])
        score = lambda vector: sum(a * b for a, b in zip(query, vector))
        self.assertGreater(score(matching), score(unrelated))
        self.assertAlmostEqual(math.hypot(*matching), 1.0)
        self.assertAlmostEqual(math.hypot(*blank), 1.0)
        self.assertEqual(fixture.embed([]), [])
        self.assertNotEqual(fixture.signature, HashEmbedding().signature)
        self.assertEqual(len(query), fixture.dimension)

    def test_rejects_invalid_inputs(self):
        for dimension in (False, 0, -1, 10.0):
            with self.subTest(dimension=dimension), self.assertRaises(CorpusError):
                HashEmbedding(dimension)
        for texts in ("raw string", [None]):
            with self.subTest(texts=texts), self.assertRaises(CorpusError) as raised:
                HashEmbedding().embed(texts)
            self.assertEqual(raised.exception.code, "embedding_input_invalid")


class OnnxEmbeddingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.model = Path(temporary.name) / "snapshot-123"
        self.model.mkdir()
        model_config = {"hidden_size": 1024, "_name_or_path": "intfloat/multilingual-e5-large"}
        (self.model / "config.json").write_text(json.dumps(model_config), encoding="utf-8")
        (self.model / "tokenizer_config.json").write_text(json.dumps({"model_max_length": 512}), encoding="utf-8")
        (self.model / "model.onnx").write_bytes(b"fake graph")
        (self.model / "model.onnx_data").write_bytes(b"fake external weights")
        (self.model / "tokenizer.json").write_text("{}", encoding="utf-8")
        self.tokenizer = Mock()
        self.tokenizer.token_to_id.return_value = 1
        self.tokenizer_class = Mock()
        self.tokenizer_class.from_file.return_value = self.tokenizer
        self.session = Mock()
        self.session.get_inputs.return_value = [SimpleNamespace(name=name) for name in ("input_ids", "attention_mask")]
        self.session.get_outputs.return_value = [SimpleNamespace(name="last_hidden_state")]
        self.session.get_providers.return_value = [CUDA, CPU]
        self.ort = SimpleNamespace(
            get_available_providers=Mock(return_value=[CUDA, CPU]),
            InferenceSession=Mock(return_value=self.session),
        )
        self.runtime = self.enterContext(patch("corpus.embeddings._load_runtime"))
        self.runtime.return_value = (None, self.ort, self.tokenizer_class)

    def test_default_cuda_and_device_independent_signature(self):
        gpu = OnnxEmbedding(self.model)
        self.ort.InferenceSession.assert_called_once_with(str(self.model / "model.onnx"), providers=[CUDA])
        self.session.disable_fallback.assert_called_once_with()
        self.tokenizer.enable_truncation.assert_called_once_with(max_length=512, direction="right")
        self.tokenizer.enable_padding.assert_called_once_with(pad_id=1, pad_token="<pad>", direction="right")
        self.session.get_providers.return_value = [CPU]
        cpu = OnnxEmbedding(self.model, device="cpu", batch_size=2)
        self.ort.InferenceSession.assert_called_with(str(self.model / "model.onnx"), providers=[CPU])
        self.assertEqual(gpu.signature, cpu.signature)
        self.assertNotEqual(cpu.signature, OnnxEmbedding(self.model, device="cpu", revision="snapshot-456").signature)
        self.assertEqual(gpu.dimension, 1024)

    def test_unavailable_or_fallen_back_cuda_is_rejected(self):
        self.ort.get_available_providers.return_value = [CPU]
        with self.assertRaises(CorpusError) as unavailable:
            OnnxEmbedding(self.model)
        self.assertEqual(unavailable.exception.code, "embedding_provider_unavailable")
        self.ort.InferenceSession.assert_not_called()
        self.ort.get_available_providers.return_value = [CUDA, CPU]
        self.session.get_providers.return_value = [CPU]
        with self.assertRaises(CorpusError) as fallen_back:
            OnnxEmbedding(self.model)
        self.assertEqual(fallen_back.exception.code, "embedding_provider_unavailable")

    def test_assets_checked_before_loading_runtime(self):
        (self.model / "model.onnx_data").unlink()
        with self.assertRaises(CorpusError) as missing:
            OnnxEmbedding(self.model)
        self.assertEqual(missing.exception.code, "embedding_model_invalid")
        self.runtime.assert_not_called()

    def test_rejects_unsupported_graph_contract(self):
        self.session.get_outputs.return_value = [SimpleNamespace(name="already_pooled")]
        with self.assertRaises(CorpusError) as invalid:
            OnnxEmbedding(self.model)
        self.assertEqual(invalid.exception.code, "embedding_model_invalid")

    def test_batches_and_canonicalizes_existing_prefixes(self):
        embedding = OnnxEmbedding(self.model, batch_size=2)
        with patch.object(embedding, "_embed_batch", side_effect=[[[1.0], [2.0]], [[3.0]], [[4.0]]]) as batch:
            self.assertEqual(embedding.embed([" A ", " PASSAGE: B", "query: C"], query=True), [[1.0], [2.0], [3.0]])
            self.assertEqual(embedding.embed(["query: D"]), [[4.0]])
            self.assertEqual(embedding.embed([]), [])
        self.assertEqual([item.args for item in batch.call_args_list], [(["query: A", "query: B"],), (["query: C"],), (["passage: D"],)])

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "optional numerical runtime not installed")
    def test_masked_mean_l2_and_invalid_vectors(self):
        import numpy as np

        self.runtime.return_value = (np, self.ort, self.tokenizer_class)
        self.tokenizer.encode_batch.return_value = [
            SimpleNamespace(ids=[0, 2, 1], attention_mask=[1, 1, 0]),
            SimpleNamespace(ids=[0, 2, 7], attention_mask=[1, 1, 1]),
        ]
        hidden = np.zeros((2, 3, 1024), dtype=np.float32)
        hidden[0, :, :2] = [(6, 0), (0, 8), (float("nan"), 9999)]
        hidden[1, :, :2] = [(1, 0), (1, 0), (1, 0)]
        self.session.run.return_value = [hidden]
        model = OnnxEmbedding(self.model)
        first, second = model.embed(["alpha", "beta"])
        self.assertAlmostEqual(first[0], 0.6)
        self.assertAlmostEqual(first[1], 0.8)
        self.assertEqual(second[:2], [1.0, 0.0])
        self.assertAlmostEqual(math.hypot(*first), 1.0)
        outputs, feed = self.session.run.call_args.args
        self.assertEqual(outputs, ["last_hidden_state"])
        self.assertEqual(feed["input_ids"].dtype, np.int64)
        self.assertEqual(feed["attention_mask"].tolist(), [[1, 1, 0], [1, 1, 1]])
        for invalid in (hidden[:, :, :1], np.zeros_like(hidden), np.full_like(hidden, float("inf"))):
            self.session.run.return_value = [invalid]
            with self.assertRaises(CorpusError) as failure:
                model.embed(["alpha", "beta"])
            self.assertEqual(failure.exception.code, "embedding_inference_failed")
