"""Trusted Nature R adapter package and verified runtime identity."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path

from src.agent.research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

SCRIPT = '/workspace/nature_r_template.py'
TEMPLATE = 'marginal'
R_PACKAGE_VERSIONS = dict(ggplot2='4.0.4', patchwork='1.3.2', svglite='2.2.2', ragg='1.5.2', jsonlite='2.0.0')
R_VERSION = '4.4.3'
RUNTIME_LOCK_SHA256 = '56cb8a99a3279e89a0d9fb9089363c49485b1ecac30936db264c4acbface4607'
# A new SIF build gets its own reviewed digest. Shared ordinary Code and
# Figure image paths still go through the existing launcher and worker guards.
R_IMAGE_SHA256 = 'f576f83f98f6c18c972babb866e0065ca5d694cb6c9b5fe47dacd4b78dda8f62'
_UPSTREAM = 'skills/nature-figure/scripts/'
_HELPERS = {
    'panel_alignment.R': 'aeb2db8355e2026e2f2640d25bf18edfa57fb0165ec2c7811f97bb75ba53d26b',
    'audit_panel_alignment.py': '4ed2d6afe9db45aaf6ca26aeb264cfc251ae92b49ee9fbf117d926e1956acf56',
    'audit_pdf_text.py': '6d570cc8efe91825d6da156ac2599b1ef8fb61ce7074ddc9d1646aeb7a02d9d4',
    'audit_figure_collisions.py': '4ee745c540355835944f33594833a6a87c44fca8d2232188d6c50a858bc33dcb',
    'validate_figure.py': 'efe65ff2ed243a7c09819bf3093a7cc167f6e829fe31a6f3e6fa0d9c99d72d31',
}


def digest(raw):
    return sha256(raw).hexdigest()


def available():
    return (os.getenv('FIGURES_SANDBOX_BACKEND') == 'apptainer'
            and os.getenv('FIGURES_SANDBOX_IMAGE_SHA256') == R_IMAGE_SHA256)


def require_template(spec):
    if spec.plot.template != TEMPLATE:
        raise LookupError('nature_plot_r_template_unavailable')


def sealed_adapter(release, pin):
    """Bind exact active upstream helpers and trusted application sources as one snapshot."""
    raw_inventory = _bytes(_path(release, 'inventory.json'), _MAX_RESOURCE_BYTES)
    inventory = json.loads(raw_inventory)
    if type(inventory) is not dict or digest(raw_inventory) != pin.release_sha256:
        raise ValueError('nature_plot_r_release_pin_mismatch')
    folder = Path(__file__).resolve().parent
    payload = {'nature_marginal.R': (folder / 'nature_skill_marginal.R').read_bytes()}
    for name, expected in _HELPERS.items():
        path = _UPSTREAM + name
        raw = _bytes(_path(release, path), 64 << 10)
        if inventory.get(path) != expected or digest(raw) != expected:
            raise ValueError('nature_plot_r_helper_pin_mismatch')
        payload[name] = raw
    bootstrap = (folder / 'nature_skill_r_bootstrap.py').read_bytes()
    contents = bootstrap + (
        f'\n_PAYLOAD = {payload!r}\n_RUNTIME_LOCK_SHA256 = {RUNTIME_LOCK_SHA256!r}\nmain()\n'
    ).encode('ascii')
    if len(contents) > 200000:
        raise ValueError('nature_plot_r_snapshot_too_large')
    return replace(pin, content=contents, sha256=digest(contents))


def arguments(spec, stem):
    require_template(spec)
    return (json.dumps({'stem': stem, 'title': spec.title, 'plot': spec.plot.model_dump()},
                       ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')),)


def figure_qa(qa, pdf_sha256):
    runtime = qa.get('runtime')
    expected = {'r': R_VERSION, 'packages': R_PACKAGE_VERSIONS, 'lock_sha256': RUNTIME_LOCK_SHA256}
    geometry = qa.get('figure_geometry')
    text = geometry.get('pdf_text') if type(geometry) is dict else None
    collisions = geometry.get('pdf_collisions') if type(geometry) is dict else None
    if (runtime != expected or type(geometry) is not dict or geometry.get('pdf_sha256') != pdf_sha256
            or not pdf_sha256 or geometry.get('alignment') != 'PASS'
            or type(text) is not dict or text.get('below_minimum') != 0
            or type(collisions) is not dict or collisions.get('verdict') not in {'PASS', 'REVIEW REQUIRED'}
            or geometry.get('visual_review') != 'not_run'):
        raise ValueError('nature_plot_r_runtime_or_geometry_mismatch')
    return {'runtime': runtime, 'figure_geometry': geometry}


def verify_worker_environment(receipt):
    environment = receipt.get('verified_environment')
    if (type(environment) is not dict or environment.get('image_id') != 'sha256:' + R_IMAGE_SHA256
            or environment.get('environment_id') != 'base' or environment.get('revision') != 1):
        raise PermissionError('nature_plot_r_image_receipt_mismatch')
