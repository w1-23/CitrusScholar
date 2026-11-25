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


ROOT_FILES = frozenset({"SKILL.md", "README.md", "LICENSE", "NOTICE", "NOTICE.md",
                        "THIRD_PARTY_NOTICES.md", "CAPABILITY_MIGRATION.md"})
RESOURCE_DIRECTORIES = frozenset({"scripts", "references", "assets", "examples"})
NATURE_ACTIVE_FILES = frozenset({
    "SKILL.md", "references/citrus-host-adapter.md", "references/figure-methods.md",
    "references/quality.md", "scripts/freeze_groups.py", "scripts/render_grouped.py",
    "examples/grouped_svg.py",
})


def source_path(value, *, allow_root=False):
    if allow_root and value in ("", "."):
        return PurePosixPath(".")
    if (not isinstance(value, str) or len(value) > 240 or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} or part.startswith("-") for part in value.split("/"))
            or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid_skill_source_path")
    return PurePosixPath(value)


def stage_package(repository: Path, commit: str, output: Path | None, *,
                  python_root: Path, source_package: str | None,
                  active_files: frozenset[str] | None = None,
                  skill_directory: PurePosixPath = PurePosixPath(".")) -> None:
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
                elif output and path.is_relative_to(skill_directory):
                    relative = path.relative_to(skill_directory)
                    if not relative.parts:
                        if not member.isdir():
                            raise ValueError("invalid_skill_directory")
                        continue
                    if active_files is not None:
                        if relative.as_posix() not in active_files:
                            continue
                    elif relative.parts[0] not in RESOURCE_DIRECTORIES and relative.as_posix() not in ROOT_FILES:
                        continue
                    target = output.joinpath(*relative.parts)
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


def stage_nature_qa(repository: Path, commit: str, release: Path) -> None:
    from citrus_figures.nature_runtime import (RELEASE_VENDOR_COMMIT, RELEASE_VENDOR_MEMBERS,
                                               RELEASE_VENDOR_PATH, SOURCE, validate_vendor)

    vendor = release / RELEASE_VENDOR_PATH
    files = []
    for path in RELEASE_VENDOR_MEMBERS:
        origin = path if path == "LICENSE" else f"vendor/upstream/{path}"
        data = subprocess.check_output(["git", "-C", str(repository), "show", f"{commit}:{origin}"])
        target = vendor / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})
    provenance = {"source": SOURCE, "commit": RELEASE_VENDOR_COMMIT, "files": files}
    (vendor / "provenance.json").write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
    validate_vendor(vendor, expected_commit=RELEASE_VENDOR_COMMIT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--lock", type=Path, default=root / "02_config/standalone-skills.lock.json")
    parser.add_argument("--repositories", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fusion-handoff", type=Path,
                        help="Stage the reviewed fusion handoff instead of the legacy active skills.")
    parser.add_argument("--enable", nargs="*", help="Stage only this subset; an empty list stages no methods.")
    parser.add_argument("--include-runtime", action="store_true",
                        help="Stage executable Python packages independently of method visibility.")
    args = parser.parse_args(argv)
    entries = json.loads(args.lock.read_bytes())
    if not isinstance(entries, dict) or len(entries) > 100:
        raise ValueError("invalid_skill_lock")
    available = set(entries)
    if args.fusion_handoff is not None:
        from stage_fusion_release import FUSION_IDS, NATURE

        unchanged = "research-question-answering"
        entries = {unchanged: entries[unchanged]}
        available = {unchanged, NATURE} | FUSION_IDS
    names = available if args.enable is None else set(args.enable)
    if not names <= available:
        raise ValueError("unknown_skill")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("skill_release_output_already_exists")

    sys.path[:0] = [str(root / "01_src"), str(root / "packages")]
    from src.agent.installed_skills import list_installed

    with tempfile.TemporaryDirectory(prefix=".citrus-skill-stage-", dir=args.output.parent) as staging:
        release = Path(staging) / "release"
        skills = release / "skills"
        skills.mkdir(parents=True)
        selected = {}
        runtime = {}
        approved = {}
        python_root = release / "python"
        runtime_names = {name for name, entry in entries.items()
                         if isinstance(entry, dict) and "python_package" in entry} if args.include_runtime else set()
        nature_entry = entries.get("nature-figure")
        adapted_nature = (isinstance(nature_entry, dict)
                          and nature_entry.get("repository") == "https://github.com/w1-23/nature-figure.git"
                          and nature_entry.get("skill_directory", ".") in {".", ""})
        nature_qa = ({"nature-figure"} & (set(entries) if args.include_runtime else names)
                     if adapted_nature else set())
        for name in sorted((names & entries.keys()) | runtime_names | nature_qa):
            entry = entries[name]
            if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    or not isinstance(entry, dict)
                    or not re.fullmatch(r"[a-f0-9]{40}", entry.get("commit", ""))
                    or not isinstance(entry.get("dependencies", {}), dict)
                    or not isinstance(entry.get("repository"), str)
                    or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+",
                                        entry["repository"])):
                raise ValueError("invalid_skill_lock_entry")
            repository = args.repositories / name
            directory = source_path(entry.get("skill_directory", "."), allow_root=True)
            if name in names and directory.parts:
                kind = subprocess.check_output(["git", "-C", str(repository), "cat-file", "-t",
                                                f"{entry['commit']}:{directory.as_posix()}"]).strip()
                if kind != b"tree":
                    raise ValueError("invalid_skill_directory")
            for filename, expected in entry.get("dependencies", {}).items():
                if (not re.fullmatch(r"requirements(?:-[a-z0-9-]+)?\.lock", filename)
                        or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                    raise ValueError("invalid_skill_dependency_lock")
                raw = subprocess.check_output(["git", "-C", str(repository), "show",
                                               f"{entry['commit']}:{filename}"])
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("skill_dependency_lock_mismatch")
            if name in nature_qa:
                stage_nature_qa(repository, entry["commit"], release)
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
                          python_root=python_root, source_package=source_package,
                          active_files=NATURE_ACTIVE_FILES if name == "nature-figure" and adapted_nature else None,
                          skill_directory=directory)
            if name in names:
                selected[name] = entry["commit"]
                scripts = entry.get("approved_scripts", [])
                if not isinstance(scripts, list) or len(scripts) > 16:
                    raise ValueError("invalid_approved_skill_scripts")
                for script in scripts:
                    relative = source_path(script)
                    file = skills / name / relative
                    if (relative.parts[0] not in {"scripts", "examples"} or relative.suffix != ".py"
                            or not file.is_file() or file.stat().st_size > 65536):
                        raise ValueError("approved_skill_script_unavailable")
                    approved[f"skills/{name}/{relative}"] = hashlib.sha256(file.read_bytes()).hexdigest()
        if args.fusion_handoff is not None:
            from stage_fusion_release import stage_fusion

            stage_fusion(root, args.fusion_handoff, release, names, include_runtime=args.include_runtime,
                         selected=selected, approvals=approved)
        for module in runtime:
            if not (python_root / module / "__init__.py").is_file():
                raise ValueError("incomplete_skill_python_package")
        (release / "runtime.json").write_text(json.dumps(runtime, sort_keys=True) + "\n", encoding="utf-8")
        (release / "release.json").write_text(json.dumps(selected, sort_keys=True) + "\n", encoding="utf-8")
        (release / "approved-scripts.json").write_text(json.dumps(approved, sort_keys=True) + "\n", encoding="utf-8")
        list_installed(release)
        inventory = {path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(release.rglob("*")) if path.is_file()}
        (release / "inventory.json").write_text(json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8")
        release.rename(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
