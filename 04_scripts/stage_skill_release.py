"""Stage model-readable Skill resources from trusted, already fetched commits."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT_FILES = frozenset({"SKILL.md", "README.md", "LICENSE", "NOTICE", "NOTICE.md"})
RESOURCE_DIRECTORIES = frozenset({"scripts", "references", "assets", "examples"})


def stage_package(repository: Path, commit: str, output: Path) -> None:
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(repository), "archive", "--format=tar", commit],
                       stdout=archive, check=True)
        archive.seek(0)
        total = 0
        with tarfile.open(fileobj=archive, mode="r:") as package:
            for member in package:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or not path.parts or "\\" in member.name or ":" in member.name
                        or any(part in {"", ".", ".."} for part in path.parts)):
                    raise ValueError("invalid_skill_archive_path")
                if path.parts[0] not in RESOURCE_DIRECTORIES and member.name not in ROOT_FILES:
                    continue
                target = output.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.size
                if not member.isfile() or member.size > 8_000_000 or total > 32_000_000:
                    raise ValueError("invalid_skill_archive_member")
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--lock", type=Path, default=root / "02_config/standalone-skills.lock.json")
    parser.add_argument("--repositories", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--enable", nargs="*", help="Stage only this subset; an empty list stages no methods.")
    args = parser.parse_args(argv)
    entries = json.loads(args.lock.read_bytes())
    if not isinstance(entries, dict) or len(entries) > 100:
        raise ValueError("invalid_skill_lock")
    names = set(entries) if args.enable is None else set(args.enable)
    if not names <= set(entries):
        raise ValueError("unknown_skill")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("skill_release_output_already_exists")

    sys.path.insert(0, str(root / "01_src"))
    from src.agent.installed_skills import list_installed

    with tempfile.TemporaryDirectory(prefix=".citrus-skill-stage-", dir=args.output.parent) as staging:
        release = Path(staging) / "release"
        skills = release / "skills"
        skills.mkdir(parents=True)
        selected = {}
        for name in sorted(names):
            entry = entries[name]
            if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    or not isinstance(entry, dict)
                    or not re.fullmatch(r"[a-f0-9]{40}", entry.get("commit", ""))
                    or not isinstance(entry.get("dependencies"), dict)
                    or not entry.get("repository", "").endswith(f"/{name}.git")):
                raise ValueError("invalid_skill_lock_entry")
            repository = args.repositories / name
            for filename, expected in entry["dependencies"].items():
                if (not re.fullmatch(r"requirements(?:-[a-z0-9-]+)?\.lock", filename)
                        or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                    raise ValueError("invalid_skill_dependency_lock")
                raw = subprocess.check_output(["git", "-C", str(repository), "show",
                                               f"{entry['commit']}:{filename}"])
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("skill_dependency_lock_mismatch")
            stage_package(repository, entry["commit"], skills / name)
            selected[name] = entry["commit"]
        (release / "release.json").write_text(json.dumps(selected, sort_keys=True) + "\n", encoding="utf-8")
        list_installed(release)
        release.rename(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
