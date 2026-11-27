"""Host entrypoint for a trusted Web/Worker SIF and a separate offline CPU SIF.

Use the same absolute host paths in the application SIF and in systemd jobs.
No credentials in argv; no service installation, fallback or automatic deletion.
"""
from __future__ import annotations

import argparse
import hashlib
from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import re
import sys


def absolute(value):
    if (not isinstance(value, str) or not value.startswith('/')
            or any(c in value for c in ':,\n\r\x00')
            or any(part in {'.', '..', ''} for part in value.split('/')[1:])):
        raise ValueError('An absolute normalized bind-safe Linux path is required')
    return value


def lan_ingress(config, port):
    ingress = config.get('lan_ingress')
    if ingress is None:
        return [], []
    required = {'ipv4', 'port', 'proxy_token_file', 'allowed_client_cidrs'}
    if not isinstance(ingress, dict) or set(ingress) != required:
        raise ValueError('LAN ingress requires ipv4, port, proxy_token_file and allowed_client_cidrs')
    allowed = ingress['allowed_client_cidrs']
    if not isinstance(allowed, list) or not allowed or not all(isinstance(cidr, str) for cidr in allowed):
        raise ValueError('LAN ingress requires explicit IPv4 client networks')
    cidrs = [IPv4Network(cidr) for cidr in allowed]
    if any(network.prefixlen == 0 for network in cidrs):
        raise ValueError('LAN ingress disallows a global client allowlist')
    if not isinstance(ingress['ipv4'], str):
        raise ValueError('LAN ingress requires an explicit IPv4 address')
    ip = IPv4Address(ingress['ipv4'])
    ingress_port = ingress['port']
    if (ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local
            or type(ingress_port) is not int or not 1024 <= ingress_port <= 65535
            or ingress_port == port or config.get('listen_host', '127.0.0.1') != '127.0.0.1'):
        raise ValueError('LAN ingress requires a dedicated IPv4 port and a loopback Web listener')
    token_file = absolute(ingress['proxy_token_file'])
    return ([(token_file, 'ro')], ['--env', f'CITRUS_LAN_AUTHORITY={ip}:{ingress_port}',
                                  '--env', f'CITRUS_LAN_UPSTREAM_PORT={port}',
                                  '--env', f'CITRUS_LAN_TOKEN_FILE={token_file}',
                                  '--env', 'CITRUS_LAN_CLIENT_CIDRS=' + ' '.join(map(str, cidrs))])


def release_identity(config):
    source = Path(absolute(config['source_root']))
    manifest = (source / 'LINUX-SOURCE-MANIFEST.json').read_bytes()
    # Config contains paths and expected image hashes, never private file contents.
    configuration = json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(hashlib.sha256(manifest).digest() + configuration).hexdigest()


def verified_skill_release(config, binds):
    path = config.get('skill_release')
    expected = config.get('skill_release_sha256')
    if path is None and expected is None:
        return None
    root = Path(absolute(path))
    if (root.resolve(strict=True) != root or not root.is_dir()
            or not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)
            or any(root.is_relative_to(writable) or writable.is_relative_to(root)
                   for item, mode in binds if mode == 'rw' for writable in (Path(item).resolve(),))):
        raise ValueError('Skill release must be pinned and outside writable deployment binds')
    inventory_path = root / 'inventory.json'
    if inventory_path.is_symlink():
        raise ValueError('Skill release inventory must be a regular file')
    raw = inventory_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Skill release inventory SHA-256 mismatch')
    inventory = json.loads(raw)
    actual = {}
    for member in root.rglob('*'):
        if member.is_symlink() or (not member.is_dir() and not member.is_file()):
            raise ValueError('Invalid Skill release member')
        if member.is_file() and member != inventory_path:
            actual[member.relative_to(root).as_posix()] = hashlib.sha256(member.read_bytes()).hexdigest()
    if inventory != actual or not (root / 'skills').is_dir():
        raise ValueError('Skill release content mismatch')
    return root


def isolated_corpus(config, binds):
    options = config.get('isolated_corpus')
    if options is None:
        return [], []
    allowed = {'root', 'growth', 'read', 'embedding_cache', 'read_generation', 'durable_intake'}
    if (not isinstance(options, dict) or not options.keys() <= allowed
            or config.get('shared_literature_single_web')):
        raise ValueError('Invalid or conflicting isolated corpus deployment')
    growth, read = options.get('growth', False), options.get('read', False)
    durable = options.get('durable_intake', False)
    if any(type(switch) is not bool for switch in (growth, read, durable)):
        raise ValueError('Isolated corpus switches must be boolean')
    if durable and not growth:
        raise ValueError('Durable intake requires isolated corpus growth')
    root = Path(absolute(options.get('root')))
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError('Isolated corpus needs a real, dedicated directory')
    occupied = [Path(path).resolve() for path, _mode in binds]
    if any(root.is_relative_to(path) or path.is_relative_to(root) for path in occupied):
        raise ValueError('Isolated corpus root overlaps a deployment bind')
    if config.get('external_modules'):
        growth = read = durable = False
    mounts = [(str(root), 'ro' if config.get('external_modules') else 'rw')]
    variables = ['CITRUS_CORPUS_MODE=isolated', 'CITRUS_ISOLATED_CORPUS_ROOT=' + str(root),
                 'CITRUS_CORPUS_FETCH_GROWTH=' + str(int(growth)), 'CITRUS_CORPUS_READ=' + str(int(read)),
                 'CITRUS_CORPUS_DURABLE_INTAKE=' + str(int(durable))]
    cache = options.get('embedding_cache')
    if read or cache is not None:
        cache = Path(absolute(cache))
        if (cache.resolve(strict=True) != cache or not cache.is_dir()
                or cache.is_relative_to(root) or root.is_relative_to(cache)):
            raise ValueError('Isolated corpus needs a separate model cache')
        mounts.append((str(cache), 'ro'))
        variables.append('CITRUS_ISOLATED_CORPUS_EMBEDDING_CACHE=' + str(cache))
    generation = options.get('read_generation')
    if generation is not None:
        if not read or not isinstance(generation, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', generation):
            raise ValueError('Invalid isolated corpus experiment generation')
        variables.append('CITRUS_CORPUS_READ_GENERATION=' + generation)
    return mounts, [arg for value in variables for arg in ('--env', value)]


def external_module_bindings(config, binds):
    values = config.get('external_modules')
    if values is None:
        return [], [], []
    if not isinstance(values, dict) or set(values) != {'selfcorpus', 'workbench'}:
        raise ValueError('Invalid external modules')
    corpus, workbench = values['selfcorpus'], values['workbench']
    keys = {'client_path', 'client_sha256', 'secret_file'}
    if (not isinstance(corpus, dict) or not keys <= set(corpus) <= keys | {'auto_intake'}
            or type(corpus.get('auto_intake', False)) is not bool
            or not isinstance(workbench, dict) or set(workbench) != {'secret_file'}):
        raise ValueError('Invalid external module configuration')
    client = Path(absolute(corpus['client_path']))
    if not client.is_dir() or client.resolve(strict=True) != client or client.stat().st_mode & 0o222:
        raise ValueError('Selfcorpus client package must be immutable')
    pins = corpus['client_sha256']
    required = {'corpus/__init__.py', 'corpus/auth.py'}
    if not isinstance(pins, dict) or set(pins) != required:
        raise ValueError('Selfcorpus client requires exact file pins')
    files = set()
    for path in client.rglob('*'):
        if path.is_symlink() or path.stat().st_mode & 0o222:
            raise ValueError('Invalid Selfcorpus client package member')
        if path.is_dir():
            continue
        rel = path.relative_to(client).as_posix()
        if rel not in pins or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != pins[rel]:
            raise ValueError('Selfcorpus package file digest mismatch')
        files.add(rel)
    if files != required:
        raise ValueError('Incomplete Selfcorpus client package')
    secrets = []
    for value in (corpus['secret_file'], workbench['secret_file']):
        path = Path(absolute(value))
        if (path.resolve(strict=True) != path or not path.is_file() or path.stat().st_mode & 0o077
                or not 32 <= path.stat().st_size <= 4096):
            raise ValueError('Module secret must be a private regular file')
        secrets.append(path)
    mounts = [client, *secrets]
    seen = [Path(path).resolve() for path, _ in binds]
    for path in mounts:
        if any(path.is_relative_to(other) or other.is_relative_to(path) for other in seen):
            raise ValueError('External module bind overlaps another deployment bind')
        seen.append(path)
    env = ['CITRUS_SELFCORPUS_RETRIEVAL=1', 'CITRUS_SELFCORPUS_URL=http://127.0.0.1:8061',
           f'CITRUS_SELFCORPUS_SECRET_FILE={secrets[0]}', 'CITRUS_WORKBENCH_BRIDGE_ENABLED=1',
           f'WORKBENCH_HOST_KEY_FILE={secrets[1]}', 'CITRUS_WORKBENCH_UPSTREAM=http://127.0.0.1:8062',
           'CITRUS_WORKBENCH_PUBLIC_URL=/workbench/workbench/',
           'CITRUS_WORKBENCH_LAUNCH_ENDPOINT=/workbench/api/v1/launch']
    if corpus.get('auto_intake'):
        env.append('CITRUS_SELFCORPUS_AUTO_INTAKE=1')
    return [(str(path), 'ro') for path in mounts], [arg for value in env for arg in ('--env', value)], [str(client)]


def command(config, role):
    if role not in {'web', 'worker', 'prepare'}:
        raise ValueError('Unknown deployment role')
    runtime = absolute(config['runtime_root'])
    source = absolute(config['source_root'])
    app = absolute(config['app_sif'])
    sandbox = absolute(config['sandbox_sif'])
    host_python = absolute(config['host_python'])
    apptainer = absolute(config.get('apptainer_path', '/usr/bin/apptainer'))
    staging = absolute(config['code_staging'] if role == 'worker' else config['figure_staging'])
    namespace = config.get('namespace_sif', '')
    namespace_digest = config.get('namespace_sha256', '')
    if bool(namespace) != bool(namespace_digest):
        raise ValueError('Namespace SIF path and SHA-256 must be supplied together')
    if namespace:
        namespace = absolute(namespace)
    for digest in (config['app_sha256'], config['sandbox_sha256'], *([namespace_digest] if namespace else [])):
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('All configured SIF files require SHA-256 pins')
    if any(not path.endswith('.sif') for path in (app, sandbox, *([namespace] if namespace else []))):
        raise ValueError('Local .sif images are required')
    port = config.get('port', 8030)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid service port')
    result = [apptainer, 'exec', '--cleanenv', '--no-home', '--no-mount',
              'hostfs,cwd,home,bind-paths', '--pwd', source]
    # GPU exposure belongs to the trusted Web model runtime only. The ordinary
    # Code sandbox command never inherits this flag or these application binds.
    if role in {'web', 'worker'}:
        result += ['--env', 'CITRUS_WORKER_RELEASE=' + release_identity(config)]
        commit = config.get('source_head')
        if commit is not None:
            if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit):
                raise ValueError('Invalid source commit for deployment')
            result += ['--env', 'CITRUS_SOURCE_COMMIT=' + commit]
    if role == 'worker' and os.getenv('INVOCATION_ID'):
        invocation = os.environ['INVOCATION_ID']
        if not re.fullmatch('[a-f0-9]{32}', invocation) or invocation == '0' * 32:
            raise ValueError('Invalid Worker systemd activation')
        result += ['--env', 'CITRUS_WORKER_ACTIVATION=' + invocation]
    if role != 'worker':
        gpu_device = config.get('gpu_device', 0)
        if type(gpu_device) is not int or not 0 <= gpu_device < 128:
            raise ValueError('Invalid GPU device index')
        result += ['--nv', '--env', 'CUDA_VISIBLE_DEVICES=' + str(gpu_device)]
        # Trusted Web uses the host resolver in the same network namespace.
        # Runtime configuration binds are otherwise disabled; the SIF's file
        # is empty. This never changes the host or the offline Code command.
        resolver = absolute(str(Path('/etc/resolv.conf').resolve(strict=True)))
        result += ['--bind', resolver + ':/etc/resolv.conf:ro']
    binds = [(runtime, 'rw'), (source, 'ro'), (staging, 'rw'),
             (str(Path(sandbox).parent), 'ro'), (apptainer, 'ro'), (host_python, 'ro'),
             (f'/run/user/{os.getuid()}', 'rw')]
    if role == 'worker' and namespace:
        binds += [(namespace, 'ro')]
    if role != 'worker':
        binds += [(absolute(config['corpus_root']), 'ro'),
                  (absolute(config['model_cache_root']), 'rw')]
    if role == 'web':
        shared_enabled = config.get('shared_literature_single_web', False)
        if type(shared_enabled) is not bool:
            raise ValueError('Invalid shared literature deployment mode')
        if shared_enabled:
            data = Path(absolute(config['corpus_root']))
            for private_dir in (data / 'shared_agent_literature', data / '_review_queue'):
                if not private_dir.is_dir() or private_dir.is_symlink():
                    raise ValueError('Provide existing private shared literature and review queue directories')
                binds.append((str(private_dir), 'rw'))
            result += ['--env', 'CITRUS_SHARED_CORPUS=single-web']
        ingress_binds, ingress_env = lan_ingress(config, port)
        binds += ingress_binds
        result += ingress_env
        corpus_binds, corpus_env = isolated_corpus(config, binds)
        binds += corpus_binds
        result += corpus_env
    module_paths = []
    if role == 'web':
        module_binds, module_env, module_paths = external_module_bindings(config, binds)
        binds += module_binds
        result += module_env
    if role == 'web' and config.get('browse_config'):
        # Script is launched with -I; import only from this trusted source snapshot.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.browse.integrity import IntegrityError, load_bundle, trusted_source_file, verify_bundle
        from src.code_execution.egress_policy import normalize_host

        try:
            bundle = load_bundle(absolute(config['browse_config']))
            browse_assets = (bundle.config_path, bundle.image, bundle.packages, bundle.packages_manifest)
            writable = [Path(path).resolve() for path, mode in binds if mode == 'rw']
            if any(asset.is_relative_to(path) or path.is_relative_to(asset)
                   for asset in browse_assets for path in writable):
                raise IntegrityError('Browse assets must be outside writable deployment binds')
            for host in bundle.hosts:
                normalize_host(host)
            for name in ('integrity.py', 'sandbox.py'):
                trusted_source_file(f'{source}/01_src/src/browse/{name}')
            verify_bundle(bundle)
        except (OSError, ValueError) as exc:
            raise ValueError(f'Browse bundle integrity: {exc}') from exc
        binds += [(str(path), 'ro') for path in (*browse_assets, bundle.host_python)]
        result += ['--env', 'CITRUS_BROWSE_CONFIG=' + str(bundle.config_path),
                   '--env', 'CITRUS_BROWSE_CONFIG_SHA256=' + bundle.config_sha256,
                   '--env', 'CITRUS_BROWSE_APPTAINER=1']
    skill_release = verified_skill_release(config, binds)
    external_required = config.get('external_fusion_required', False)
    methods_only = config.get('scientific_methods_only', False)
    if type(external_required) is not bool or type(methods_only) is not bool or (methods_only and not external_required):
        raise ValueError('Invalid scientific Skill policy')
    if methods_only:
        if (skill_release is None or not (skill_release / 'scientific-methods.json').is_file()
                or (skill_release / 'fusion').exists() or (skill_release / 'python').exists()):
            raise ValueError('Pinned method-only release unavailable')
        result += ['--env', 'CITRUS_SCIENTIFIC_METHODS_ONLY=1']
    if external_required:
        if not methods_only and (skill_release is None or not (skill_release / 'fusion/bundle.json').is_file()):
            raise ValueError('Required external Skill bundle unavailable')
        result += ['--env', 'CITRUS_EXTERNAL_FUSION=required']
    # The release source tree is the single authority for both the application
    # and the method packages. Without ``packages/`` on PYTHONPATH the image's
    # site-packages copy shadows a freshly published skill package, so a fixed
    # reference kernel or audit rule never reaches the running task.
    python_path = [*module_paths, f'{source}/packages', f'{source}/01_src']
    if skill_release is not None:
        binds.append((str(skill_release), 'ro'))
        result += ['--env', 'CITRUS_SKILL_RELEASE=' + str(skill_release)]
        if (skill_release / 'python').is_dir():
            python_path.insert(0, f'{skill_release}/python')
        if methods_only and role == 'web' and (skill_release / 'host-python').is_dir():
            python_path.insert(0, f'{skill_release}/host-python')
    historical = config.get('historical_skill_releases', [])
    if not isinstance(historical, list) or len(historical) > 64:
        raise ValueError('Invalid historical Skill release registry')
    if role == 'web':
        used = {Path(path) for path, _mode in binds}
        pre_fusion, fusion_history, seen_inventory = [], [], set()
        for item in historical:
            if not isinstance(item, dict) or item.keys() != {'path', 'mount_path', 'sha256'}:
                raise ValueError('Invalid historical Skill release entry')
            prior = verified_skill_release({'skill_release': item['path'],
                                           'skill_release_sha256': item['sha256']}, binds)
            destination = Path(absolute(item['mount_path']))
            if (prior is None or destination == Path('/') or
                    any(destination.is_relative_to(path) or path.is_relative_to(destination) for path in used)):
                raise ValueError('Historical Skill mount overlaps an existing deployment bind')
            used.add(destination)
            result += ['--bind', f'{prior}:{destination}:ro']
            if item['sha256'] in seen_inventory:
                continue  # Preserve the mount for exact path pins, but index identical bytes once.
            seen_inventory.add(item['sha256'])
            if (prior / 'fusion/bundle.json').is_file():
                fusion_history.append(str(destination))
            elif not (prior / 'fusion').exists():
                pre_fusion.append(item['sha256'])
        if external_required:
            result += ['--env', 'CITRUS_FUSION_REPORT_RELEASES=' + os.pathsep.join(fusion_history),
                       '--env', 'CITRUS_PRE_FUSION_INVENTORY_SHA256=' + ' '.join(pre_fusion)]
    result += ['--env', 'PYTHONPATH=' + ':'.join(python_path)]
    for path, mode in dict(binds).items():
        result += ['--bind', f'{path}:{path}:{mode}']
    result += [app, '/usr/local/bin/python']
    if role == 'worker':
        result += [source + '/01_src/scripts/code_worker.py', '--runtime-root', runtime,
            '--staging-root', staging, '--backend', 'apptainer', '--sif-path', sandbox,
            '--image-id', 'sha256:' + config['sandbox_sha256'], '--apptainer-path', apptainer,
            '--host-python', host_python, '--service-path',
            source + '/01_src/src/code_execution/apptainer_service.py', '--development-oci']
        if namespace:
            result += ['--namespace-sif-path', namespace, '--namespace-sif-sha256', namespace_digest]
    else:
        result += [source + '/01_src/scripts/configured_local.py', '--runtime-root', runtime,
            '--model-config-root', absolute(config['model_config_root']),
            '--corpus-root', absolute(config['corpus_root']),
            '--model-cache-root', absolute(config['model_cache_root']), '--worker-staging', staging,
            '--backend', 'apptainer', '--image', sandbox, '--image-sha256', config['sandbox_sha256'],
            '--apptainer-path', apptainer, '--host-python', host_python,
            '--host', config.get('listen_host', '127.0.0.1'), '--port', str(port), '--require-full']
        if role == 'prepare':
            if config.get('existing_runtime'):
                raise ValueError('Do not prepare/reinitialize an existing runtime')
            result += ['--prepare-only']
        else:
            result += ['--offline-code']
            if config.get('existing_runtime'):
                result += ['--existing-runtime']
        if config.get('reuse_web_search_settings') is False:
            result += ['--without-source-web-search']
        # Existing configuration can live outside the writable runtime during
        # isolated acceptance. The Web alone may read its authorized key source.
        model_root = absolute(config['model_config_root'])
        if not Path(model_root).is_relative_to(runtime):
            result[result.index(app):result.index(app)] = ['--bind', f'{model_root}:{model_root}:ro']
    return result


def verify_image(path, expected):
    p = Path(path)
    if p.suffix != '.sif' or any(q.is_symlink() for q in (p, *p.parents)):
        raise ValueError('SIF must use a real immutable absolute path')
    before = p.stat()
    import stat
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o022:
        raise ValueError('SIF must be a nonlinked regular file without group/world write access')
    digest = hashlib.sha256()
    with p.open('rb') as stream:
        if not os.path.samestat(before, os.fstat(stream.fileno())):
            raise ValueError('SIF changed')
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (digest.hexdigest() != expected or not os.path.samestat(after, p.stat())
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError('SIF pin mismatch or image changed')


def worker_dependency_inventory(config):
    # The App SIF may have an older libc than the host Apptainer binary.
    # Run this offline probe before entering the App SIF.
    source = Path(absolute(config['source_root']))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source / '01_src'))
    try:
        from src.code_execution.apptainer import ApptainerConfig, OfflineApptainerBackend
        backend = OfflineApptainerBackend(ApptainerConfig(
            image_id='sha256:' + config['sandbox_sha256'],
            staging_root=absolute(config['code_staging']),
            sif_path=absolute(config['sandbox_sif']),
            runtime=absolute(config.get('apptainer_path', '/usr/bin/apptainer')),
            host_python=absolute(config['host_python']),
        ))
        return backend.base_dependency_versions()
    except (OSError, RuntimeError, ValueError):
        return {}
    finally:
        sys.path.pop(0)


def _worker_activation(unit):
    import subprocess
    if not re.fullmatch(r'citrusscholar-worker@[a-zA-Z0-9_.-]+\.service', unit):
        raise ValueError('Invalid Worker unit name')
    try:
        receipt = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit,
            '--property=ActiveState', '--property=InvocationID'], capture_output=True,
            text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = dict(line.split('=', 1) for line in receipt.stdout.splitlines() if '=' in line)
    value = fields.get('InvocationID', '')
    if fields.get('ActiveState') == 'active' and re.fullmatch('[a-f0-9]{32}', value) and value != '0' * 32:
        return value
    return None


def wait_for_worker(config, *, worker_unit=None, timeout=90):
    """Wait for a fresh heartbeat from this release and the active Worker unit."""
    import time

    source = Path(absolute(config['source_root']))
    runtime = Path(absolute(config['runtime_root']))
    sys.path.insert(0, str(source / '01_src'))
    try:
        from src.code_execution.presence import worker_available
    finally:
        sys.path.pop(0)
    db = runtime / 'state' / 'code_executions.sqlite3'
    release = release_identity(config)
    since = time.time()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        activation = _worker_activation(worker_unit) if worker_unit else None
        if ((not worker_unit or activation) and worker_available(db, since=since, release=release, activation=activation)
                and (not worker_unit or _worker_activation(worker_unit) == activation)):
            return
        time.sleep(0.5)
    raise SystemExit('No new offline Code Worker heartbeat for the configured runtime')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--role', choices=('web', 'worker', 'prepare', 'wait-worker'), required=True)
    parser.add_argument('--print-command', action='store_true')
    parser.add_argument('--worker-unit', help='Expected systemd Worker unit for the startup gate')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.getuid() == 0:
        parser.error('Run as the unprivileged Linux service user')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.role == 'wait-worker':
        if args.print_command:
            parser.error('wait-worker does not launch an Apptainer command')
        wait_for_worker(config, worker_unit=args.worker_unit)
        return
    if args.worker_unit:
        parser.error('--worker-unit applies only to wait-worker')
    argv = command(config, args.role)
    if args.print_command:
        print(json.dumps(argv))
        return
    prefixes = ['app', 'sandbox']
    if args.role == 'worker' and config.get('namespace_sif'):
        prefixes.append('namespace')
    for prefix in prefixes:
        verify_image(config[prefix + '_sif'], config[prefix + '_sha256'])
    threads = config.get('cpu_threads', 8)
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError('Invalid CPU thread budget')
    allowed = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, allowed[:threads])
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8',
           'GOMAXPROCS': str(threads), 'GOMEMLIMIT': '256MiB',
           'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
           'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'}
    if args.role == 'worker':
        argv += ['--base-dependency-inventory', json.dumps(worker_dependency_inventory(config))]
    os.execve(argv[0], argv, env)


if __name__ == '__main__':
    main()
