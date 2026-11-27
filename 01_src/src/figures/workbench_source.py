"""Prepare an inert Workbench handoff from an accepted frozen Python render."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import stat

from . import projects, store
from .script_outputs import normalize_output_contract

_BUNDLE_LIMIT = 12 * 1024 * 1024  # Independent Workbench input limit.


@dataclass(frozen=True)
class FrozenInput:
    binding: str
    filename: str
    sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class FrozenScriptBundle:
    renderer: str = field(repr=False)
    renderer_sha256: str
    inputs: tuple[FrozenInput, ...]
    render_version: int = 1
    style_json: bytes | None = field(default=None, repr=False)


def _read_verified(path, size, digest, remaining):
    if type(size) is not int or not 0 < size <= remaining:
        raise projects.ProjectError("workbench_bundle_too_large", "脚本和数据超过工作台导入大小上限。", status=409)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError
            data = handle.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError
    except (OSError, ValueError):
        raise projects.ProjectError("source_changed", "冻结的数据文件已变更或不可读。", status=409) from None
    return data


def _saved_style(project, receipt):
    from citrus_figures.style_replay import typed_payload_sha256, valid_baseline

    baseline = receipt.get("style_replay")
    final_manifest = project.get("manifest_json")
    overrides = project.get("overrides_json")
    if (not valid_baseline(baseline) or not isinstance(final_manifest, dict)
            or not isinstance(overrides, list) or not 0 < len(overrides) <= 200
            or any(not isinstance(item, dict) or set(item) != {"gid", "prop", "value"}
                   or not isinstance(item["gid"], str) or not isinstance(item["prop"], str)
                   for item in overrides)):
        raise projects.ProjectError("source_unverified", "此版本缺少可核验的样式记录。", status=409)
    try:
        checksum = typed_payload_sha256(overrides)
        proof = final_manifest.get("style_replay_proof")
        if proof != {"coordinates_sha256": baseline["coordinates_sha256"], "overrides_sha256": checksum}:
            raise ValueError
        payload = json.dumps({"baseline": baseline, "overrides": overrides, "overrides_sha256": checksum},
                             ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        if len(payload) > 64 * 1024:
            raise ValueError
        return payload
    except (TypeError, ValueError, RecursionError):
        raise projects.ProjectError("source_unverified", "此版本缺少可核验的样式记录。", status=409) from None


def prepare_frozen_bundle(*, figure_id: str, owner: int, session: str, render_version: int = 1) -> FrozenScriptBundle:
    """Read only the precise accepted entrypoint and table bytes in this session.

    The caller must authenticate the account and owning session. This is a
    server-internal snapshot, not a listing of internal account files.
    """
    project = store.get_project(figure_id, owner) if session else None
    if not project or project.get("session_id") != session or project.get("source_kind") != "script":
        raise projects.ProjectError("not_found", "图件不存在或无权访问。", status=404)
    source = project.get("source_json") or {}
    frozen = projects._frozen_render_state(source)
    figure = next(item for item in normalize_output_contract(source["output_contract"])["figures"]
                  if item["name"] == source["output_key"])
    renderer = figure["render_script"]
    if (type(render_version) is not int or render_version < 1 or not project.get("exports_json")
            or project.get("render_version") != render_version):
        raise projects.ProjectError("source_unverified", "请求的图件版本不可用。", status=409)
    style_json = _saved_style(project, frozen["receipt"]) if render_version > 1 else None
    from src.session.files import get_account_file

    remaining = _BUNDLE_LIMIT - len(renderer.encode("utf-8")) - len(style_json or b"")
    inputs = []
    for item in frozen["input_snapshots"]:
        entry = get_account_file(str(item.get("file_id") or ""), owner, allow_internal=True, touch=False)
        name = item.get("sandbox_name")
        index = len(inputs)
        if (name != f"input_{index}.json" or not entry or entry.get("session_id") != session or
                entry.get("visibility") != "internal" or entry.get("sha256") != item["sha256"] or
                entry.get("size_bytes") != item.get("size_bytes")):
            raise projects.ProjectError("source_unverified", "工作台无法确认图件绑定的冻结数据。", status=409)
        content = _read_verified(entry["path"], entry["size_bytes"], item["sha256"], remaining)
        inputs.append(FrozenInput(item["variable"], name, item["sha256"], content))
        remaining -= len(content)
    if store.get_project(figure_id, owner) != project:
        raise projects.ProjectError("revision_conflict", "读取期间图件已更新，请重新打开。", status=409)
    return FrozenScriptBundle(renderer, frozen["receipt"]["renderer_sha256"], tuple(inputs), render_version, style_json)
