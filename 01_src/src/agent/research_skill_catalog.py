"""Pinned root-release discovery and exact, bounded resource reads.

The manifest authenticates the release identity; only requested guidance bytes
are read. Vendor scripts/assets are optional resources, never imports or tools.
The archived prompt registry remains an independent compatibility namespace.

Current releases stay pinned by exact identity. A prior release remains readable
only from ``02_config/skill-history/<id>/<version>`` after its own manifest
authenticates the requested identity. Archived bytes are never silently replaced
by current bytes, and a missing optional file is reported instead of granted.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = "02_config/skills.catalog.json"
HISTORY_ROOT = "02_config/skill-history"
PINNED = {
    "candidate-search": ("3.0.0", "6d423a6010bcceb5e88b81404499944b329941bb6f265d93868ed7b280836572"),
    "reference-verification": ("3.0.0", "04694fa7594ae4d6f553875b4a0844d4ecd8583a4fe086c0f2c7f1d01d245939"),
    "data-audit": ("3.0.0", "0630f3acc35029d8e9b1b8968890c8da44791911e5175bead179007b1158e024"),
    "nature-figure": ("3.0.0", "2660ac78e3602c2e9fa3e6cd0e88d6997f1335d408c0de66d142041cb03f41b9"),
}
# Compatibility aliases naming the known prior releases that remain readable.
# Discovery itself is generic under HISTORY_ROOT; these values are retained for
# callers that reference the established archived identities by name.
_ARCHIVED_NATURE_IDENTITY = ("2.0.0", "00779e01d2c8756245c3553adb8abf451c4eb1dcd15c46ce7192ce3892d7f127")
_ARCHIVED_NATURE_ROOT = HISTORY_ROOT + "/nature-figure/2.0.0"
_ARCHIVED_DATA_AUDIT_IDENTITY = ("2.0.0", "87247d295c5fcb2bd97ac833361afb0f9376c69bc35de309f5a0e09839b60339")
_ARCHIVED_DATA_AUDIT_ROOT = HISTORY_ROOT + "/data-audit/2.0.0"
_MAX_CATALOG_BYTES = 256_000
_MAX_RESOURCE_BYTES = 1_000_000
_MAX_FILES = 512
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def _path(root: Path, name: str) -> Path:
    if (type(name) is not str or not name or len(name) > 300 or "\\" in name or ":" in name
            or "\x00" in name or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("invalid_skill_resource_path")
    relative = PurePosixPath(name)
    if relative.is_absolute():
        raise ValueError("invalid_skill_resource_path")
    candidate = root.joinpath(*relative.parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("skill_resource_outside_package")
    current = candidate
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("skill_resource_link_prohibited")
        current = current.parent
    return candidate


def _bytes(path: Path, limit: int) -> bytes:
    if not path.is_file():
        raise ValueError("skill_resource_missing")
    if path.stat().st_size > limit:
        raise ValueError("skill_resource_size_limit")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("skill_resource_size_limit")
    return content


def _catalog():
    data = json.loads(_bytes(_path(ROOT, CATALOG_PATH), _MAX_CATALOG_BYTES))
    if (type(data) is not dict or data.get("schema") != "citrus.skill-catalog/1"
            or data.get("version") != "2.0.0" or type(data.get("skills")) is not list
            or len(data["skills"]) != len(PINNED)):
        raise ValueError("invalid_research_skill_catalog")
    rows = data["skills"]
    if any(type(row) is not dict for row in rows):
        raise ValueError("invalid_research_skill_catalog")
    ids = [row.get("id") for row in rows]
    if any(type(sid) is not str for sid in ids) or set(ids) != set(PINNED):
        raise ValueError("invalid_research_skill_catalog")
    return rows


def _manifest(row, identity):
    sid = row["id"]
    if ((row.get("version"), row.get("sha256")) != identity
            or row.get("path") != "skills/" + sid or row.get("entry") != "SKILL.md"):
        raise ValueError("research_skill_identity_mismatch")
    files = row.get("files")
    if type(files) is not list or not 1 <= len(files) <= _MAX_FILES:
        raise ValueError("invalid_skill_manifest")
    records = {}
    for record in files:
        if type(record) is not dict or set(record) != {"path", "size", "sha256"}:
            raise ValueError("invalid_skill_manifest_record")
        name = record["path"]
        # Validate syntax here; optional resources need not be touched at discovery.
        _path(_path(ROOT, row["path"]), name)
        if (name in records or type(record["size"]) is not int
                or not 0 <= record["size"] <= _MAX_RESOURCE_BYTES
                or type(record["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None):
            raise ValueError("invalid_skill_manifest_record")
        records[name] = record
    digest = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    if digest != row["sha256"] or "SKILL.md" not in records:
        raise ValueError("skill_manifest_integrity_mismatch")
    return records


def _archived_root(skill_id, version):
    """Return the history root for an exact, path-safe prior release, if any."""
    if (type(skill_id) is not str or type(version) is not str
            or _SAFE_COMPONENT.fullmatch(skill_id) is None
            or _SAFE_COMPONENT.fullmatch(version) is None):
        return None
    root = HISTORY_ROOT + "/" + skill_id + "/" + version
    try:
        if not _path(_path(ROOT, root), "manifest.json").is_file():
            return None
    except ValueError:
        return None
    return root


def _archived_row(skill_id, version, sha256):
    """Authenticate a prior identity from its own manifest before serving it."""
    root = _archived_root(skill_id, version)
    if root is None:
        return None
    try:
        row = json.loads(_bytes(_path(ROOT, root + "/manifest.json"), _MAX_CATALOG_BYTES))
    except (OSError, ValueError, TypeError):
        return None
    if (type(row) is not dict or row.get("id") != skill_id
            or (row.get("version"), row.get("sha256")) != (version, sha256)):
        return None
    return root, row


def _resource_roots(row):
    """Archived bytes first for prior identities, then the current package."""
    roots = []
    if PINNED.get(row.get("id")) != (row.get("version"), row.get("sha256")):
        archived = _archived_root(row.get("id"), row.get("version"))
        if archived is not None:
            roots.append(archived)
    roots.append(row["path"])
    return list(dict.fromkeys(roots))


def _verified_bytes(row, resource, record):
    """Read one exact manifest resource; never substitute different bytes."""
    missing, mismatch = False, False
    for root in _resource_roots(row):
        try:
            content = _bytes(_path(_path(ROOT, root), resource), record["size"])
        except ValueError as exc:
            if str(exc) == "skill_resource_missing":
                missing = True
                continue
            raise
        if len(content) == record["size"] and hashlib.sha256(content).hexdigest() == record["sha256"]:
            return content
        mismatch = True
    raise ValueError("skill_resource_integrity_mismatch" if mismatch
                     else "skill_resource_missing")


def _text(row, records, resource):
    if resource not in records:
        raise ValueError("skill_resource_not_in_manifest")
    return _verified_bytes(row, resource, records[resource]).decode("utf-8")


def _required_resources(records):
    # Local method references suffice for default loads. Vendored libraries,
    # scripts, templates and tests remain exact-manifest optional resources.
    return ["SKILL.md", *sorted(name for name in records
                 if name.startswith("references/") and name.endswith(".md"))]


def _info(row, records):
    text = _text(row, records, "SKILL.md")
    header = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if header is None:
        raise ValueError("invalid_skill_frontmatter")
    fields = {key: value.strip() for key, value in
              re.findall(r"^(name|description):[ \t]*(.+)$", header[1], re.M)}
    version = re.search(r"^(?:  )?version:[ \t]*[\"']?([^\"'\r\n]+)[\"']?[ \t]*$", header[1], re.M)
    if fields.get("name") != row["id"] or version is None or version[1].strip() != row["version"]:
        raise ValueError("research_skill_identity_mismatch")
    return {"id": row["id"], "version": row["version"], "sha256": row["sha256"],
            "description": fields.get("description", row["id"]), "applicability": "",
            "entrypoint": "SKILL.md", "required_resources": _required_resources(records),
            "method_resources": [name for name in records if name == "ADAPTER.md"
                                 or name in _required_resources(records) and name != "SKILL.md"],
            "depends_on": [], "requires_any": [], "verified": True}


def list_skills():
    """Discovery failures are local; no root-release failure gates generic work."""
    try:
        rows = _catalog()
    except (OSError, ValueError, KeyError, TypeError):
        return []
    result = []
    for row in rows:
        try:
            result.append(_info(row, _manifest(row, PINNED[row["id"]])))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def has_identity(skill_id, version, sha256):
    identity = (version, sha256)
    if PINNED.get(skill_id) == identity:
        return True
    return _archived_row(skill_id, version, sha256) is not None


def _resolve_row(skill_id, version, sha256):
    identity = (version, sha256)
    if PINNED.get(skill_id) == identity:
        row = next((entry for entry in _catalog() if entry["id"] == skill_id), None)
        if row is None:
            raise ValueError("research_skill_unavailable")
        return row, False
    archived = _archived_row(skill_id, version, sha256)
    if archived is None:
        raise ValueError("research_skill_identity_mismatch")
    return archived[1], True


def resolve(skill_id, version, sha256):
    """Resolve current and prior guidance by full, authenticated identity."""
    row, _ = _resolve_row(skill_id, version, sha256)
    records = _manifest(row, (version, sha256))
    return row, records, _info(row, records)


def validate_release(skill_id, version, sha256):
    """Report missing/invalid manifest files for a current or prior release.

    ``verified`` means every declared file is present at its recorded digest.
    ``delivery_ready`` means the method entrypoint and local references are all
    readable. Optional scripts/assets/tests may therefore be absent without
    making authorized tools unavailable; archived prior releases are checked
    against their own history manifest, never against current bytes.
    """
    row, archived = _resolve_row(skill_id, version, sha256)
    records = _manifest(row, (version, sha256))
    missing, invalid = [], []
    for name, record in records.items():
        try:
            _verified_bytes(row, name, record)
        except ValueError as exc:
            if str(exc) == "skill_resource_missing":
                missing.append(name)
            else:
                invalid.append(name)
    required = _required_resources(records)
    unavailable = set(missing) | set(invalid)
    info_ok = "SKILL.md" not in unavailable
    if info_ok:
        try:
            _info(row, records)
        except (OSError, ValueError):
            info_ok = False
            invalid.append("SKILL.md")
    return {"id": row["id"], "version": row["version"], "sha256": row["sha256"],
            "archived": archived,
            "verified": not missing and not invalid,
            "delivery_ready": info_ok and all(name not in unavailable for name in required),
            "missing_files": sorted(set(missing)),
            "invalid_files": sorted(set(invalid)),
            "required_resources": required,
            "available_required_resources": [name for name in required
                                             if name not in unavailable]}


def read_resource(skill_id, version, sha256, resource="", *, start_char=0, max_chars=3000):
    if (type(start_char) is not int or start_char < 0
            or type(max_chars) is not int or not 1 <= max_chars <= 20000):
        raise ValueError("invalid_skill_pagination")
    row, records, info = resolve(skill_id, version, sha256)
    name = resource or info["entrypoint"]
    text = _text(row, records, name)
    start = min(start_char, len(text))
    end = min(start + max_chars, len(text))
    page = {"skill_id": skill_id, "version": version, "resource": name,
            "source": "installed:research-skills", "commit": "", "sha256": records[name]["sha256"],
            "text": text[start:end], "total_chars": len(text), "start_char": start,
            "next_start_char": end if end < len(text) else None,
            "content_origin": "installed_skill_guidance", "read_only": True}
    return info, page
