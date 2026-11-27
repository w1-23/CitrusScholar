"""Activate reviewed Markdown methods on top of a verified retirement candidate."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from stage_8043_release import (METHOD_IDS, METHOD_RELEASE_FILE, METHOD_RELEASE_SCHEMA,
                                RETAINED_METHODS, check_skills, hashes, inventory,
                                method_only_release, read_object, require, sha, write_object)

HOST_PACKAGES = frozenset({'pandera', 'typeguard', 'typing-inspect', 'mypy-extensions'})


def _stage_host_runtime(output, wheels, lock):
    pins = {line.split('==', 1)[0]: line for line in lock.read_text().splitlines()
            if line.split('==', 1)[0] in HOST_PACKAGES}
    require(set(pins) == HOST_PACKAGES, 'Incomplete, fixed Pandera requirements')
    with tempfile.TemporaryDirectory(prefix='8043-host-requirements-') as tmp:
        req = Path(tmp) / 'requirements.txt'
        req.write_text('\n'.join(pins[key] for key in sorted(pins)) + '\n')
        subprocess.run([sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
                        'install', '--only-binary=:all:', '--no-index', '--no-deps', '--no-compile',
                        '--require-hashes', '--find-links', str(wheels), '-r', str(req),
                        '--target', str(output / 'host-python')], check=True)
    bin_path = output / 'host-python' / 'bin'
    if bin_path.is_dir():
        shutil.rmtree(bin_path)  # generated CLI shims are not part of the Host import surface


def stage(retired, retired_sha256, candidates, output, *, wheels, lock):
    check_skills(str(retired), retired_sha256)
    selected = read_object(retired / 'release.json')
    require(set(selected) == RETAINED_METHODS and not (retired / 'fusion').exists(),
            'Verify old method retirement before publishing replacements')
    require(not any((retired / 'skills' / skill).exists() for skill in METHOD_IDS),
            'Old active method resources remain')
    require(output.is_absolute() and not output.exists() and not output.is_symlink(), 'Output must be new')
    payloads = {}
    for skill in sorted(METHOD_IDS):
        root = candidates / skill
        members = hashes(inventory(root))
        require('SKILL.md' in members and members and all(name == 'SKILL.md' or (
                name.startswith('references/') and name.endswith('.md')) for name in members),
                f'Only reviewed method Markdown may be installed: {skill}')
        name_line = f'name: {skill}'
        header = (root / 'SKILL.md').read_text().split('---', 2)
        require(len(header) == 3 and not header[0].strip() and name_line in header[1].splitlines(),
                f'Invalid method identity: {skill}')
        digest = hashlib.sha1(usedforsecurity=False)  # historical catalog version field is 40 hex chars
        digest.update(b'citrusscholar.scientific-method-resource/1\0')
        for name, checksum in sorted(members.items()):
            digest.update(name.encode() + b'\0' + bytes.fromhex(checksum))
        selected[skill] = digest.hexdigest()
        payloads[skill] = (root, members)
    output.mkdir(mode=0o700)
    def skip_retired_manifests(directory, entries):
        return set(entries) & {"inventory.json", "release.json"} if Path(directory) == retired else set()

    shutil.copytree(retired, output, dirs_exist_ok=True, copy_function=shutil.copyfile,
                    ignore=skip_retired_manifests)
    for skill, (root, members) in payloads.items():
        dest = output / 'skills' / skill
        shutil.copytree(root, dest, copy_function=shutil.copyfile)
        require(members == hashes(inventory(dest)), 'Method copy failed integrity verification')
    write_object(output / 'release.json', selected)
    write_object(output / METHOD_RELEASE_FILE, {'schema': METHOD_RELEASE_SCHEMA, 'methods': sorted(METHOD_IDS)})
    _stage_host_runtime(output, wheels, lock)
    all_files = hashes(inventory(output))
    require(method_only_release(output, all_files), 'Invalid method-only release')
    write_object(output / 'inventory.json', all_files)
    pin = sha(output / 'inventory.json')
    check_skills(str(output), pin, active=True)
    return pin


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('retired', 'candidates', 'output', 'wheels', 'lock'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--retired-sha256', required=True)
    args = parser.parse_args()
    print(stage(args.retired, args.retired_sha256, args.candidates, args.output,
                wheels=args.wheels, lock=args.lock))
