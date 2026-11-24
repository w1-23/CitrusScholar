"""Owned file bytes registered to the requested figure revision."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import xml.etree.ElementTree as ET

from src.figures import projects, store
from src.session import files

FORMATS = ("svg", "png", "pdf")
_MAX_BYTES = {"svg": projects.MAX_PREVIEW_BYTES, "png": 24 * 1024 * 1024, "pdf": 24 * 1024 * 1024}


@dataclass(frozen=True)
class Artifact:
    body: bytes
    name: str
    digest: str


def _valid_format(blob, fmt):
    if fmt == "png":
        return blob.startswith(b"\x89PNG\r\n\x1a\n")
    if fmt == "pdf":
        return blob.startswith(b"%PDF-")
    if b"<!DOCTYPE" in blob.upper() or b"<!ENTITY" in blob.upper():
        return False
    try:
        return ET.fromstring(blob).tag == "{http://www.w3.org/2000/svg}svg"
    except (ValueError, ET.ParseError):
        return False


def read_entry(entry, owner, fmt):
    if not isinstance(entry, dict) or not entry.get("file_id"):
        raise projects.ProjectError("format_missing", f"此版本没有已登记的 {fmt.upper()}。", status=404)
    file = files.get_account_file(entry["file_id"], owner)
    if file:
        try:
            with Path(file["path"]).open("rb") as stream:
                blob = stream.read(_MAX_BYTES[fmt] + 1)
            digest = sha256(blob).hexdigest()
            if (len(blob) <= _MAX_BYTES[fmt] and digest == file.get("sha256")
                    and (not entry.get("sha256") or entry["sha256"] == digest) and _valid_format(blob, fmt)):
                return Artifact(blob, str(entry.get("filename") or file.get("filename") or f"figure.{fmt}"), digest)
        except (KeyError, OSError, TypeError):
            pass
    raise projects.ProjectError("artifact_unavailable", f"此版本的 {fmt.upper()} 文件无法验证。", status=404)


def entries(project, owner):
    current = project.get("exports_json")
    return {**store.list_revision_exports(project["figure_id"], owner, project["render_version"]),
            **(current if isinstance(current, dict) else {})}


def capabilities(project, owner, *, replayable):
    registered = entries(project, owner)
    state = {}
    for fmt in FORMATS:
        try:
            read_entry(registered.get(fmt), owner, fmt)
        except projects.ProjectError as exc:
            if exc.code == "format_missing" and replayable and fmt in ("png", "pdf"):
                state[fmt] = {"state": "generatable"}
            else:
                state[fmt] = {"state": "unavailable", "reason": exc.message}
        else:
            state[fmt] = {"state": "ready"}
    return state


def download(figure_id, owner, revision, fmt, *, generate=False):
    if fmt not in FORMATS:
        raise projects.ProjectError("format_unsupported", "图件工作台仅支持 SVG、PNG 和 PDF。", status=404)
    project = projects.get_for_owner(figure_id, owner)
    stored = store.get_revision_export(figure_id, owner, revision, fmt)
    if stored:
        return read_entry(stored["export"], owner, fmt)
    if revision != project["render_version"]:
        # A generator may have registered its captured revision while the
        # current project changed. Never substitute the new current artifact.
        stored = store.get_revision_export(figure_id, owner, revision, fmt)
        if stored:
            return read_entry(stored["export"], owner, fmt)
        raise projects.ProjectError("revision_conflict", "图件版本已更新，请重新打开。", status=409)
    current = project.get("exports_json") or {}
    entry = current.get(fmt) if isinstance(current, dict) else None
    if entry or not generate:
        return read_entry(entry, owner, fmt)
    result = projects.generate_export(figure_id, owner, fmt, expected_revision=revision)
    if result.get("render_version") != revision or result.get("format") != fmt:
        raise projects.ProjectError("export_revision_unconfirmed", "图件导出返回了无法确认的版本。", status=409)
    return read_entry(result["export"], owner, fmt)
