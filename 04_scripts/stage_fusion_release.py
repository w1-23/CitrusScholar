"""Verify and stage the reviewed fusion handoff as an immutable Skill release input."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat


FUSION_IDS = frozenset({"literature-verification", "data-audit", "research-integrity"})
NATURE = "nature-figure"
FUSION_ENTRY = "03_skill_fusion/run.py"
DEPENDENCY_LOCK = "03_skill_fusion/requirements-8043.lock"
APPROVED_SCRIPT = "scripts/run.py"
_SAFE_PATH_PART = re.compile(r"\.?[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
_MAX_MEMBER = 8_000_000
_MAX_BUNDLE_FILES = 94  # Code request also reserves bounded input and metadata slots.


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _below(root: Path, name: str) -> Path:
    if not isinstance(name, str) or len(name) > 240 or not all(
            _SAFE_PATH_PART.fullmatch(part) for part in name.split("/")):
        raise ValueError(f"invalid_fusion_path: {name!r}")
    return root.joinpath(*PurePosixPath(name).parts)


def _read_member(root: Path, name: str, limit: int = _MAX_MEMBER) -> bytes:
    member = _below(root, name)
    parent = member.parent
    while parent != root:
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError(f"invalid_fusion_source_directory: {name}")
        parent = parent.parent
    info = member.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError(f"invalid_fusion_source_member: {name}")
    with member.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"oversize_fusion_source_member: {name}")
    return raw


def _checked(raw: bytes, expected: str, description: str) -> bytes:
    if not isinstance(expected, str) or not _HASH.fullmatch(expected) or _sha(raw) != expected:
        raise ValueError(f"fusion_source_hash_mismatch: {description}")
    return raw


def _write(root: Path, name: str, raw: bytes) -> str:
    target = _below(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(raw)
    return _sha(raw)


def _source_root(root: Path) -> Path:
    if not root.is_absolute() or not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("invalid_fusion_input_directory")
    return root.resolve(strict=True)


def verified_handoff(root: Path, lock: dict) -> tuple[Path, dict]:
    root = _source_root(root)
    expected = lock["handoff_manifest"]
    manifest = _checked(_read_member(root, "handoff.sha256", 65_536), expected["sha256"], "handoff.sha256")
    rows = {}
    for line in manifest.decode().splitlines():
        digest, sep, filename = line.partition("  ./")
        if not sep or filename in rows or not _HASH.fullmatch(digest):
            raise ValueError("invalid_fusion_handoff_manifest")
        rows[filename] = digest
        _checked(_read_member(root, filename), digest, filename)
    if len(rows) != expected["members"]:
        raise ValueError("incomplete_fusion_handoff_manifest")
    for name, digest in (lock["handoff_files"] | lock["nature_files"]).items():
        if rows.get(name) != digest:
            raise ValueError(f"fusion_source_lock_handoff_mismatch: {name}")
    return root, rows


def _locked_copy(source: Path, name: str, expected: str, output: Path, dest: str) -> str:
    raw = _checked(_read_member(source, name), expected, name)
    return _write(output, dest, raw)


def _requirements(worker: dict) -> bytes:
    base, wheels = worker["base"], worker["wheels"]
    if base.keys() & wheels.keys():
        raise ValueError("duplicate_fusion_requirements")
    requirements = base | {k: v["version"] for k, v in wheels.items()}
    if any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", k) or not _VERSION.fullmatch(v)
           for k, v in requirements.items()):
        raise ValueError("invalid_fusion_requirements")
    return "".join(f"{k}=={v}\n" for k, v in sorted(requirements.items())).encode()


def _adapt_worker_sandbox(raw: bytes, pin: dict) -> bytes:
    """Preserve the stricter inherited RLIMIT_NOFILE used by the Code Worker."""
    _checked(raw, pin["original_sha256"], "original sandbox.py")
    original = b"    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))\n"
    replacement = (b"    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
                   b"    resource.setrlimit(resource.RLIMIT_NOFILE, tuple(\n"
                   b"        1024 if value == resource.RLIM_INFINITY else min(value, 1024)\n"
                   b"        for value in (soft, hard)))\n")
    if raw.count(original) != 1:
        raise ValueError("unexpected_fusion_sandbox_overlay_input")
    return _checked(raw.replace(original, replacement), pin["staged_sha256"], "staged sandbox.py")


def _stage_analysis_sources(handoff: Path, upstreams: Path, lock: dict, bundle: Path) -> dict:
    members = {}
    sandbox_pin = lock["sandbox_compatibility"]
    for name, digest in lock["handoff_files"].items():
        dest = f"03_skill_fusion/{name}"
        if name == sandbox_pin["path"]:
            original = _checked(_read_member(handoff, name), digest, name)
            members[dest] = _write(bundle, dest, _adapt_worker_sandbox(original, sandbox_pin))
        else:
            members[dest] = _locked_copy(handoff, name, digest, bundle, dest)
    metadata = "03_skill_fusion/sandbox-compatibility.json"
    members[metadata] = _write(bundle, metadata, _json(sandbox_pin))
    snapshot_index = json.loads(_read_member(upstreams, "sources.json", 8_192))
    verified_sources = {}
    for key, pin in lock["sources"].items():
        if snapshot_index.get(key) != [pin["repository"], pin["commit"]]:
            raise ValueError(f"fusion_upstream_identity_mismatch: {key}")
        if key == "nature":
            continue
        verified_sources[key] = [pin["repository"], pin["commit"]]
        for name, digest in pin["files"].items():
            source = f"{key}/tree/{name}"
            dest = f"upstreams/{source}"
            members[dest] = _locked_copy(upstreams, source, digest, bundle, dest)
    name = "upstreams/sources.json"
    members[name] = _write(bundle, name, _json(verified_sources))
    license_record = lock["eda_license"]
    raw = _checked(license_record["text"].encode(), license_record["sha256"], "EDA LICENSE.md")
    name = f"upstreams/eda/tree/{license_record['path']}"
    members[name] = _write(bundle, name, raw)
    members[DEPENDENCY_LOCK] = _write(bundle, DEPENDENCY_LOCK, _requirements(lock["worker"]))
    if len(members) > _MAX_BUNDLE_FILES:
        raise ValueError("fusion_bundle_member_limit")
    return members


def _stage_analysis(repo: Path, handoff: Path, upstreams: Path, release: Path,
                    names: set[str], lock: dict, selected: dict, approvals: dict) -> None:
    bundle = release / "fusion"
    files = _stage_analysis_sources(handoff, upstreams, lock, bundle)
    versions = {}
    for name in sorted(names):
        manifest = json.loads(_read_member(bundle, f"03_skill_fusion/{name}/manifest.json"))
        if (manifest.get("name") != name or manifest.get("entrypoint") != f"run.py {name}"
                or manifest.get("interface_version") != 1 or not _VERSION.fullmatch(manifest.get("version", ""))):
            raise ValueError(f"invalid_fusion_method_manifest: {name}")
        versions[name] = manifest["version"]
    worker = lock["worker"]
    metadata = {"schema": "citrus.fusion-bundle/1", "entrypoint": FUSION_ENTRY,
                "skills": versions, "files": files,
                "dependencies": {"python": worker["python"], "platform": worker["platform"],
                                 "system": worker["system"], "requirements": DEPENDENCY_LOCK,
                                 "sha256": files[DEPENDENCY_LOCK]}}
    _write(bundle, "bundle.json", _json(metadata))
    wrapper = _read_member(repo, "01_src/src/agent/fusion_worker_entry.py", 65_400)
    for name in sorted(names):
        base = f"skills/{name}"
        public = {}
        for source, dest in ((f"{name}/SKILL.md", "SKILL.md"),
                             (f"{name}/references/workflow.md", "references/workflow.md"),
                             ("INTERFACE_CONTRACT.md", "INTERFACE_CONTRACT.md")):
            public[dest] = _locked_copy(handoff, source, lock["handoff_files"][source], release, f"{base}/{dest}")
        script = f"{base}/{APPROVED_SCRIPT}"
        approvals[script] = _write(release, script, f"# Citrus approved fusion skill: {name}\n".encode() + wrapper)
        # User-visible release identity covers payload bytes and this Skill's approved launcher.
        # This is a 40-character content identity for the existing release schema, not a Git commit.
        selected[name] = _sha(_json({"id": name, "version": versions[name], "files": files,
                                    "public": public, "launcher": approvals[script]}))[:40]


def _stage_nature(handoff: Path, release: Path, lock: dict, *, active: bool) -> None:
    from citrus_figures.nature_runtime import (RELEASE_VENDOR_COMMIT, RELEASE_VENDOR_MEMBERS,
                                               RELEASE_VENDOR_PATH, SOURCE, validate_vendor)
    origin = lock["sources"]["nature"]
    if RELEASE_VENDOR_COMMIT != origin["commit"] or SOURCE.removeprefix("https://github.com/") != origin["repository"]:
        raise ValueError("fusion_nature_worker_pin_mismatch")
    pins = lock["nature_files"]
    license_name = "methods/licenses/nature/LICENSE"
    if active:
        for source, digest in pins.items():
            dest = f"skills/{NATURE}/LICENSE" if source == license_name else source.replace("methods/", "skills/", 1)
            if dest == "skills/nature-shared/SKILL.md":
                raise ValueError("fusion_shared_skill_cannot_be_discovered")
            _locked_copy(handoff, source, digest, release, dest)
        _write(release, f"skills/{NATURE}/provenance.json", _json({
            "repository": origin["repository"], "commit": origin["commit"], "license": origin["license"],
            "omissions": lock["nature_omissions"],
        }))
    vendor = release / RELEASE_VENDOR_PATH
    files = []
    for name in RELEASE_VENDOR_MEMBERS:
        source = license_name if name == "LICENSE" else "methods/" + name.removeprefix("skills/")
        digest = _locked_copy(handoff, source, pins[source], vendor, name)
        files.append({"path": name, "sha256": digest})
    _write(vendor, "provenance.json", _json({"source": SOURCE, "commit": RELEASE_VENDOR_COMMIT, "files": files}))
    validate_vendor(vendor, expected_commit=RELEASE_VENDOR_COMMIT)


def stage_fusion(repo: Path, handoff: Path, release: Path, names: set[str], *,
                 include_runtime: bool, selected: dict, approvals: dict) -> None:
    lock = json.loads(_read_member(repo, "02_config/skill-fusion.lock.json", 256_000))
    if lock.get("schema") != "citrus.fusion-source-lock/1":
        raise ValueError("invalid_fusion_source_lock")
    handoff, _rows = verified_handoff(handoff, lock)
    if FUSION_IDS & names:
        upstreams = _source_root(handoff.parent / "upstreams")
        _stage_analysis(repo, handoff, upstreams, release, FUSION_IDS & names, lock, selected, approvals)
    if NATURE in names or include_runtime:
        _stage_nature(handoff, release, lock, active=NATURE in names)
    if NATURE in names:
        selected[NATURE] = lock["sources"]["nature"]["commit"]
