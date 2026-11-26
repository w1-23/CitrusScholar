"""Short-lived, process-local preview memoization; validation belongs before hits.

No cancellation promise is made for a running sandbox. Concurrent equal previews
share a bounded striped lock, then re-check authorization, source integrity and
version. Saves invalidate entries and never use preview bytes as export bytes.

Audit F-05 changed two things here:

* **Apptainer now has an immutable environment identity.** A SIF image is a
  single file; its SHA-256 is the environment. Caching used to be limited to
  Docker/WSL-Docker digest tags, so every Apptainer preview re-ran the whole
  script. The digest is only trusted when it is *verified* — either the SIF on
  disk hashes to the configured digest, or an operator explicitly declared the
  digest verified for this deployment. A mismatch disables reuse instead of
  serving bytes from an unknown image.
* **Preview and publication are separate profiles.** A preview is a low-DPI
  draft of one revision; the exported figure keeps publication quality. The
  profile is part of the cache key, so a preview can never be mistaken for an
  export at the same revision.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable

PROTOCOL_VERSION = 3
_TTL = 30.0
_MAX_ENTRIES = 24
_MAX_BYTES = 48 * 1024 * 1024
PREVIEW_DPI = 110
EXPORT_DPI = 300
_DIGEST_IMAGE = re.compile(r"(?:[A-Za-z0-9./:_-]+@)?sha256:[a-f0-9]{64}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_CACHE: OrderedDict[str, tuple[float, int, tuple[int, str], dict]] = OrderedDict()
_LOCK = threading.RLock()
_STRIPES = [threading.Lock() for _ in range(16)]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def preview_dpi(default: int = PREVIEW_DPI) -> int:
    """Low-DPI draft resolution; never the publication value."""
    raw = os.environ.get("CITRUS_FIGURES_PREVIEW_DPI", "").strip()
    try:
        value = int(raw) if raw else int(default)
    except ValueError:
        value = int(default)
    return max(48, min(300, value))


def render_profile(*, preview: bool = True) -> dict[str, Any]:
    """The rendering profile a cache entry belongs to."""
    from src.config import settings

    export = int(getattr(settings, "FIGURES_DPI", EXPORT_DPI) or EXPORT_DPI)
    return {"mode": "preview" if preview else "export",
            "dpi": preview_dpi() if preview else export,
            "formats": ["svg"] if preview else ["svg", "png", "pdf"],
            "quality": "draft" if preview else "publication"}


def sif_digest(path: str | os.PathLike[str]) -> str:
    """SHA-256 of a local SIF file, or an empty string when unreadable."""
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return ""
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def apptainer_identity(*, image: str, declared_sha256: str) -> tuple[dict, dict]:
    """(immutable identity, verification record) for an Apptainer SIF image.

    An empty identity means "do not reuse": no SIF path, no declared digest, a
    digest that does not verify, or a file that hashes differently. The
    verification record is kept either way so a receipt can say exactly why
    reuse was refused.
    """
    values = {"image": str(image or ""), "declared_sha256": str(declared_sha256 or "").lower()}
    if not values["image"].lower().endswith(".sif"):
        return {}, {**values, "identity": "not_a_sif"}
    if not _SHA256.fullmatch(values["declared_sha256"]):
        return {}, {**values, "identity": "no_pinned_digest"}
    actual = sif_digest(values["image"])
    if actual:
        if actual != values["declared_sha256"]:
            return {}, {**values, "identity": "sif_sha256", "actual_sha256": actual,
                        "verified": "digest_mismatch"}
        return ({"identity": "sif_sha256", "sha256": actual, "verified": "file_digest"},
                {**values, "identity": "sif_sha256", "actual_sha256": actual,
                 "verified": "file_digest"})
    if os.environ.get("CITRUS_APPTAINER_SIF_VERIFIED", "").strip() == "1":
        return ({"identity": "sif_sha256", "sha256": values["declared_sha256"],
                 "verified": "operator_declaration"},
                {**values, "identity": "sif_sha256", "verified": "operator_declaration"})
    return {}, {**values, "identity": "unverified_sif"}


def environment_key() -> dict:
    from src.config import settings
    from importlib.metadata import PackageNotFoundError, version
    names = ("FIGURES_SANDBOX_BACKEND", "FIGURES_SANDBOX_IMAGE", "FIGURES_SANDBOX_IMAGE_SHA256",
             "FIGURES_SANDBOX_RUNTIME", "FIGURES_SANDBOX_ALLOW_LOCAL", "FIGURES_SANDBOX_WSL_DISTRO",
             "FIGURES_DPI", "FIGURES_SANDBOX_MEMORY_MB", "FIGURES_SANDBOX_CPUS")
    values = {name: getattr(settings, name, None) for name in names}
    # Mutable container tags do not identify an environment. Disable reuse for
    # those script backends rather than assuming the tag still names one image.
    image = str(values.get("FIGURES_SANDBOX_IMAGE") or "")
    backend = str(values.get("FIGURES_SANDBOX_BACKEND") or "").lower()
    values["immutable_image"] = (backend in {"docker", "wsl-docker"}
                                 and bool(_DIGEST_IMAGE.fullmatch(image)))
    sif, sif_verification = apptainer_identity(
        image=image, declared_sha256=str(values.get("FIGURES_SANDBOX_IMAGE_SHA256") or ""))
    values["sif"] = sif
    values["sif_verification"] = sif_verification
    if sif:
        # A verified SIF is an immutable environment: same file, same bytes.
        values["immutable_image"] = True
    values["python"] = sys.version
    values["executable"] = sys.executable
    packages = {}
    for name in ("matplotlib", "numpy", "pandas"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    values["packages"] = packages
    from matplotlib import font_manager
    values["fonts"] = [(entry.fname, Path(entry.fname).stat().st_mtime_ns, Path(entry.fname).stat().st_size)
                       for entry in font_manager.fontManager.ttflist if Path(entry.fname).is_file()]
    # A rolling source update invalidates even an already-running service.
    values["engine"] = [(path.name, path.stat().st_mtime_ns, path.stat().st_size)
                        for path in sorted(Path(__file__).parent.glob("*.py"))]
    return values


def invalidate(owner: int, figure_id: str) -> None:
    with _LOCK:
        for key in [key for key, entry in _CACHE.items() if entry[2] == (int(owner), str(figure_id))]:
            _CACHE.pop(key, None)


def _ttl() -> float:
    raw = os.environ.get("CITRUS_FIGURES_PREVIEW_TTL", "").strip()
    try:
        return max(0.0, float(raw)) if raw else _TTL
    except ValueError:
        return _TTL


def render_preview(project: dict, owner: int, edits: Any, *, validate: Callable[[], None],
                   render: Callable[[], dict], is_script: bool, preview: bool = True) -> dict:
    environment = environment_key()
    identity = (int(owner), str(project["figure_id"]))
    profile = render_profile(preview=preview)
    key = _digest({"protocol": PROTOCOL_VERSION, "owner": owner, "project": identity[1],
                   "version": project.get("render_version"), "source": project.get("source_json"),
                   "refs": project.get("data_refs_json"), "saved": project.get("overrides_json"),
                   "environment": environment, "profile": profile, "edits": edits})
    reusable = not is_script or environment["immutable_image"]
    # Script execution can be random; this is only the same disposable draft,
    # never a promise that a future save will reproduce these bytes exactly.
    with _STRIPES[int(key[:8], 16) % len(_STRIPES)]:
        try:
            validate()
        except Exception:
            invalidate(*identity)
            raise
        now = time.monotonic()
        with _LOCK:
            for stale in [k for k, v in _CACHE.items() if now - v[0] >= _ttl()]:
                _CACHE.pop(stale, None)
            found = _CACHE.get(key) if reusable else None
            if found is not None:
                _CACHE.move_to_end(key)
                result = deepcopy(found[3])
                result["cache"] = {"hit": True, "profile": profile,
                                   "environment": _environment_receipt(environment)}
                return result
        result = render()
        validate()
        if reusable:
            size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size <= _MAX_BYTES:
                with _LOCK:
                    _CACHE[key] = (time.monotonic(), size, identity, deepcopy(result))
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > _MAX_ENTRIES or sum(item[1] for item in _CACHE.values()) > _MAX_BYTES:
                        _CACHE.popitem(last=False)
        result["cache"] = {"hit": False, "reusable": bool(reusable), "profile": profile,
                           "environment": _environment_receipt(environment)}
        return result


def _environment_receipt(environment: dict) -> dict:
    """The small, explainable view of an environment identity."""
    return {"immutable_image": bool(environment.get("immutable_image")),
            "sif": environment.get("sif") or {},
            "sif_verification": environment.get("sif_verification") or {},
            "backend": environment.get("FIGURES_SANDBOX_BACKEND")}
