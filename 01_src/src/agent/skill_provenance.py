"""Record which installed Skill package bytes a task actually imported."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

RUNTIME_PACKAGES = {"reference-verification": "citrus_refcheck", "data-audit": "citrus_audit"}


def package_digest(location: Path) -> dict:
    digest = hashlib.sha256()
    files = 0
    for path in sorted(location.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(location).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        files += 1
    return {"sha256": digest.hexdigest(), "files": files}


def runtime_provenance(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
    try:
        lock = json.loads((root / "02_config" / "standalone-skills.lock.json").read_text("utf-8"))
    except (OSError, ValueError):
        lock = {}
    if not isinstance(lock, dict):
        lock = {}
    rows = {}
    for name, module in RUNTIME_PACKAGES.items():
        entry = lock.get(name)
        row = {"lock_commit": entry.get("commit", "") if isinstance(entry, dict) else ""}
        try:
            spec = importlib.util.find_spec(module)
            location = Path(spec.origin).parent if spec and spec.origin else None
            if location is not None and location.is_dir():
                row.update(package_digest(location))
                row["imported_from_source"] = "packages" in location.parts
            else:
                row.update({"sha256": None, "files": 0})
        except (ImportError, OSError, ValueError):
            row.update({"sha256": None, "files": 0})
        rows[module] = row
    return rows
