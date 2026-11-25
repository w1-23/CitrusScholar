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


def task_release(state: dict, *, resume: bool) -> Path | None:
    """Pin one server-side release for method discovery and Code access."""
    saved = state.get("native_skill_release") if resume else None
    configured = Path(saved) if saved else None
    if not resume:
        configured = release_path()
    if configured is None:
        if resume and (state.get("skill_release_inventory_sha256")
                       or (state.get("capability_runtime") or {}).get("skill_release_inventory_sha256")):
            raise ValueError("skill_release_missing_during_recovery")
        state["native_skill_release"] = ""
        state["skill_release_inventory_sha256"] = ""
        return None
    if not configured.is_absolute():
        raise ValueError("skill_release_path_must_be_absolute")
    root = configured.resolve(strict=True)
    model_directory(root)
    from .research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

    digest = hashlib.sha256(_bytes(_path(root, "inventory.json"), _MAX_RESOURCE_BYTES)).hexdigest()
    expected = state.get("skill_release_inventory_sha256") if resume else None
    if expected and expected != digest:
        raise ValueError("skill_release_changed_during_recovery")
    state["native_skill_release"] = str(root)
    state["skill_release_inventory_sha256"] = digest
    return root


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


def active_skills():
    from src.core.capability_policy import allows

    try:
        release = release_path()
        rows = list_installed(release) if release is not None else []
    except (OSError, ValueError, KeyError):
        return []
    return [row for row in rows if allows("skill:" + row["id"])]


def read_installed_resource(release, selected, resource, *, start_char, max_chars):
    from .research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

    info = next((row for row in list_installed(release) if row["id"] == selected["id"]), None)
    if not info or any(info[key] != selected[key] for key in ("id", "version", "sha256")):
        raise ValueError("selected_skill_identity_mismatch")
    name = resource or "SKILL.md"
    inventory = json.loads(_bytes(release / "inventory.json", _MAX_RESOURCE_BYTES))
    expected = inventory.get(f"skills/{info['id']}/{name}") if isinstance(inventory, dict) else None
    if not isinstance(expected, str):
        raise ValueError("skill_resource_unavailable")
    root = model_directory(release) / info["id"]
    raw = _bytes(_path(root, name), _MAX_RESOURCE_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if expected != digest:
        raise ValueError("skill_changed_during_read")
    if name == "SKILL.md" and digest != info["sha256"]:
        raise ValueError("skill_changed_during_read")
    info["method_resources"] = sorted(
        key.removeprefix(f"skills/{info['id']}/") for key in inventory
        if key.startswith(f"skills/{info['id']}/references/"))
    text = raw.decode("utf-8")
    end = min(start_char + max_chars, len(text))
    return info, {"resource": name, "text": text[start_char:end], "sha256": digest,
                  "version": info["version"], "start_char": start_char, "total_chars": len(text),
                  "next_start_char": end if end < len(text) else None}


def _script_approvals(release: Path):
    from .research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

    if not (release / "approved-scripts.json").exists():
        return {}, {}
    inventory = json.loads(_bytes(_path(release, "inventory.json"), _MAX_RESOURCE_BYTES))
    raw = _bytes(_path(release, "approved-scripts.json"), _MAX_RESOURCE_BYTES)
    if not isinstance(inventory, dict) or inventory.get("approved-scripts.json") != hashlib.sha256(raw).hexdigest():
        raise ValueError("skill_script_approval_integrity")
    approved = json.loads(raw)
    if not isinstance(approved, dict):
        raise ValueError("invalid_skill_script_approval")
    return approved, inventory


def approved_script_entries(release: Path | None) -> list[dict]:
    if release is None:
        return []
    approved, _inventory = _script_approvals(release)
    from src.core.capability_policy import allows
    enabled = {row["id"]: row["version"] for row in list_installed(release) if allows("skill:" + row["id"])}
    entries = []
    for key in sorted(approved):
        prefix, skill, resource = key.split("/", 2)
        if prefix == "skills" and skill in enabled:
            entries.append({"skill_id": skill, "version": enabled[skill], "resource": resource})
    return entries


def read_approved_script(release: Path | None, skill_id: str, version: str, resource: str) -> tuple[bytes, str]:
    from src.core.capability_policy import allows
    from .research_skill_catalog import _bytes, _path

    if (release is None or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_id)
            or not allows("skill:" + skill_id) or not resource.endswith(".py")):
        raise ValueError("skill_script_not_approved")
    if not any(row["id"] == skill_id and row["version"] == version for row in list_installed(release)):
        raise ValueError("skill_script_version_mismatch")
    approved, inventory = _script_approvals(release)
    key = f"skills/{skill_id}/{resource}"
    expected = approved.get(key)
    if not isinstance(expected, str) or expected != inventory.get(key):
        raise ValueError("skill_script_not_approved")
    content = _bytes(_path(model_directory(release) / skill_id, resource), 65536)
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("skill_script_integrity_failed")
    return content, expected
