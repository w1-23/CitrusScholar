import json
import tempfile
import unittest
from pathlib import Path

from src.agent.skill_provenance import package_digest, runtime_provenance


class ProvenanceTests(unittest.TestCase):
    def test_package_digest_is_stable_and_ignores_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "__init__.py").write_text("x = 1\n", encoding="utf-8")
            (root / "mod.py").write_text("y = 2\n", encoding="utf-8")
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "mod.cpython-312.pyc").write_bytes(b"junk")
            first = package_digest(root)
            self.assertEqual(first["files"], 2)
            self.assertEqual(package_digest(root), first)
            (root / "mod.py").write_text("y = 3\n", encoding="utf-8")
            self.assertNotEqual(package_digest(root)["sha256"], first["sha256"])

    def test_runtime_provenance_reads_locked_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "02_config"
            config.mkdir()
            (config / "standalone-skills.lock.json").write_text(json.dumps({
                "reference-verification": {"commit": "a" * 40},
                "data-audit": {"commit": "b" * 40}}), encoding="utf-8")
            rows = runtime_provenance(root)
            self.assertEqual(rows["citrus_refcheck"]["lock_commit"], "a" * 40)
            self.assertEqual(rows["citrus_audit"]["lock_commit"], "b" * 40)

    def test_missing_lock_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = runtime_provenance(Path(tmp))
            self.assertEqual(rows["citrus_refcheck"]["lock_commit"], "")
