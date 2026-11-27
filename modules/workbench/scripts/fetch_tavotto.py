"""Fetch the fixed Tavotto Git tree outside the tracked Workbench sources."""
import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CAP = 100 * 1024 * 1024


def git_blob(path):
    body = path.read_bytes()
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body, usedforsecurity=False).hexdigest()


def verify(root, lock):
    paths = {"LICENSE": lock["license_file_git_blob"], "uv.lock": lock["dependency_lock_git_blob"],
             "packaging/runtime-lock.json": lock["runtime_lock_git_blob"],
             "src/tavotto/rendercore/fonts_allowlist.json": lock["font_allowlist_git_blob"]}
    paths.update({"src/tavotto/engine/" + n: h for n, h in lock["engine_git_blobs"].items()})
    for path, checksum in paths.items():
        if git_blob(root / path) != checksum:
            raise ValueError(f"Tavotto file differs from upstream lock: {path}")


def fetch(directory, archive=None):
    lock = json.loads((ROOT / "TAVOTTO_UPSTREAM.lock.json").read_text(encoding="utf-8"))
    dest = Path(directory).absolute()
    if dest.exists():
        verify(dest, lock)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as stage:
        stage = Path(stage)
        if archive is None:
            url = "https://codeload.github.com/Tavotto/Tavotto/tar.gz/" + lock["commit"]
            archive = stage / "upstream.tar.gz"
            with urlopen(url, timeout=45) as response, archive.open("wb") as output:
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > CAP:
                        raise ValueError("Upstream archive exceeded download limit")
                    output.write(block)
        if Path(archive).stat().st_size > CAP:
            raise ValueError("Upstream archive exceeded file limit")
        with tarfile.open(archive, "r:gz") as packed:
            items = packed.getmembers()
            expected = "Tavotto-" + lock["commit"]
            if not items or any(i.name.split("/", 1)[0] != expected for i in items):
                raise ValueError("Unexpected commit directory in archive")
            packed.extractall(stage, members=items, filter="data")
        source = stage / expected
        verify(source, lock)
        shutil.move(source, dest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive", type=Path, help="Use a local copy of the pinned GitHub archive")
    args = parser.parse_args()
    fetch(args.destination, args.archive)
    print(f"Verified Tavotto tree: {args.destination}")
