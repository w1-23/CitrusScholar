#!/usr/bin/env python3
"""Compare locked staged standalone packages with the host, including one exact overlay."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

# The host-only review adapter preserves exact merged-cell addresses, annotations,
# safe ZIP input, and cross-target summaries. Do not change either pin silently.
OVERLAY = Path('citrus_audit/workbook.py')
OVERLAY_HASHES = (
    '93aa851610d0ce557917eaec298374bd98cedc311c5b7b456ab824144c093070',
    '67514b1f021f4869e1c4b850a93a4d5d5ea4ee05b214ba674da1004d19541df9',
)
LEGACY_FIXTURES = {
    'citrus_refcheck': {'reference_verdict_eval_test.py', 'reference_verdict_fixtures.py'},
    'citrus_audit': {'plan_test.py', 'region_workbook_test.py'},
}


def files(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f'Missing regular Skill package directory: {path}')
    result = {}
    for file in path.rglob('*'):
        relative = file.relative_to(path)
        if '__pycache__' in relative.parts:
            continue
        if file.is_symlink() or (not file.is_file() and not file.is_dir()):
            raise ValueError(f'Invalid Skill package member: {file}')
        if file.is_file():
            result[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def compare(staged_root, host_root):
    errors = []
    for module, allowed in LEGACY_FIXTURES.items():
        staged, host = files(staged_root / module), files(host_root / module)
        for path in sorted(host.keys() - staged.keys() - {Path(name) for name in allowed}):
            errors.append(f'{module}/{path}: extra host file')
        if module == OVERLAY.parts[0]:
            path = Path(OVERLAY.name)
            if (staged.get(path), host.get(path)) != OVERLAY_HASHES:
                errors.append(f'{OVERLAY}: upstream and host overlay must both match exact pins')
        for path, sha in sorted(staged.items()):
            if Path(module) / path == OVERLAY:
                continue
            if path not in host:
                errors.append(f'{module}/{path}: missing host file')
            elif sha != host[path]:
                errors.append(f'{module}/{path}: differs from locked repository')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged-python', type=Path, required=True)
    parser.add_argument('--host-packages', type=Path, required=True)
    args = parser.parse_args()
    try:
        errors = compare(args.staged_python, args.host_packages)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Skill lock drift: {exc}\n')
    if errors:
        parser.exit(1, 'Skill lock drift:\n' + '\n'.join(errors) + '\n')
    print('Locked standalone runtimes and exact audit workbook overlay verified')


if __name__ == '__main__':
    main()
