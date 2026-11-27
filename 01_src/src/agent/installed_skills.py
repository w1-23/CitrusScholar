"""Read metadata for the same immutable Skill directory mounted by DeepAgents."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path

import yaml


RQA_ID = "research-question-answering"
_PIN_FIELDS = ("native_skill_release", "skill_release_inventory_sha256")
_IDENTITY_FIELDS = ("id", "version", "sha256")
_CATALOG_FIELDS = (*_IDENTITY_FIELDS, "description", "applicability", "depends_on",
                   "required_resources", "requires_any")
_USE_ACTIVE_RELEASE = object()
_turn_skills = ContextVar("installed_turn_skills", default=None)


def release_path() -> Path | None:
    configured = os.getenv("CITRUS_SKILL_RELEASE", "")
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("skill_release_path_must_be_absolute")
    return path


def release_pin(release: Path | None) -> dict[str, str]:
    """Capture the existing server release and its inventory identity together."""
    if release is None:
        return dict.fromkeys(_PIN_FIELDS, "")
    if not release.is_absolute():
        raise ValueError("skill_release_path_must_be_absolute")
    root = release.resolve(strict=True)
    model_directory(root)
    from .research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

    digest = hashlib.sha256(_bytes(_path(root, "inventory.json"), _MAX_RESOURCE_BYTES)).hexdigest()
    return dict(zip(_PIN_FIELDS, (str(root), digest)))


def stored_release_pin(state: dict) -> dict:
    """Return only the existing release fields from trusted persisted state."""
    return {key: state[key] for key in _PIN_FIELDS if key in state}


def task_release(state: dict, *, resume: bool, continuation_pin: dict | None = None) -> Path | None:
    """Restore a server-frozen release for queued input, clarification or recovery."""
    saved = stored_release_pin(state) if resume or continuation_pin is None else continuation_pin
    if not saved and not resume and continuation_pin is None:
        actual = release_pin(release_path())
    else:
        path, digest = (saved.get(key, "") for key in _PIN_FIELDS)
        if (type(path) is not str or type(digest) is not str or bool(path) != bool(digest)
                or (digest and not re.fullmatch(r"[a-f0-9]{64}", digest))):
            raise ValueError("skill_release_pin_invalid")
        actual = release_pin(Path(path) if path else None)
        if path and (actual[_PIN_FIELDS[0]] != path or actual[_PIN_FIELDS[1]] != digest):
            raise ValueError("skill_release_changed_during_recovery")
        if resume and not path and (state.get("capability_runtime") or {}).get(_PIN_FIELDS[1]):
            raise ValueError("skill_release_missing_during_recovery")
    state.update(actual)
    return Path(actual[_PIN_FIELDS[0]]) if actual[_PIN_FIELDS[0]] else None


@dataclass(frozen=True)
class TurnSkills:
    """One authorized method view for the SDK, File and research workspace."""

    release: Path | None
    scope: frozenset[str] = frozenset()

    @classmethod
    def from_selection(cls, release, selected):
        # Explicit selections can also be descriptive on an unrestricted turn.
        return cls(release)

    def allows(self, skill_id: str) -> bool:
        from src.core.capability_policy import allows

        return (self.release is not None and (not self.scope or skill_id in self.scope)
                and allows("skill:" + skill_id))

    def rows(self) -> list[dict]:
        if self.release is None:
            return []
        return [row for row in list_installed(self.release) if self.allows(row["id"])]


@contextmanager
def bind_turn_skills(view: TurnSkills):
    token = _turn_skills.set(view)
    try:
        yield
    finally:
        _turn_skills.reset(token)


def current_turn_skills() -> TurnSkills:
    bound = _turn_skills.get()
    return bound if bound is not None else TurnSkills(release_path())


_RQA_READ_TOOLS = frozenset({"ls", "glob", "grep", "read_file", "workspace_list", "workspace_glob",
                             "workspace_search", "workspace_read", "research_workspace", "load_skill", "read_skill"})
_RQA_PATH = re.compile(r"(?:^|/)skills/" + RQA_ID + r"(?:$|/|[\s\"'])")


def is_rqa_tool_result(name, content, args=None) -> bool:
    if name not in _RQA_READ_TOOLS:
        return False
    args = args or {}
    if args.get("skill_id") == RQA_ID or any(_RQA_PATH.search(value) for key in ("path", "file_path")
            if isinstance(value := args.get(key), str)):
        return True
    return RQA_ID in str(content or "")


def project_rqa_history(messages, identity, view: TurnSkills | None, *, parent_run_id: str = ""):
    """Redact only RQA method observations in the model's outgoing message view."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, get_notification_metadata

    permitted = view is not None and view.release is not None and view.allows(RQA_ID)
    permitted_runs = {identity.run_id, parent_run_id} - {""} if permitted else frozenset()
    cutoff = max((index for index, message in enumerate(messages) if isinstance(message, HumanMessage)
                  and message.additional_kwargs.get("context_trusted") is True
                  and message.additional_kwargs.get("context_origin") == "user"
                  and message.additional_kwargs.get("run_id") == identity.run_id), default=len(messages))
    calls, hidden = {}, set()
    result = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            notification = get_notification_metadata(message)
            skill = message.additional_kwargs.get("skill")
            if (notification and notification["source"] == "pinned_skill"
                    and isinstance(skill, dict) and skill.get("name") == RQA_ID
                    and (not permitted or index < cutoff)):
                continue
        if isinstance(message, AIMessage):
            calls.update((call["id"], call["args"]) for call in message.tool_calls)
        if isinstance(message, ToolMessage):
            meta = message.additional_kwargs
            if (meta.get("context_trusted") is True
                    and meta.get("session_id") == identity.session_id
                    and meta.get("run_id") in permitted_runs):
                result.append(message)
                continue
            method = is_rqa_tool_result(message.name, message.content, calls.get(message.tool_call_id))
            if message.name == "read_tool_result" and isinstance(message.content, str):
                try:
                    data = json.loads(message.content)
                    origin = data.get("tool_result_ref", {}).get("tool_call_id")
                    method = (isinstance(origin, str) and origin in hidden
                              or is_rqa_tool_result(data.get("tool_name"), data.get("result_text")))
                except (AttributeError, TypeError, ValueError):
                    pass
            if method:
                hidden.add(message.tool_call_id)
                receipt = meta.get("result_reference")
                if isinstance(receipt, dict) and isinstance(receipt.get("tool_call_id"), str):
                    hidden.add(receipt["tool_call_id"])
                # Keep the provider's paired result but not the retired method,
                # attached bytes, or any pointers that expose its stored copy.
                message = message.model_copy(update={"content": "本轮不可使用这条技能方法回执。",
                    "artifact": None, "additional_kwargs": {}})
        result.append(message)
    return tuple(result)


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


def active_skills(release=_USE_ACTIVE_RELEASE):
    """Public current-release catalog for the Skill picker and turn admission."""
    from src.core.capability_policy import allows

    try:
        if release is _USE_ACTIVE_RELEASE:
            release = release_path()
        rows = list_installed(release) if release is not None else []
    except (OSError, ValueError, KeyError):
        return []
    return [row for row in rows if allows("skill:" + row["id"])]


def scenario_catalog() -> list[dict]:
    """Installed product methods; availability never grants execution rights."""
    rows = active_skills()
    return [{key: row.get(key, [] if key in {"depends_on", "required_resources", "requires_any"} else "")
             for key in _CATALOG_FIELDS} for row in rows]


def validate_scenario_selection(value, *, catalog=None) -> list[dict[str, str]]:
    """Accept only exact current release identities for a new product request.

    Archived identities are not upgraded or accepted as a current selection.
    """
    if value is None or (type(value) is list and not value):
        return []
    if type(value) is not list or len(value) > 4:
        raise ValueError("invalid_selected_skills")
    rows = {row["id"]: row for row in (scenario_catalog() if catalog is None else catalog)}
    result, seen = [], set()
    for selected in value:
        if (type(selected) is not dict or set(selected) != set(_IDENTITY_FIELDS)
                or any(type(selected[key]) is not str for key in _IDENTITY_FIELDS)):
            raise ValueError("invalid_selected_skill_identity")
        sid = selected["id"]
        row = rows.get(sid)
        if sid in seen or row is None or any(selected[key] != row[key] for key in _IDENTITY_FIELDS):
            raise ValueError("selected_skill_identity_mismatch")
        seen.add(sid)
        result.append({key: row[key] for key in _IDENTITY_FIELDS})
    return result


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
