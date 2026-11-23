"""Read metadata for the same immutable Skill directory mounted by DeepAgents."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import yaml


def release_path() -> Path | None:
    configured = os.getenv("CITRUS_SKILL_RELEASE", "")
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("skill_release_path_must_be_absolute")
    return path


def model_directory(release: Path) -> Path:
    directory = release / "skills"
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("skill_release_directory_unavailable")
    return directory


def list_installed(release: Path) -> list[dict]:
    root = model_directory(release)
    release_file = release / "release.json"
    with release_file.open("rb") as stream:
        selected = json.loads(stream.read(65537))
    if not isinstance(selected, dict) or len(selected) > 100:
        raise ValueError("invalid_skill_release")
    rows = []
    for entry in sorted(root.glob("*/SKILL.md")):
        name = entry.parent.name
        commit = selected.get(name)
        if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit)
                or entry.parent.is_symlink() or entry.is_symlink()):
            raise ValueError("invalid_installed_skill")
        with entry.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("skill_entry_size_limit")
        frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", raw.decode(), re.S)
        try:
            metadata = yaml.safe_load(frontmatter[1]) if frontmatter else None
        except yaml.YAMLError as exc:
            raise ValueError("invalid_skill_frontmatter") from exc
        if not isinstance(metadata, dict) or metadata.get("name") != name:
            raise ValueError("invalid_skill_frontmatter")
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError("invalid_skill_description")
        rows.append({
            "id": name,
            "version": commit,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "description": description,
            "required_resources": ["SKILL.md"],
        })
    if {row["id"] for row in rows} != set(selected):
        raise ValueError("incomplete_skill_release")
    return rows
