# SPDX-License-Identifier: AGPL-3.0-only
"""Content identity for the authorized deploy-time renderer and mounted runtime."""
from functools import lru_cache
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path

from .sandbox import SandboxError

from types import ModuleType


def historical_renderer(language, expected):
    if language not in {"python", "r", "frozen-python"}:
        raise SandboxError("The original renderer is unavailable.")
    candidates = (os.getenv("WORKBENCH_PREVIOUS_SOURCE", ""),
                  os.getenv("WORKBENCH_OLDER_SOURCE", ""))
    for configured in dict.fromkeys(filter(None, candidates)):
        root = Path(configured)
        try:
            if not root.is_absolute() or root.resolve(strict=True) != root or any(
                    p.is_symlink() or p.stat().st_mode & 0o222 for p in (root, *root.rglob("*"))):
                continue
            if identity(language, root=root) != expected:
                continue
            path = root / "wb/sandbox.py"
            module = ModuleType("wb._historical_sandbox")
            module.__file__ = str(path)
            module.__package__ = "wb"
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
            break
        except (OSError, SandboxError):
            continue
    else:
        raise SandboxError("The original renderer is unavailable.")

    def run(*args, **kwargs):
        try:
            return module.run(*args, **kwargs)
        except module.SandboxError as exc:
            raise SandboxError(str(exc)) from None

    return run, lambda: identity(language, root=root)


POLICY = "isolated-relative-bundle/v1"


@lru_cache(maxsize=64)
def _image_digest(path, _fingerprint):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _hash_image(path):
    def fingerprint():
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    before = fingerprint()
    result = _image_digest(path, before)
    if fingerprint() != before:
        raise OSError("image changed")
    return result


def identity(language, *, root=None):
    root = root or Path(__file__).resolve().parents[1]
    try:
        image = Path(os.environ["WORKBENCH_IMAGE"]).resolve(strict=True)
        if image.suffix != ".sif" or not image.is_file():
            raise OSError("invalid image")
        paths = [(root, root / "requirements.lock"), (root, root / "wb/sandbox.py")]
        paths += [(root, f) for f in sorted((root / "renderer").iterdir()) if f.is_file()]
        if language in {"python", "frozen-python"}:
            upstream = Path(os.environ["WORKBENCH_TAVOTTO"]).resolve(strict=True)
            paths.extend((upstream, upstream / f) for f in ("uv.lock", "packaging/runtime-lock.json"))
            src = upstream / "src/tavotto"
            if not src.is_dir():
                raise OSError("missing Tavotto source")
            paths.extend((upstream, f) for f in sorted(src.rglob("*"))
                         if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc")
        if language == "frozen-python":
            packages = Path(os.environ["WORKBENCH_FROZEN_PACKAGES"]).resolve(strict=True)
            for name in ("citrus_figures", "citrus_audit"):
                package = packages / name
                if not package.is_dir() or package.is_symlink() or not (package / "__init__.py").is_file():
                    raise OSError("missing frozen dataset runtime")
                paths.extend((packages, f) for f in sorted(package.rglob("*.py")) if f.is_file())
        combined = sha256(json.dumps([POLICY, language, _hash_image(image)]).encode())
        for base, path in paths:
            if path.is_symlink():
                raise OSError("symlink in runtime source")
            with path.open("rb") as stream:
                combined.update(json.dumps([path.relative_to(base).as_posix(),
                                           file_digest(stream, "sha256").hexdigest()]).encode())
        return combined.hexdigest()
    except (OSError, KeyError) as exc:
        raise SandboxError("The pinned renderer runtime is unavailable.") from exc
