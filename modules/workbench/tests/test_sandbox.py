"""Render protocol tests and opt-in trusted Apptainer smoke tests."""

import json
import os
import struct

import pytest

from wb import sandbox


@pytest.mark.parametrize(('language', 'source', 'request_data'), [
    ('shell', b'a', {'format': 'png'}),
    ('r', b'', {'format': 'png'}),
    ('r', b'a', {'format': '../png'}),
    ('r', b'a', {'format': []}),
    ('r', b'a', {'format': 'png', 'invalid': float('nan')}),
])
def test_invalid_requests_never_launch(monkeypatch, language, source, request_data):
    monkeypatch.setattr(sandbox, '_capture', lambda *_: pytest.fail('invalid job launched'))
    with pytest.raises(sandbox.SandboxError):
        sandbox.run(language, source, request_data)


def test_invalid_configuration_never_launches(monkeypatch):
    monkeypatch.setenv('WORKBENCH_IMAGE', 'relative.sif')
    monkeypatch.setattr(sandbox, '_capture', lambda *_: pytest.fail('invalid deployment launched'))
    with pytest.raises(sandbox.SandboxError, match='not configured'):
        sandbox.run('r', b'1', {'format': 'png'})


def test_exact_frame():
    data = json.dumps({'ok': True}).encode()
    assert sandbox._decode(struct.pack('!II', len(data), 3) + data + b'fig') == ({'ok': True}, b'fig')


@pytest.mark.parametrize('frame', [
    b'', struct.pack('!II', 2, 1) + b'{}', struct.pack('!II', 2, 1) + b'{}xy',
    struct.pack('!II', 2, 1) + b'[]x', struct.pack('!II', 3, 1) + b'NaNx',
    struct.pack('!II', 11, 1) + b'{"x":1e309}x',
    struct.pack('!II', sandbox._RESULT_MAX + 1, 1),
])
def test_malformed_output(frame):
    with pytest.raises(sandbox.SandboxError):
        sandbox._decode(frame)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    if not os.environ.get('WORKBENCH_SANDBOX_SMOKE_IMAGE'):
        pytest.skip('set WORKBENCH_SANDBOX_SMOKE_IMAGE to a trusted workbench SIF')
    monkeypatch.setenv('WORKBENCH_IMAGE', os.environ['WORKBENCH_SANDBOX_SMOKE_IMAGE'])
    monkeypatch.setattr(sandbox, '_APP_ROOT', tmp_path)
    monkeypatch.setenv('WORKBENCH_TAVOTTO', str(tmp_path / 'upstream'))
    monkeypatch.setenv('WORKBENCH_SANDBOX_TEST_SECRET', 'private environment marker')
    renderer = tmp_path / 'renderer'
    renderer.mkdir()
    (tmp_path / 'upstream' / 'src').mkdir(parents=True)
    (tmp_path / 'host-only-marker').write_text('private app marker')
    (tmp_path / 'upstream' / 'src' / 'marker').write_text('upstream marker')
    return renderer


_PYTHON = '''\
import errno, json, os, socket, sys
from pathlib import Path
src, request, out = map(Path, sys.argv[1:])
assert src.read_text() == 'source marker'
assert not Path('/app/host-only-marker').exists()
assert 'WORKBENCH_SANDBOX_TEST_SECRET' not in os.environ
assert not Path('/sys/fs/cgroup').exists()
assert Path('/tavotto/src/marker').read_text() == 'upstream marker'
assert os.statvfs('/inputs').f_flag & os.ST_RDONLY
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(('192.0.2.1', 443))
except OSError as exc:
    assert exc.errno == errno.ENETUNREACH
else:
    raise AssertionError('unexpected IPv4 route')
(out / 'figure.png').write_bytes(b'fixture')
(out / 'result.json').write_text(json.dumps({'seen': json.loads(request.read_text())['format']}))
'''


def test_python_smoke(deployment):
    (deployment / 'python_render.py').write_text(_PYTHON)
    assert sandbox.run('python', b'source marker', {'format': 'png'}) == ({'seen': 'png'}, b'fixture')


def test_r_smoke(deployment):
    (deployment / 'r_render.R').write_text('''\
a <- commandArgs(trailingOnly=TRUE)
stopifnot(basename(a[1]) == 'source.R', readLines(a[1]) == 'source marker')
stopifnot(!file.exists('/tavotto/src/marker'))
writeLines('{"seen":"pdf"}', file.path(a[3], 'result.json'))
writeBin(charToRaw('fixture'), file.path(a[3], 'figure.pdf'))
''')
    assert sandbox.run('r', b'source marker\n', {'format': 'pdf'}) == ({'seen': 'pdf'}, b'fixture')


def test_static_smoke(deployment):
    (deployment / 'static_render.py').write_text('''\
from pathlib import Path
import sys
src, request, out = map(Path, sys.argv[1:])
assert src.name == 'source.asset'
assert not Path('/tavotto/src/marker').exists()
(out / 'figure.svg').write_bytes(src.read_bytes())
(out / 'result.json').write_bytes(b'{}')
''')
    assert sandbox.run('static', b'fixture', {'format': 'svg'}) == ({}, b'fixture')


@pytest.mark.parametrize('write', [
    "(out / 'figure.png').symlink_to('/inputs/source.py')",
    "os.mkfifo(out / 'figure.png')",
    "with (out / 'figure.png').open('wb') as stream: stream.truncate(26 * 1024 * 1024)",
    "(out / 'figure.png').write_bytes(b''); print('private renderer diagnostic')",
])
def test_rejects_invalid_collected_file(deployment, write):
    (deployment / 'python_render.py').write_text('''\
import os, sys
from pathlib import Path
out = Path(sys.argv[-1])
(out / 'result.json').write_text('{}')
''' + write + '\n')
    with pytest.raises(sandbox.SandboxError, match='Sandbox or renderer failed'):
        sandbox.run('python', b'source marker', {'format': 'png'})


def test_timeout(deployment, monkeypatch):
    monkeypatch.setattr(sandbox, '_RUNTIME', 2)
    (deployment / 'python_render.py').write_text('import time\ntime.sleep(60)\n')
    with pytest.raises(sandbox.SandboxError):
        sandbox.run('python', b'source marker', {'format': 'png'})
