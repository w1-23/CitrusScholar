"""Release preparation tests, with opt-in isolated checks against the remote handoff."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stage_fusion_release import FUSION_IDS, _checked, _read_member, _requirements, _sha, verified_handoff


ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "04_scripts/stage_skill_release.py"
LOCK = ROOT / "02_config/skill-fusion.lock.json"
RQA = "research-question-answering"
NATURE = "nature-figure"
LEGACY = "reference-verification"


class SourceVerification(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original = b"original handoff member\n"
        (self.root / "run.py").write_bytes(self.original)
        manifest = f"{_sha(self.original)}  ./run.py\n".encode()
        (self.root / "handoff.sha256").write_bytes(manifest)
        self.lock = {"handoff_manifest": {"sha256": _sha(manifest), "members": 1},
                     "handoff_files": {"run.py": _sha(self.original)}, "nature_files": {}}

    def test_handoff_tamper_and_count(self):
        _, files = verified_handoff(self.root, self.lock)
        self.assertEqual(files, self.lock["handoff_files"])
        self.lock["handoff_manifest"]["members"] = 2
        with self.assertRaisesRegex(ValueError, "incomplete_fusion_handoff_manifest"):
            verified_handoff(self.root, self.lock)
        self.lock["handoff_manifest"]["members"] = 1
        (self.root / "run.py").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "fusion_source_hash_mismatch: run.py"):
            verified_handoff(self.root, self.lock)

    def test_symlink_even_matching_bytes_is_rejected(self):
        (self.root / "copy.py").write_bytes(self.original)
        (self.root / "run.py").unlink()
        (self.root / "run.py").symlink_to("copy.py")
        with self.assertRaisesRegex(ValueError, "invalid_fusion_source_member"):
            verified_handoff(self.root, self.lock)
        (self.root / "nested").symlink_to(".")
        with self.assertRaisesRegex(ValueError, "invalid_fusion_source_directory"):
            _read_member(self.root, "nested/copy.py")

    def test_unsafe_paths_and_hashes_fail_closed(self):
        for path in ("/absolute", "../run.py", "nested/../run.py", "nested//run.py", "nested\\run.py"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "invalid_fusion_path"):
                _read_member(self.root, path)
        with self.assertRaisesRegex(ValueError, "fusion_source_hash_mismatch"):
            _checked(b"x", "0" * 64, "test")

    def test_image_and_runtime_requirements_derive_from_same_exact_pins(self):
        worker = json.loads(LOCK.read_bytes())["worker"]
        expected = "".join(f'{name}=={pin["version"]} --hash=sha256:{pin["sha256"]}\n'
                           for name, pin in sorted(worker["wheels"].items()))
        self.assertEqual((LOCK.parent / "skill-fusion-worker.requirements.lock").read_text(), expected)
        effective = dict(line.split("==") for line in _requirements(worker).decode().splitlines())
        self.assertEqual(effective, worker["base"] | {k: p["version"] for k, p in worker["wheels"].items()})


@unittest.skipUnless(os.getenv("CITRUS_TEST_SKILL_HANDOFF") and os.getenv("CITRUS_TEST_SKILL_REPOSITORIES"),
                     "requires isolated access to approved handoff and pinned Git checkout")
class CandidatePreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handoff = Path(os.environ["CITRUS_TEST_SKILL_HANDOFF"])
        cls.repositories = Path(os.environ["CITRUS_TEST_SKILL_REPOSITORIES"])
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()
        cls.active = cls.stage("candidate", "--include-runtime")
        cls.legacy_rqa = cls.stage("legacy-rqa", "--enable", RQA, fusion=False)

    @classmethod
    def stage(cls, name, *arguments, fusion=True):
        output = cls.root / name
        command = [sys.executable, "-B", str(STAGE), "--repositories", str(cls.repositories), "--output", str(output)]
        if fusion:
            command.extend(["--fusion-handoff", str(cls.handoff)])
        subprocess.run(command + list(arguments), check=True, capture_output=True, text=True)
        return output

    def test_inventory_discovery_and_approval(self):
        sys.path[:0] = [str(ROOT / "01_src"), str(ROOT / "packages")]
        self.addCleanup(lambda: sys.path.__delitem__(slice(0, 2)))
        from src.agent.installed_skills import list_installed
        from src.code_execution.protocol import logical_path

        release = self.active
        payload = lambda path: json.loads((release / path).read_bytes())
        ids = {x["id"] for x in list_installed(release)}
        self.assertEqual(ids, FUSION_IDS | {RQA, NATURE})
        self.assertFalse((release / "skills" / LEGACY).exists())
        self.assertFalse((release / "skills/nature-shared/SKILL.md").exists())
        self.assertFalse((release / "python").exists())
        approvals = payload("approved-scripts.json")
        self.assertEqual(set(approvals), {f"skills/{name}/scripts/run.py" for name in FUSION_IDS})
        self.assertEqual(len(set(approvals.values())), 3)
        inventory = payload("inventory.json")
        self.assertEqual(inventory, {p.relative_to(release).as_posix(): _sha(p.read_bytes())
                                    for p in release.rglob("*") if p.is_file() and p.name != "inventory.json"})
        bundle = payload("fusion/bundle.json")
        self.assertEqual(bundle["skills"], dict.fromkeys(FUSION_IDS, "0.1.0"))
        self.assertLessEqual(len(bundle["files"]), 94)
        for name, digest in bundle["files"].items():
            self.assertEqual(logical_path("/input/fusion/" + name), "/input/fusion/" + name)
            self.assertEqual(inventory[f"fusion/{name}"], digest)
        for approval, digest in approvals.items():
            self.assertEqual(inventory[approval], digest)

    def test_rqa_is_original_and_nature_has_exact_bytes(self):
        def sums(root):
            return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in root.rglob("*") if p.is_file()}
        self.assertEqual(sums(self.active / "skills" / RQA), sums(self.legacy_rqa / "skills" / RQA))
        nature = self.active / "skills" / NATURE
        original = self.handoff / "methods" / NATURE
        compare = ("SKILL.md", "manifest.yaml", "scripts/plot_templates.py", "scripts/panel_alignment.R",
                   "assets/chart-atlas/atlas-01-bar-charts.png")
        for name in compare:
            self.assertEqual((nature / name).read_bytes(), (original / name).read_bytes())
        self.assertEqual((nature / "scripts/plot_templates.py").read_bytes(),
                         (self.active / "nature-qa/skills/nature-figure/scripts/plot_templates.py").read_bytes())
        assets = nature / "assets/figures4papers"
        self.assertEqual({x.name for x in assets.iterdir()}, {"THIRD_PARTY_NOTICES.md"})
        self.assertTrue((self.active / "skills/nature-shared/core/nature-results-discussion.md").is_file())

    def test_sandbox_overlay_is_exact_and_preserves_inherited_ceiling(self):
        from stage_fusion_release import _adapt_worker_sandbox

        bundle = self.active / "fusion/03_skill_fusion"
        pin = json.loads((bundle / "sandbox-compatibility.json").read_bytes())
        original = (self.handoff / "sandbox.py").read_bytes()
        derived = (bundle / "sandbox.py").read_bytes()
        self.assertEqual(_sha(original), pin["original_sha256"])
        self.assertEqual(_sha(derived), pin["staged_sha256"])
        self.assertEqual(derived, _adapt_worker_sandbox(original, pin))
        with self.assertRaisesRegex(ValueError, "fusion_source_hash_mismatch: original sandbox.py"):
            _adapt_worker_sandbox(original + b"# tampered", pin)

    def test_empty_enable_and_determinism(self):
        empty = self.stage("empty", "--include-runtime", "--enable")
        self.assertEqual(json.loads((empty / "release.json").read_bytes()), {})
        self.assertFalse((empty / "fusion").exists())
        self.assertTrue((empty / "nature-qa/provenance.json").is_file())
        rebuilt = self.stage("rebuild", "--include-runtime")
        self.assertEqual((self.active / "inventory.json").read_bytes(), (rebuilt / "inventory.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
