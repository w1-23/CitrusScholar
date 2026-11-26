#!/usr/bin/env python3
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
