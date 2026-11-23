"""Stage Skill resources and Python packages from trusted, fixed commits."""
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


ROOT_FILES = frozenset({"SKILL.md", "README.md", "LICENSE", "NOTICE", "NOTICE.md", "CAPABILITY_MIGRATION.md"})
RESOURCE_DIRECTORIES = frozenset({"scripts", "references", "assets", "examples"})


def stage_package(repository: Path, commit: str, output: Path | None, *,
                  python_root: Path, source_package: str | None) -> None:
    module = PurePosixPath(source_package) if source_package else None
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
                if module and path.is_relative_to(module):
                    if not member.isdir() and path.suffix not in {".py", ".json", ".yaml", ".md"}:
                        raise ValueError("invalid_skill_python_member")
                    target = python_root / module.name / path.relative_to(module)
                elif output and (path.parts[0] in RESOURCE_DIRECTORIES or member.name in ROOT_FILES):
                    target = output.joinpath(*path.parts)
                else:
                    continue
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
    parser.add_argument("--include-runtime", action="store_true",
                        help="Stage executable Python packages independently of method visibility.")
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
        runtime = {}
        python_root = release / "python"
        runtime_names = {name for name, entry in entries.items()
                         if isinstance(entry, dict) and "python_package" in entry} if args.include_runtime else set()
        for name in sorted(names | runtime_names):
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
            source_package = entry.get("python_package") if name in runtime_names else None
            if name in runtime_names:
                if (not isinstance(source_package, str)
                        or not re.fullmatch(r"(?:packages/)?[a-z][a-z0-9_]*", source_package)):
                    raise ValueError("invalid_skill_python_package")
                module = PurePosixPath(source_package).name
                if module in runtime:
                    raise ValueError("duplicate_skill_python_package")
                runtime[module] = entry["commit"]
            stage_package(repository, entry["commit"], skills / name if name in names else None,
                          python_root=python_root, source_package=source_package)
            if name in names:
                selected[name] = entry["commit"]
        for module in runtime:
            if not (python_root / module / "__init__.py").is_file():
                raise ValueError("incomplete_skill_python_package")
        (release / "runtime.json").write_text(json.dumps(runtime, sort_keys=True) + "\n", encoding="utf-8")
        (release / "release.json").write_text(json.dumps(selected, sort_keys=True) + "\n", encoding="utf-8")
        list_installed(release)
        inventory = {path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(release.rglob("*")) if path.is_file()}
        (release / "inventory.json").write_text(json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8")
        release.rename(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
