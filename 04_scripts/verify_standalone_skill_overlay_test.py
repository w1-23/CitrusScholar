"""The host overlay allowlist cannot hide unapproved or missing package bytes."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import verify_standalone_skill_overlay as gate


class OverlayPinsTest(unittest.TestCase):
    def test_only_the_exact_upstream_and_host_members_can_differ(self):
        first = Path("citrus_audit/workbook.py")
        second = Path("citrus_refcheck/verification.py")
        members = {first: (b"a", b"b"), second: (b"c", b"d")}
        pins = {key: tuple(sha256(value).hexdigest() for value in pair)
                for key, pair in members.items()}
        with TemporaryDirectory() as directory, patch.object(gate, "OVERLAYS", pins):
            staged, host = (Path(directory) / name for name in ("staged", "host"))
            for key, expected in members.items():
                for root, data in zip((staged, host), expected):
                    (root / key).parent.mkdir(parents=True, exist_ok=True)
                    (root / key).write_bytes(data)
            self.assertFalse(gate.compare(staged, host))
            for root, data in zip((staged, host), members[first]):
                (root / first).write_bytes(b"wrong")
                self.assertIn(str(first), gate.compare(staged, host)[0])
                (root / first).write_bytes(data)
            (host / second).unlink()
            self.assertIn(str(second), gate.compare(staged, host)[0])
            (host / second).write_bytes(members[second][1])
            (host / second).with_name("unapproved.py").write_text("pass\n")
            self.assertIn("extra host file", gate.compare(staged, host)[0])


if __name__ == "__main__":
    unittest.main()
