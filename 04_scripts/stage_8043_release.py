#!/usr/bin/env python3
"""Stage and verify a pinned 8043 release without launching or updating services."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

SCHEMA = 'citrusscholar.8043-release/1'
SOURCE_MANIFEST = 'LINUX-SOURCE-MANIFEST.json'
METADATA = 'release.json'
IMAGES = {'app': 'application.sif', 'sandbox': 'sandbox.sif', 'namespace': 'namespace.sif'}
MODEL_FILES = ('model_config.json', 'provider_api_keys.json')
RELEASE_FILES = ('deployment.json', 'launch.py', 'source-sha256.json', 'source.tar')
ALIAS_ROOT = 'skills/nature-shared/'
ALIAS_COPY = 'skills/nature-figure/references/shared/'
WRAPPER = '''#!/usr/bin/env python3
"""8043 release entry point. Preflight never executes Apptainer."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import sys

release = Path(__file__).resolve().parent
config_path = release / 'deployment.json'
config = json.loads(config_path.read_text(encoding='utf-8'))
if len(sys.argv) < 2:
    raise SystemExit('Usage: launch.py ROLE [--preflight]')
role = sys.argv[1]
sys.dont_write_bytecode = True
launcher = Path(config['source_root']) / '01_src/scripts/apptainer_launch.py'
spec = spec_from_file_location('citrus_launcher', launcher)
module = module_from_spec(spec)
spec.loader.exec_module(module)
original_command = module.command


def command(settings, deployment_role):
    argv = original_command(settings, deployment_role)
    if deployment_role == 'web':
        origin = Path(module.absolute(settings['model_origin']))
        destination = Path(module.absolute(settings['model_config_root'])) / 'state'
        image_index = argv.index(settings['app_sif'])
        for name in ('model_config.json', 'provider_api_keys.json'):
            source = origin / name
            if source.is_symlink() or not source.is_file():
                raise ValueError('Missing regular model origin file: ' + name)
            argv[image_index:image_index] = ['--bind', f'{source}:{destination / name}:ro']
    return argv


module.command = command
if sys.argv[2:] == ['--preflight']:
    if role not in ('web', 'worker'):
        raise SystemExit('Preflight requires web or worker')
    command(config, role)
    for prefix in ('app', 'sandbox', 'namespace') if role == 'worker' else ('app', 'sandbox'):
        module.verify_image(config[prefix + '_sif'], config[prefix + '_sha256'])
    print(role, 'command and SIF pins verified')
else:
    sys.argv[1:] = ['--config', str(config_path), '--role', role, *sys.argv[2:]]
    module.main()
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value, label, size=64):
    require(isinstance(value, str) and re.fullmatch(f'[0-9a-f]{{{size}}}', value),
            f'{label} must be a full lowercase {size}-digit hash')
    return value


def local_path(value):
    path = Path(value)
    require(path.is_absolute() and str(path) == str(value) and path != Path('/')
            and '..' not in path.parts and not any(c in str(path) for c in ':,\n\r\0'),
            'Expected an absolute normalized bind-safe Linux path')
    return path


def existing(value, *, directory=False):
    path = local_path(value)
    require(path.resolve(strict=True) == path and (path.is_dir() if directory else path.is_file()),
            f'Expected a canonical {"directory" if directory else "regular file"}: {path}')
    return path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def read_object(path):
    value = json.loads(path.read_bytes(), object_pairs_hook=unique_object)
    require(isinstance(value, dict), f'Expected a JSON object: {path}')
    return value


def write_object(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_pin(path, expected):
    digest(expected, str(path))
    require(sha(existing(path)) == expected, f'SHA-256 mismatch: {path}')


def relative_name(value):
    relative = PurePosixPath(value)
    require(value not in ('', '.') and relative.as_posix() == value and not relative.is_absolute()
            and '..' not in relative.parts and '\\' not in value and '\n' not in value and '\r' not in value,
            f'Invalid release member: {value!r}')
    return value


def inventory(root):
    files = {}
    for path in root.rglob('*'):
        name = relative_name(path.relative_to(root).as_posix())
        mode = path.lstat().st_mode
        require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), f'Invalid release file type: {path}')
        if stat.S_ISREG(mode):
            files[name] = {'sha256': sha(path), 'bytes': path.stat().st_size, 'executable': bool(mode & 0o100)}
    return files


def hashes(members):
    return {name: item['sha256'] for name, item in members.items()}


def check_skills(path, pin, *, active=False):
    root = existing(path, directory=True)
    check_pin(root / 'inventory.json', pin)
    expected = read_object(root / 'inventory.json')
    files = hashes(inventory(root))
    files.pop('inventory.json')
    require(files == expected and (root / 'skills').is_dir(), f'Skill inventory content mismatch: {root}')
    if active:
        require('fusion/bundle.json' in files and ALIAS_ROOT + 'SKILL.md' not in files,
                'Active release requires Fusion and must not expose the shared Nature tree as a Skill')
        shared = {key.removeprefix(ALIAS_ROOT): val for key, val in files.items() if key.startswith(ALIAS_ROOT)}
        aliases = {key.removeprefix(ALIAS_COPY): val for key, val in files.items() if key.startswith(ALIAS_COPY)}
        require(len(shared) == 14 and shared == aliases, 'The 14 shared Nature aliases must match byte-for-byte')
    return root


def history(config):
    old = config['historical_skill_releases']
    require(isinstance(old, list), 'Invalid baseline historical release list')
    path = config['skill_release']
    result = [*old, {'path': path, 'mount_path': path, 'sha256': config['skill_release_sha256']}]
    require(len(result) <= 16, 'Too many historical releases for the launcher')
    mounts = set()
    for item in result:
        require(isinstance(item, dict) and set(item) == {'path', 'mount_path', 'sha256'},
                'Invalid historical Skill record')
        mount = local_path(item['mount_path'])
        require(not any(overlap(mount, other) for other in mounts), 'Overlapping historical Skill mounts')
        mounts.add(mount)
        check_skills(item['path'], item['sha256'])
    return result


def check_baseline(path):
    path = existing(path)
    config = read_object(path)
    source = existing(config['source_root'], directory=True)
    expected = read_object(existing(path.parent / 'source-sha256.json'))
    require(expected['commit'] == digest(config['source_head'], 'Baseline commit', 40), 'Baseline commit mismatch')
    actual = inventory(source)
    require(hashes(actual) == expected['files'], 'Baseline source inventory mismatch')
    runtime = read_object(source / SOURCE_MANIFEST)
    actual.pop(SOURCE_MANIFEST)
    require(runtime['integrated_branch_commit'] == config['source_head']
            and runtime['members'] in (hashes(actual), actual),
            'Baseline runtime source manifest mismatch')
    for label in IMAGES:
        check_pin(config[f'{label}_sif'], config[f'{label}_sha256'])
    origins = existing(config['model_origin'], directory=True)
    for name in MODEL_FILES:
        existing(origins / name)
    existing(path.parent / 'launch.py')
    return config, history(config)


def git(repo, *args, stdout=subprocess.PIPE):
    return subprocess.run(['git', '-C', str(repo), '-c', 'tar.umask=000', *args],
                          stdout=stdout, check=True).stdout


def git_tree(repo, commit):
    digest(commit, 'Candidate commit', 40)
    tree = git(repo, 'rev-parse', '--verify', f'{commit}^{{tree}}').decode().strip()
    require(git(repo, 'cat-file', '-t', commit) == b'commit\n', 'Source pin must name a Git commit')
    modes = {}
    for entry in git(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            mode = meta.split(b' ', 1)[0]
            require(mode in (b'100644', b'100755'), 'Source commit includes non-regular Git entry')
            modes[relative_name(name.decode())] = mode == b'100755'
    return digest(tree, 'Git tree', 40), modes


def archive_members(archive, commit, target=None):
    files = {}
    seen = set()
    with tarfile.open(archive, mode='r:') as stream:
        require(stream.pax_headers.get('comment') == commit, 'Source archive commit mismatch')
        for member in stream:
            name = relative_name(member.name)
            require(name not in seen and (member.isdir() or member.isreg()), f'Invalid archive entry: {name}')
            seen.add(name)
            dest = target / name if target else None
            if member.isdir():
                if dest is not None:
                    dest.mkdir(parents=True, exist_ok=True)
                continue
            executable = bool(member.mode & 0o100)
            with stream.extractfile(member) as content:
                if dest is None:
                    checksum = hashlib.file_digest(content, 'sha256').hexdigest()
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open('xb') as output:
                        shutil.copyfileobj(content, output)
                    dest.chmod(0o700 if executable else 0o600)
                    checksum = sha(dest)
            files[name] = {'sha256': checksum, 'bytes': member.size, 'executable': executable}
    return files


def archive_commit(repo, commit, output, target=None):
    tree, modes = git_tree(repo, commit)
    with output.open('xb') as stream:
        git(repo, 'archive', '--format=tar', commit, stdout=stream)
    files = archive_members(output, commit, target)
    require({name: rec['executable'] for name, rec in files.items()} == modes,
            'Source archive paths/modes do not match Git tree')
    return tree, files


def source_overlay(commit, tree, baseline, members):
    return {'schema': 'citrusscholar.linux-source/1', 'deployment': '8043-reliability',
            'baseline_commit': baseline, 'integrated_branch_commit': commit, 'source_tree': tree,
            'source': 'Git archived immutable 8043 release',
            'inventory_scope': 'All archived regular members except this manifest', 'members': members}


def overlap(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def release_config(baseline, preserved, output, commit, sandbox_pin, skill_pin):
    config = dict(baseline, source_root=str(output / 'source'), source_head=commit,
                  sandbox_sha256=sandbox_pin, skill_release=str(output / 'skills-release'),
                  skill_release_sha256=skill_pin, external_fusion_required=True,
                  historical_skill_releases=preserved)
    for label, filename in IMAGES.items():
        config[f'{label}_sif'] = str(output / 'images' / filename)
    return config


def seal(root):
    for path in root.rglob('*'):
        path.chmod(0o500 if path.is_dir() or path.stat().st_mode & 0o100 else 0o400)
    root.chmod(0o500)


def check_seal(root):
    for path in [root, *root.rglob('*')]:
        mode = path.lstat().st_mode
        require(not (mode & 0o222) and (stat.S_ISREG(mode) or stat.S_ISDIR(mode)),
                f'Release members must be read-only, regular files or directories: {path}')


def stage(args):
    repo = existing(args.repository, directory=True)
    baseline_path = existing(args.baseline_deployment)
    baseline, preserved = check_baseline(baseline_path)
    digest(args.commit, 'Candidate commit', 40)
    git(repo, 'merge-base', '--is-ancestor', baseline['source_head'], args.commit)
    require(args.commit != baseline['source_head'], 'Candidate commit must differ from the baseline')
    sandbox = existing(args.sandbox_sif)
    check_pin(sandbox, args.sandbox_sha256)
    skill = check_skills(args.skill_release, args.skill_release_sha256, active=True)
    output = local_path(args.output)
    existing(output.parent, directory=True)
    protected = [repo, baseline_path.parent, sandbox, skill, *(local_path(row[k]) for row in preserved
                 for k in ('path', 'mount_path'))]
    protected.extend(local_path(baseline[key]) for key in ('runtime_root', 'corpus_root', 'code_staging',
                     'figure_staging', 'model_origin', 'model_config_root', 'model_cache_root') if key in baseline)
    require(not any(overlap(output, item) for item in protected), 'Output overlaps an input or runtime path')
    output.mkdir(mode=0o700)  # exclusive; no overwrites or cleanup of prior releases
    source = output / 'source'
    source.mkdir()
    tree, members = archive_commit(repo, args.commit, output / 'source.tar', source)
    require((source / '01_src/scripts/apptainer_launch.py').is_file(), 'Missing candidate application launcher')
    members.pop(SOURCE_MANIFEST, None)
    (source / SOURCE_MANIFEST).unlink(missing_ok=True)
    write_object(source / SOURCE_MANIFEST, source_overlay(args.commit, tree, baseline['source_head'], members))
    write_object(output / 'source-sha256.json', {'commit': args.commit, 'files': hashes(inventory(source))})
    config = release_config(baseline, preserved, output, args.commit, args.sandbox_sha256, args.skill_release_sha256)
    write_object(output / 'deployment.json', config)
    (output / 'launch.py').write_text(WRAPPER, encoding='utf-8')
    (output / 'launch.py').chmod(0o700)
    (output / 'images').mkdir()
    for label in IMAGES:
        origin = sandbox if label == 'sandbox' else existing(baseline[f'{label}_sif'])
        shutil.copyfile(origin, config[f'{label}_sif'])
    shutil.copytree(skill, config['skill_release'])
    write_object(output / METADATA, {
        'schema': SCHEMA, 'source_commit': args.commit, 'source_tree': tree,
        'baseline': {'deployment': str(baseline_path), 'sha256': sha(baseline_path),
                     'launcher_sha256': sha(baseline_path.parent / 'launch.py'),
                     'source_inventory_sha256': sha(baseline_path.parent / 'source-sha256.json')},
        'artifacts': {name: sha(output / name) for name in RELEASE_FILES},
        'images': {label: config[f'{label}_sha256'] for label in IMAGES},
        'active_skill_sha256': args.skill_release_sha256,
    })
    seal(output)
    return verify(output)


def verify(output, repository=None, manifest_pin=None):
    output = existing(output, directory=True)
    check_seal(output)
    require({entry.name for entry in output.iterdir()} == {*RELEASE_FILES, METADATA,
            'source', 'images', 'skills-release'}, 'Unexpected release tree entry')
    require({entry.name for entry in (output / 'images').iterdir()} == set(IMAGES.values()),
            'Unexpected release image entry')
    meta_path = existing(output / METADATA)
    if manifest_pin is not None:
        check_pin(meta_path, manifest_pin)
    meta = read_object(meta_path)
    require(meta['schema'] == SCHEMA and set(meta['artifacts']) == set(RELEASE_FILES), 'Invalid release manifest')
    commit = digest(meta['source_commit'], 'Candidate commit', 40)
    tree = digest(meta['source_tree'], 'Git tree', 40)
    for name in RELEASE_FILES:
        check_pin(output / name, meta['artifacts'][name])
    require((output / 'launch.py').read_text(encoding='utf-8') == WRAPPER, 'Unexpected release launcher')
    baseline_path = existing(meta['baseline']['deployment'])
    check_pin(baseline_path, meta['baseline']['sha256'])
    for name, key in (('launch.py', 'launcher_sha256'), ('source-sha256.json', 'source_inventory_sha256')):
        check_pin(baseline_path.parent / name, meta['baseline'][key])
    baseline = read_object(baseline_path)
    preserved = history(baseline)
    config = read_object(output / 'deployment.json')
    require(set(meta['images']) == set(IMAGES) and
            config == release_config(baseline, preserved, output, commit,
                                     meta['images']['sandbox'], meta['active_skill_sha256']),
            'Deployment does not match pinned paths and inherited runtime contract')
    for label in IMAGES:
        require(meta['images'][label] == config[f'{label}_sha256'], f'{label} SIF manifest mismatch')
        check_pin(config[f'{label}_sif'], meta['images'][label])
    check_skills(config['skill_release'], meta['active_skill_sha256'], active=True)
    if repository is not None:
        repo = existing(repository, directory=True)
        with tempfile.TemporaryDirectory(prefix='8043-archive-check-') as temp:
            rebuilt = Path(temp) / 'source.tar'
            rebuilt_tree, _ = archive_commit(repo, commit, rebuilt)
            require(rebuilt_tree == tree and sha(rebuilt) == meta['artifacts']['source.tar'],
                    'Whole Git archive is not reproducible')
    members = archive_members(output / 'source.tar', commit)
    members.pop(SOURCE_MANIFEST, None)
    source = existing(output / 'source', directory=True)
    actual = inventory(source)
    require(read_object(output / 'source-sha256.json') == {'commit': commit, 'files': hashes(actual)},
            'Staged source inventory mismatch')
    actual.pop(SOURCE_MANIFEST)
    require(members == actual and read_object(source / SOURCE_MANIFEST) ==
            source_overlay(commit, tree, baseline['source_head'], members),
            'Staged source differs from the exact Git archive or source overlay')
    return {'release': str(output), 'source_commit': commit, 'source_archive_sha256': meta['artifacts']['source.tar'],
            'manifest_sha256': sha(meta_path), 'source_files': len(members) + 1,
            'images': meta['images'], 'skill_release_sha256': meta['active_skill_sha256'],
            'historical_skill_releases': len(preserved)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('stage', help='Stage a new release directory, then verify it')
    for arg in ('repository', 'commit', 'baseline-deployment', 'sandbox-sif', 'sandbox-sha256',
                'skill-release', 'skill-release-sha256', 'output'):
        create.add_argument(f'--{arg}', required=True)
    check = commands.add_parser('verify', help='Verify a staged release without starting it')
    check.add_argument('--release', required=True)
    check.add_argument('--repository', help='Rebuild and compare the entire committed archive')
    check.add_argument('--manifest-sha256', help='Compare a separately recorded release manifest hash')
    old = commands.add_parser('baseline-check', help='Verify a legacy rollback release read-only')
    old.add_argument('--baseline-deployment', required=True)
    arc = commands.add_parser('archive-check', help='Verify public Git archive inventory without private artifacts')
    arc.add_argument('--repository', required=True)
    arc.add_argument('--commit', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'stage':
            result = stage(args)
        elif args.command == 'verify':
            result = verify(args.release, args.repository, args.manifest_sha256)
        elif args.command == 'baseline-check':
            config, entries = check_baseline(args.baseline_deployment)
            result = {'source_commit': config['source_head'], 'historical_skill_releases': len(entries)}
        else:
            with tempfile.TemporaryDirectory(prefix='8043-archive-check-') as temp:
                output = Path(temp) / 'source.tar'
                tree, files = archive_commit(existing(args.repository, directory=True), args.commit, output)
                result = {'source_commit': args.commit, 'source_tree': tree, 'source_files': len(files),
                          'source_archive_sha256': sha(output)}
    except (ValueError, TypeError, KeyError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'8043 release gate: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
