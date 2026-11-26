"""Validate pinned 8043 activation records without systemd or service calls."""

import hashlib
import json
import re
import sys
from pathlib import Path

def require(ok, message):
    if not ok:
        raise SystemExit(message)

def unique(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, 'Duplicate JSON field in cutover input')
        obj[key] = value
    return obj

def read(path):
    require(path.is_file() and path.resolve(strict=True) == path, f'Noncanonical file: {path}')
    return path.read_bytes()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def digest(val, size=64):
    return isinstance(val, str) and re.fullmatch(f'[0-9a-f]{{{size}}}', val)

def checked(path, expected):
    require(digest(expected), f'Missing full digest for {path.name}')
    data = read(path)
    require(sha(data) == expected, f'Digest mismatch: {path}')
    return data

def parse(data):
    value = json.loads(data, object_pairs_hook=unique)
    require(isinstance(value, dict), 'Expected JSON object in cutover input')
    return value

def stage_config(data, unit, primary_units, old_path, old_cfg, new, candidate):
    source = data.decode('utf-8')
    require(re.findall(r'(?m)^WorkingDirectory=([^\r\n]+)$', source) == [old_path]
            and old_path + '/' in source, f'Unexpected unit WorkingDirectory/command: {unit}')
    if unit not in primary_units:
        old_image = old_cfg['app_sif']
        require(source.count(old_image) == 1, f'Unexpected corpus service image reference: {unit}')
        source = source.replace(old_image, candidate['app_sif'])
    source = re.sub(re.escape(old_path) + r'(?=$|[/\s"\x27])', lambda _: str(new), source, flags=re.M)
    require(re.findall(r'(?m)^WorkingDirectory=([^\r\n]+)$', source) == [str(new)]
            and old_path not in source, f'Unconverted unit reference: {unit}')
    return source.encode('utf-8')


def main():
    operation, root, new, record, baseline, pin, unitdir, *units = sys.argv[1:]
    root, new, record, baseline, unitdir = map(Path, (root, new, record, baseline, unitdir))

    def canonical_release(cwd):
        require(cwd.parent == root / 'releases' and cwd.is_dir() and cwd.resolve(strict=True) == cwd,
                f'Invalid release directory: {cwd}')

    require(baseline.is_relative_to(root / 'rollbacks'), 'Baseline must be under the rollbacks root')
    base_bytes = checked(baseline, pin)
    base = parse(base_bytes)
    require(type(base['schema']) is int and base['schema'] == 1 and set(base['units']) == set(units),
            'Invalid baseline schema or unit set')
    require(base['watch_state'] in ('active', 'inactive'), 'Invalid baseline watchdog state')
    helper = Path(base['helper']['path'])
    require(helper.is_relative_to(root / 'rollbacks') and helper.name == 'helper.sh', 'Invalid helper path')
    checked(helper, base['helper']['sha256'])
    old_configs = {}
    for unit in units:
        item = base['units'][unit]
        cwd = Path(item['cwd'])
        canonical_release(cwd)
        require(cwd != new and item['active_state'] in ('active', 'inactive') and digest(item['config_sha256']),
                'Invalid old unit pin, state or release identity')
        manifest = parse(checked(cwd / 'release.json', item['release_manifest_sha256']))
        require(digest(item['source_head'], 40) and manifest['source_commit'] == item['source_head'],
                f'Baseline source commit mismatch: {unit}')
        artifacts = manifest['artifacts']
        cfg = parse(checked(cwd / 'deployment.json', artifacts['deployment.json']))
        checked(cwd / 'launch.py', artifacts['launch.py'])
        require(cfg['source_head'] == item['source_head'], f'Baseline deployment commit mismatch: {unit}')
        old_configs[unit] = cfg

    web, worker = units[:2]
    old = Path(base['units'][web]['cwd'])
    old_cfg = old_configs[web]
    require(base['units'][worker]['cwd'] == str(old) and all(base['units'][u]['active_state'] == 'active' for u in (web, worker)),
            'Baseline Web and Worker must be active on the same release')
    require(old_cfg['listen_host'] == '127.0.0.1' and old_cfg['port'] == 8043, 'Unexpected baseline Web address')
    runtime = Path(old_cfg['runtime_root'])
    index = Path(old_cfg['isolated_corpus']['root']) / 'active_index.json'
    require(runtime.is_dir() and runtime.resolve(strict=True) == runtime and runtime.is_relative_to(root)
            and index.is_relative_to(root), 'Unexpected runtime/index paths in baseline')
    require(index.parent.is_dir() and index.parent.resolve(strict=True) == index.parent, 'Invalid corpus index directory')

    if operation == 'context':
        print(old, runtime, index, helper, base['watch_state'], sep='\n')
        for unit in units:
            print(base['units'][unit]['cwd'], base['units'][unit]['active_state'], sep='\n')
        raise SystemExit(0)

    if operation in ('capture', 'candidate'):
        canonical_release(new)
        new_deployment = read(new / 'deployment.json')
        candidate = parse(new_deployment)
        manifest = parse(read(new / 'release.json'))
        require(manifest['baseline']['deployment'] == str(old / 'deployment.json')
                and manifest['baseline']['sha256'] == sha(read(old / 'deployment.json'))
                and manifest['artifacts']['deployment.json'] == sha(new_deployment)
                and digest(candidate['source_head'], 40) and manifest['source_commit'] == candidate['source_head'],
                'Candidate was not staged against the exact validated Web baseline')
        require(candidate['port'] == 8043 and candidate['listen_host'] == '127.0.0.1'
                and candidate['external_fusion_required'] is True, 'Invalid candidate bind/admission configuration')
        for key in ('runtime_root','corpus_root','code_staging','figure_staging','isolated_corpus',
                    'gpu_device','model_origin','model_config_root'):
            require(candidate[key] == old_cfg[key], f'Unexpected candidate runtime change: {key}')
        history = candidate['historical_skill_releases']
        require(all(item in history for item in old_cfg['historical_skill_releases']) and
                any(item['mount_path'] == old_cfg['skill_release'] and item['sha256'] == old_cfg['skill_release_sha256']
                    for item in history), 'Missing or changed historical Skill mounts')

    if operation == 'candidate':
        receipt = parse(read(record / 'isolated-gate.json'))
        identity = parse(read(record / 'identity.json'))
        require(identity['new_deployment_sha256'] == sha(new_deployment)
                and identity['new_source_head'] == candidate['source_head'], 'Candidate changed since prepare')
        manifest_pin = receipt['manifest_sha256']
        require(digest(manifest_pin) and manifest_pin == sha(read(new / 'release.json'))
                and receipt['deployment_sha256'] == sha(new_deployment)
                and receipt['source_head'] == candidate['source_head'], 'Acceptance receipt identity mismatch')
        require(type(receipt['isolated_port']) is int and 1 <= receipt['isolated_port'] <= 65535
                and receipt['isolated_port'] not in (8043, 8145)
                and Path(receipt['isolated_runtime_root']).resolve() != runtime, 'Acceptance was not isolated')
        for key in ('app_sha256','sandbox_sha256','namespace_sha256','skill_release_sha256'):
            require(receipt[key] == candidate[key], f'Acceptance pin mismatch: {key}')
        for key in ('pinned_worker','app_readiness','browser','audit_delivery','r_artifacts','full_regression','source_architecture'):
            require(receipt['passed'][key] is True, f'Acceptance gate missing: {key}')
        for unit in units:
            item = base['units'][unit]
            old_bytes = checked(record / f'{unit}.conf', item['config_sha256'])
            staged = stage_config(old_bytes, unit, (web, worker), item['cwd'], old_configs[unit], new, candidate)
            require(read(record / 'staged' / f'{unit}.conf') == staged, f'Staged config changed: {unit}')
        print(manifest_pin)
        raise SystemExit(0)

    if operation == 'capture':
        # The shell supplies a temporary sibling for atomic preparation.
        (record / 'staged').mkdir(mode=0o700)
        (record / 'baseline.json').write_bytes(base_bytes)
        (record / 'active_index.before.json').write_bytes(read(index))
        (record / 'watch.state').write_text(base['watch_state'] + '\n', encoding='ascii')
        for unit in units:
            item = base['units'][unit]
            path = unitdir / (f'{unit}.d/8043-worktree.conf' if unit in (web, worker) else unit)
            data = checked(path, item['config_sha256'])
            staged = stage_config(data, unit, (web, worker), item['cwd'], old_configs[unit], new, candidate)
            (record / f'{unit}.conf').write_bytes(data)
            (record / 'staged' / f'{unit}.conf').write_bytes(staged)
        members = ['baseline.json', 'active_index.before.json', 'watch.state',
                   *(f'{unit}.conf' for unit in units), *(f'staged/{unit}.conf' for unit in units)]
        identity = {'schema': 1, 'baseline_sha256': pin, 'new': str(new),
                    'new_source_head': candidate['source_head'], 'new_deployment_sha256': sha(new_deployment),
                    'files': {name: sha(read(record / name)) for name in members}}
        (record / 'identity.json').write_text(json.dumps(identity, sort_keys=True) + '\n', encoding='utf-8')
        (record / 'state').write_text('prepared\n', encoding='ascii')
        raise SystemExit(0)

    require(operation == 'verify', 'Invalid record operation')
    require(record.is_dir() and record.resolve(strict=True) == record, 'Noncanonical rollback record')
    identity = parse(read(record / 'identity.json'))
    require(identity['schema'] == 1 and identity['baseline_sha256'] == pin and identity['new'] == str(new)
            and digest(identity['new_source_head'], 40) and digest(identity['new_deployment_sha256']), 'Record identity did not match')
    proofs = identity['files']
    expected = {'baseline.json', 'active_index.before.json', 'watch.state',
                *(f'{unit}.conf' for unit in units), *(f'staged/{unit}.conf' for unit in units)}
    require(set(proofs) == expected and proofs['baseline.json'] == pin, 'Rollback record inventory changed')
    for name, expected_sha in proofs.items():
        data = checked(record / name, expected_sha)
        if name.endswith('.conf'):
            unit = Path(name).name.removesuffix('.conf')
            if not name.startswith('staged/'):
                require(expected_sha == base['units'][unit]['config_sha256'], f'Old unit pin mismatch: {unit}')
            else:
                require(re.findall(rb'(?m)^WorkingDirectory=([^\r\n]+)$', data) == [str(new).encode()],
                        f'Staged unit binding mismatch: {unit}')
    require(read(record / 'watch.state') == (base['watch_state'] + '\n').encode(), 'Watchdog snapshot changed')


if __name__ == '__main__':
    main()
