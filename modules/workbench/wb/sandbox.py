"""Run workbench renderers behind local Linux resource and namespace limits.

Deployment owns the trusted Apptainer SIF, renderer tree, Tavotto source tree,
Apptainer installation, and systemd user manager. None are supplied by a job.
"""

import json
import math
import os
from pathlib import Path
import selectors
import struct
import subprocess
import tempfile
import time
import uuid


class SandboxError(RuntimeError):
    """Invalid job, unavailable isolation, failed render, or invalid output.

    Messages are safe to relay. Renderer stdout/stderr and host paths are not
    included. Callers must still validate the returned document and artifact.
    """


_APP_ROOT = Path(__file__).resolve().parents[1]
_LANGUAGES = {
    "python": ("py", "/usr/local/bin/python3", "/app/renderer/python_render.py"),
    "r": ("R", "/opt/citrus-r/bin/Rscript", "--vanilla", "/app/renderer/r_render.R"),
    "static": ("asset", "/usr/local/bin/python3", "/app/renderer/static_render.py"),
}
_FORMATS = frozenset({"png", "svg", "pdf"})
_SOURCE_MAX = 16 * 1024 * 1024
_REQUEST_MAX = 128 * 1024
_RESULT_MAX = 512 * 1024
_FIGURE_MAX = 24 * 1024 * 1024
_SCRATCH_MAX = 64 * 1024 * 1024
_MEMORY_MAX = 2 * 1024 * 1024 * 1024
_TASKS_MAX = 48
_RUNTIME = 30
_HEADER = struct.Struct("!II")
_FRAME_MAX = _HEADER.size + _RESULT_MAX + _FIGURE_MAX
_PATH = "/usr/bin:/bin"

# This runs after systemd joins the transient service cgroup and before any
# untrusted code. A successful DBus call alone does not prove delegation.
_CGROUP_GATE = '''\
import os, sys
from pathlib import Path
unit, memory, tasks, executable, *args = sys.argv[1:]
entry = Path('/proc/self/cgroup').read_text().strip()
assert entry.startswith('0::/') and entry.endswith('/' + unit)
base = Path('/sys/fs/cgroup' + entry[3:])
assert (base / 'cpu.max').read_text().split() == ['100000', '100000']
assert (base / 'memory.max').read_text().strip() == memory
assert (base / 'memory.swap.max').read_text().strip() == '0'
assert (base / 'pids.max').read_text().strip() == tasks
os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
os.execv(executable, [executable, *args])
'''

# A trusted collector runs inside the isolated namespace. Outputs never bind
# to host disk, and the renderer's standard streams are disconnected.
_COLLECTOR = '''\
import ctypes, os, stat, struct, subprocess, sys
net, pid, ipc, max_scratch, max_result, max_figure, fmt, language, *command = sys.argv[1:]
assert os.getuid() != 0
for name, host in (('net', net), ('pid', pid), ('ipc', ipc)):
    assert os.readlink('/proc/self/ns/' + name) != host
with open('/proc/net/dev') as f:
    assert all(line.split(':', 1)[0].strip() == 'lo' for line in list(f)[2:])
with open('/proc/self/status') as f:
    status = dict(line.split(':', 1) for line in f if ':' in line)
assert int(status['CapEff'], 16) == 0 and status['NoNewPrivs'].strip() == '1'
assert not os.path.exists('/sys/fs/cgroup')
readonly = ['/', '/inputs', '/app/renderer']
if language == 'python':
    readonly.append('/tavotto/src')
assert all(os.statvfs(p).f_flag & os.ST_RDONLY for p in readonly)
volume = os.stat('/outputs').st_dev
for path in ('/outputs', '/tmp', '/var/tmp', '/dev/shm', '/dev', os.environ['HOME']):
    fs = os.statvfs(path)
    assert os.stat(path).st_dev == volume and fs.f_frsize * fs.f_blocks <= int(max_scratch)
    assert not fs.f_flag & os.ST_RDONLY
with open('/proc/self/mountinfo') as f:
    assert any(line.split()[4] == '/outputs' and ' - tmpfs ' in line for line in f)
assert ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
flags = os.O_NOFOLLOW | os.O_CLOEXEC
out = os.open('/outputs', os.O_RDONLY | os.O_DIRECTORY | flags)

def read(name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | flags, dir_fd=out)
    with os.fdopen(fd, 'rb') as fp:
        info = os.fstat(fp.fileno())
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= limit
        data = fp.read(limit + 1)
        assert 0 < len(data) <= limit
        return data

subprocess.run(command, check=True, stdin=subprocess.DEVNULL,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
result = read('result.json', int(max_result))
figure = read('figure.' + fmt, int(max_figure))
sys.stdout.buffer.write(struct.pack('!II', len(result), len(figure)))
sys.stdout.buffer.write(result)
sys.stdout.buffer.write(figure)
'''


def _absolute_path(value, *, file=False):
    path = Path(value)
    if (not path.is_absolute() or any(ch in value for ch in (":", ",", "\n", "\r", "\0"))
            or not (path.is_file() if file else path.is_dir())):
        raise SandboxError("Sandbox deployment is not configured.")
    resolved = path.resolve(strict=True)
    if any(ch in str(resolved) for ch in (":", ",", "\n", "\r", "\0")):
        raise SandboxError("Sandbox deployment is not configured.")
    return resolved


def _container_command(language, fmt, inputs):
    suffix, *renderer = _LANGUAGES[language]
    image_value = os.environ.get("WORKBENCH_IMAGE", "")
    if not image_value.endswith(".sif"):
        raise SandboxError("Sandbox deployment is not configured.")
    image = _absolute_path(image_value, file=True)
    renderers = _absolute_path(str(_APP_ROOT / "renderer"))
    binds = ["--bind", f"{renderers}:/app/renderer:ro", "--bind", f"{inputs}:/inputs:ro"]
    renderer_env = "OPENBLAS_NUM_THREADS=1,OMP_NUM_THREADS=1,MKL_NUM_THREADS=1,TMPDIR=/tmp"
    if language == "python":
        upstream = _absolute_path(os.environ.get("WORKBENCH_TAVOTTO", ""))
        tavotto_src = _absolute_path(str(upstream / "src"))
        binds.extend(("--bind", f"{tavotto_src}:/tavotto/src:ro"))
        renderer_env += ",PYTHONPATH=/tavotto/src,MPLCONFIGDIR=/tmp/matplotlib"
    namespaces = [os.readlink(f"/proc/self/ns/{name}") for name in ("net", "pid", "ipc")]
    return [
        "/usr/bin/apptainer", "exec", "--disable-cache", "--containall", "--cleanenv",
        "--no-mount", "hostfs,bind-paths,cwd,sys", "--net", "--network", "none",
        "--no-privs", "--no-eval", "--scratch", "/outputs,/dev/shm", "--pwd", "/outputs",
        "--env", renderer_env,
        *binds, str(image), "/usr/local/bin/python3", "-I", "-c", _COLLECTOR,
        *namespaces, str(_SCRATCH_MAX), str(_RESULT_MAX), str(_FIGURE_MAX), fmt, language,
        *renderer, f"/inputs/source.{suffix}", "/inputs/request.json", "/outputs",
    ]


def _systemd_command(unit, container_command):
    return [
        "/usr/bin/systemd-run", "--user", "--wait", "--pipe", "--quiet", "--collect",
        "--service-type=exec", f"--unit={unit}", "--working-directory=/",
        "-p", f"RuntimeMaxSec={_RUNTIME}s", "-p", "TimeoutStopSec=2s",
        "-p", "CPUQuota=100%", "-p", f"MemoryMax={_MEMORY_MAX}", "-p", "MemorySwapMax=0",
        "-p", f"TasksMax={_TASKS_MAX}", "-p", "KillMode=control-group", "-p", "LimitCORE=0",
        "/usr/bin/env", "-i", f"PATH={_PATH}", "HOME=/tmp", "APPTAINER_CACHEDIR=/tmp",
        "/usr/bin/python3", "-I", "-c", _CGROUP_GATE, unit, str(_MEMORY_MAX), str(_TASKS_MAX),
        *container_command,
    ]


def _capture(unit, command):
    runtime_dir = f"/run/user/{os.getuid()}"
    env = {"PATH": _PATH, "XDG_RUNTIME_DIR": runtime_dir,
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus"}
    deadline = time.monotonic() + _RUNTIME + 5
    frame = bytearray()
    with subprocess.Popen(command, cwd="/", env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise SandboxError("Render timed out.")
                    block = os.read(proc.stdout.fileno(), min(65536, _FRAME_MAX + 1 - len(frame)))
                    if not block:
                        break
                    frame.extend(block)
                    if len(frame) > _FRAME_MAX:
                        raise SandboxError("Render exceeded the output limit.")
            if proc.wait(timeout=max(0, deadline - time.monotonic())):
                raise SandboxError("Sandbox or renderer failed.")
        except BaseException:
            proc.kill()
            # The service also enforces RuntimeMaxSec independently of this client.
            try:
                subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit], cwd="/", env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=4, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise
    return frame


def _invalid_constant(_):
    raise ValueError("non-finite number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite number")
    return parsed


def _decode(frame):
    if len(frame) < _HEADER.size:
        raise SandboxError("Renderer returned invalid output.")
    result_size, figure_size = _HEADER.unpack_from(frame)
    if (not 0 < result_size <= _RESULT_MAX or not 0 < figure_size <= _FIGURE_MAX
            or len(frame) != _HEADER.size + result_size + figure_size):
        raise SandboxError("Renderer returned invalid output.")
    split = _HEADER.size + result_size
    try:
        result = json.loads(frame[_HEADER.size:split].decode("utf-8"),
                            parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise SandboxError("Renderer returned invalid metadata.") from None
    if not isinstance(result, dict):
        raise SandboxError("Renderer returned invalid metadata.")
    return result, bytes(frame[split:])


def run(language: str, source: bytes, request: dict) -> tuple[dict, bytes]:
    """Render one job and return the decoded result.json and raw figure bytes.

    ``language`` is python, r, or static. ``request['format']`` is png, svg,
    or pdf. Source/metadata/figure limits are 16 MiB, 512 KiB and 24 MiB.
    Linux cgroup v2, a running systemd user manager and Apptainer with working
    namespace isolation are required. No unsandboxed fallback is attempted.
    Raises :class:`SandboxError` for rejected or failed jobs.
    """
    if not isinstance(language, str) or language not in _LANGUAGES:
        raise SandboxError("Unsupported render language.")
    if not isinstance(source, bytes) or not 0 < len(source) <= _SOURCE_MAX:
        raise SandboxError("Invalid source or source exceeds the size limit.")
    fmt = request.get("format") if isinstance(request, dict) else None
    if not isinstance(fmt, str) or fmt not in _FORMATS:
        raise SandboxError("Unsupported render format.")
    try:
        encoded = json.dumps(request, allow_nan=False, ensure_ascii=True).encode("ascii")
    except (ValueError, TypeError, RecursionError):
        raise SandboxError("Invalid render request.") from None
    if len(encoded) > _REQUEST_MAX:
        raise SandboxError("Render request exceeded the size limit.")

    try:
        with tempfile.TemporaryDirectory(prefix="workbench-inputs-", dir="/tmp") as temp:
            inputs = Path(temp)
            (inputs / f"source.{_LANGUAGES[language][0]}").write_bytes(source)
            (inputs / "request.json").write_bytes(encoded)
            unit = f"workbench-render-{uuid.uuid4().hex}.service"
            command = _systemd_command(unit, _container_command(language, fmt, inputs))
            return _decode(_capture(unit, command))
    except subprocess.TimeoutExpired:
        raise SandboxError("Render timed out.") from None
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, SandboxError):
            raise
        raise SandboxError("Sandbox is unavailable.") from None
