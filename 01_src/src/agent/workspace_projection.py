"""Explicit readonly projections, never a repository filesystem backend.

Enumeration reads catalog metadata only. File bytes are opened only for a
requested page/reference or a bounded search, with the private-store link rules.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).absolute().parents[3]
# Product implementation texts reviewed for this projection. No globs, config,
# credentials, state, environment directories, uploads or runtime data roots.
SOURCE_FILES = frozenset({
    "01_src/src/agent/capabilities.py", "01_src/src/agent/execution.py",
    "01_src/src/agent/identity.py", "01_src/src/agent/host_tools.py",
    "01_src/src/agent/workspace_storage.py", "01_src/src/agent/workspace_tools.py",
    "01_src/src/agent/workspace_projection.py", "01_src/src/agent/research_skill_catalog.py",
    "01_src/src/agent/skill_tools.py", "01_src/src/research/tools.py",
    "01_src/src/research/reference_extraction.py", "01_src/src/research/data_audit_rules.py",
    "01_src/src/retrieval/candidate_pool.py", "01_src/src/retrieval/bm25.py",
    "01_src/src/figures/spec.py", "01_src/src/figures/manifest.py",
})
ROOTS = ("/input", "/data", "/skills", "/01_src", "/workspace", "/outputs")
_UNBOUND = object()
_skill_release = ContextVar("workspace_projection_skill_release", default=_UNBOUND)


@contextmanager
def bind_skill_release(release: Path | None):
    """Expose the host's authenticated release snapshot to this tool call."""
    token = _skill_release.set(release)
    try:
        yield
    finally:
        _skill_release.reset(token)


def _projection_release():
    release = _skill_release.get()
    if release is _UNBOUND:
        # Standalone projection callers have no task checkpoint to restore.
        from .installed_skills import release_path
        return release_path()
    return release  # Explicit None must not pick up a newly activated release.


def within(key, scope):
    return scope == "/" or key == scope or key.startswith(scope + "/")


def bounded_bytes(path: Path, limit: int) -> bytes:
    from .workspace_storage import _no_links
    _no_links(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("workspace_file_limit_or_type")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        _no_links(path)
        after = path.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise PermissionError("workspace_file_changed")
        if len(data) > limit:
            raise ValueError("workspace_file_limit_or_type")
        return data
    finally:
        os.close(fd)


def _installed_resources(release, scope):
    from src.core.capability_policy import allows
    from .installed_skills import list_installed
    from .workspace_storage import MAX_FILE_BYTES, virtual_path

    inventory = json.loads(bounded_bytes(release / "inventory.json", MAX_FILE_BYTES * 16))
    if not isinstance(inventory, dict):
        raise ValueError("invalid_skill_inventory")
    installed = {row["id"]: row for row in list_installed(release) if allows("skill:" + row["id"])}
    for name, digest in sorted(inventory.items()):
        mount, _, remainder = name.partition("/")
        skill_id, _, resource = remainder.partition("/")
        key = "/" + name
        if mount != "skills" or skill_id not in installed or not resource or not within(key, scope):
            continue
        try:
            virtual_path(key)
            info = (release / name).stat(follow_symlinks=False)
        except (OSError, ValueError):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            continue
        row = installed[skill_id]
        yield key, {"kind": "workspace_skill_resource", "path": key, "sha256": digest,
                    "size_bytes": info.st_size, "skill_id": skill_id, "skill_version": row["version"],
                    "catalog_sha256": row["sha256"], "read_only": True}


def resources(scope):
    """Yield allowed resource names and metadata without opening reference bodies."""
    from .workspace_storage import MAX_FILE_BYTES, virtual_path
    if scope == "/" or scope.startswith("/01_src"):
        for name in sorted(SOURCE_FILES):
            key = "/" + name
            if within(key, scope):
                yield key, {"kind": "workspace_source", "path": key, "read_only": True}
    if scope != "/" and not scope.startswith("/skills"):
        return
    try:
        release = _projection_release()
        if release is not None:
            yield from _installed_resources(release, scope)
            return
    except (OSError, ValueError, TypeError, KeyError):
        return



def read(key, descriptor):
    from .workspace_storage import MAX_FILE_BYTES
    if descriptor["kind"] == "workspace_source":
        if key[1:] not in SOURCE_FILES:
            raise PermissionError("workspace_projection_denied")
        root = ROOT
    else:
        from src.core.capability_policy import allows
        if not allows("skill:" + descriptor["skill_id"]):
            raise PermissionError("skill_disabled")
        root = _projection_release()
        if root is None:
            raise PermissionError("workspace_projection_denied")
    path = root.joinpath(*key[1:].split("/"))
    # Lexical allowlist plus ancestor link checks, never caller-selected host paths.
    data = bounded_bytes(path, MAX_FILE_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if descriptor["kind"] == "workspace_skill_resource" and (
            digest != descriptor["sha256"] or len(data) != descriptor["size_bytes"]):
        raise ValueError("workspace_skill_integrity_mismatch")
    text = data.decode("utf-8")
    if "\x00" in text:
        raise ValueError("workspace_text_required")
    return text, {**descriptor, "sha256": digest, "size_bytes": len(data)}
