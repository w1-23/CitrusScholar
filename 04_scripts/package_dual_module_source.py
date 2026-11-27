#!/usr/bin/env python3
"""Create a deterministic source distribution for a committed dual-module release."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
from tempfile import TemporaryDirectory


REQUIRED = (
    "LICENSE", "01_src/scripts/apptainer_launch.py", "docs/dual-module-release.md",
    "modules/workbench/LICENSE", "modules/workbench/wb/api.py", "modules/workbench/wb/web/index.html",
    "modules/workbench/TAVOTTO_UPSTREAM.lock.json", "modules/selfcorpus/pyproject.toml",
    "modules/selfcorpus/corpus/api.py",
)


def run_git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def check_member(entry):
    name = PurePosixPath(entry.name)
    if name.is_absolute() or ".." in name.parts or not (entry.isdir() or entry.isfile()):
        raise ValueError("Archive member is not a regular package path")
    denied = {".git", ".runtime", ".venv", ".workbench-dev", "__pycache__", "node_modules"}
    if denied.intersection(name.parts) or any(part.startswith(".env") and not part.endswith(".example") for part in name.parts):
        raise ValueError(f"Runtime or checkout data in source release: {entry.name}")
    if name.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".key", ".pem", ".pfx", ".sif", ".onnx", ".onnx_data", ".log"}:
        raise ValueError(f"Unpackaged state, secrets or runtime binary: {entry.name}")
    if entry.size > 25 * 1024 * 1024:
        raise ValueError(f"Oversized source package member: {entry.name}")


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def package(repo, revision, output):
    commit = run_git(repo, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    prefix = "citrusscholar-" + commit[:12] + "/"
    with TemporaryDirectory() as temp:
        temporary = Path(temp) / "source.tar"
        with temporary.open("xb") as raw:
            subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "--prefix="+prefix, commit],
                           check=True, stdout=raw)
        with tarfile.open(temporary, "r:") as archive:
            names = set()
            for member in archive:
                check_member(member)
                if member.isfile():
                    names.add(member.name.removeprefix(prefix))
        missing = set(REQUIRED).difference(names)
        if missing:
            raise ValueError("Missing release files: " + ", ".join(sorted(missing)))
        with temporary.open("rb") as raw, output.open("xb") as stream, gzip.GzipFile(
            filename="", mode="wb", mtime=0, fileobj=stream
        ) as packed:
            for block in iter(lambda: raw.read(1024 * 1024), b""):
                packed.write(block)
    return {"source_commit":commit, "files":len(names), "source_archive":output.name, "sha256":sha(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.repository,args.revision,args.output),sort_keys=True))


if __name__ == "__main__":
    main()
