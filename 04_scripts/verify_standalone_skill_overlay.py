#!/usr/bin/env python3
"""Compare locked staged standalone packages with the host, including exact reviewed overlays."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

# Every sanctioned divergence pins both the upstream and the host bytes.
# Keep this allowlist exact. A package-wide exception would hide source drift.
OVERLAYS = {
    Path('citrus_audit/detectors/paperconan.py'): (
        '336915b080a3d0a56a3c1698d3c5411e250379fdc71a8305168146d2f256b6ec',
        '4931694f683ab7e5487e8881c0c6bdd7348f424f4a1159f8777fb6c7459d94fa',
    ),
    Path('citrus_audit/methods.py'): (
        'dcf9f8a85156e966a372461ace584d79c0c0de649dcc1054d6d68dfe188f2879',
        '562c029ee148de052a8609dba37f2ee4375e1b69d46be25635993ef84396005d',
    ),
    Path('citrus_audit/readers.py'): (
        'a6bc31f39ffb3ac521691bbc502faa755f0ee5106f437ba26378ae83d1fe3bf3',
        '0bf03614c4e68cabea806eba922c6e7c6cf73084f01a84d69d7e0432061d9f24',
    ),
    Path('citrus_audit/review.py'): (
        'ab3ed719876b70d1f402d3eade97b1f9ed5178b5751a7eec4e792c78c9f26546',
        'e3b74ed4d080f9a42858b5239ba4fb9acea71b9c3d2917a067d0e3aec58248a1',
    ),
    Path('citrus_audit/workbook.py'): (
        '93aa851610d0ce557917eaec298374bd98cedc311c5b7b456ab824144c093070',
        '348b009ce6badb094ccdd1e5888c8c7ab869d7937df8b17afbd80c063422b858',
    ),
    # The audited host baseline preserves a checked, macro-free workbook export.
    Path('citrus_audit/_zip_safety.py'): (
        '05948433912499ec34282f2be3ecf811e367ce48161122a4eb4fc5cf8377cc4b',
        '24a3bcc31007d5d23578c7a04613d376ba016b22cfa00f72a97683f42d9c612d',
    ),
    Path('citrus_refcheck/lookup.py'): (
        '917d509f299b62b3ef67dbe3139f2ec7a466fc6aa179f49089cee31ec4d8cbdd',
        '5946b28dce14f25c2b5b2526f425ddb3fcac2d4f43c1061a02a0828d5e46df48',
    ),
    Path('citrus_refcheck/providers.py'): (
        'a82130c2575e9ce82563ff8ca38eb26e05234d59622e4f588a9d13ce8ee74962',
        '6800495f303f15eacaaf260bceb2fc0dcaf4836f77161f958560da06491e300f',
    ),
    Path('citrus_refcheck/verdict.py'): (
        '8803c6fb88bbeabdcffd78b34ba73a64cdf8426a81956e45f4f26d19a62880b8',
        'e781f0dc9026671c4e8d39c9282c2cc3d8dd89b216e20c160bc3f2cd62e76ef4',
    ),
    Path('citrus_refcheck/verification.py'): (
        '4bc3ec4e6a3888c2b352f61d1a973a604460b53a8684fd0f9c299dd7fd5aade7',
        '6a6dc5a3d3a3bd4fcaf1f17e473b09ec90e8b64af84dc3f2c2b1f6939b95c418',
    ),
}
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
        for entry, pins in sorted(OVERLAYS.items()):
            if entry.parts[0] != module:
                continue
            path = entry.relative_to(module)
            if (staged.get(path), host.get(path)) != pins:
                errors.append(f'{entry}: upstream and host overlay must both match exact pins')
        for path, sha in sorted(staged.items()):
            if Path(module) / path in OVERLAYS:
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
    print('Locked standalone runtimes and exact host overlays verified')


if __name__ == '__main__':
    main()
