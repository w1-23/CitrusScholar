"""Approved launcher for v1 analyzers. Runs only inside the offline Code Worker."""
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile


SKILLS = frozenset({"literature-verification", "data-audit", "research-integrity"})
ENTRYPOINT = "03_skill_fusion/run.py"
REQUIREMENTS = "03_skill_fusion/requirements-8043.lock"
MAX_REPORT_BYTES = 8 * 1024**2 + 1  # Handoff writer appends one newline.


def locked_dependencies(text):
    """Only exact installed-distribution pins are accepted, never pip options."""
    pins = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9.]+)", line)
        if not match:
            raise ValueError("fusion_invalid_environment_lock")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in pins:
            raise ValueError("fusion_invalid_environment_lock")
        pins[name] = match[2]
    if not {"numpy", "openpyxl", "pandas", "pandera", "pillow", "pypdf"} <= pins.keys():
        raise ValueError("fusion_incomplete_environment_lock")
    return pins


def runtime_identity(root, bundle):
    required = bundle["dependencies"]
    lock = (root / REQUIREMENTS).read_bytes()
    if (required["requirements"] != REQUIREMENTS
            or hashlib.sha256(lock).hexdigest() != required["sha256"]
            or required["system"] != ["libseccomp.so.2"]):
        raise ValueError("fusion_invalid_environment_lock")
    pins = locked_dependencies(lock.decode())
    versions = {}
    for name in pins:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    try:
        library = ctypes.CDLL("libseccomp.so.2")
        # The unmodified handoff requires this name when building its filter.
        resolve = library.seccomp_syscall_resolve_name
        resolve.argtypes = [ctypes.c_char_p]
        resolve.restype = ctypes.c_int
        seccomp = resolve(b"fchmodat2") >= 0
    except (OSError, AttributeError):
        seccomp = False
    host_platform = f"{sys.platform}-{platform.machine()}"
    available = (platform.python_implementation() == "CPython"
                 and required["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"
                 and required["platform"] == host_platform and seccomp and versions == pins)
    return {"python": platform.python_version(), "platform": host_platform,
            "dependencies": versions, "lock_sha256": required["sha256"],
            "libseccomp_fchmodat2": seccomp}, available


def run_analyzer(root, skill, request):
    # -I omits the script directory. Insert only the release bundle directory,
    # never a writable scratch or user input path, then call the original entry.
    bootstrap = ("import pathlib,runpy,sys;sys.argv=sys.argv[1:];"
                 "root=pathlib.Path(sys.argv[0]).parent;"
                 "sys.path[:0]=[str(root),str(root.parent/'upstreams/refchecker/tree/src')];"
                 "runpy.run_path(sys.argv[0],run_name='__main__')")
    with tempfile.TemporaryDirectory(prefix="fusion-result-", dir="/tmp") as work:
        root_tmp = Path(work)
        source = Path("/input/fusion-data")
        if not source.is_dir():
            source = root_tmp / "empty"
            source.mkdir(mode=0o700)
        output = root_tmp / "output"
        output.mkdir(mode=0o700)
        done = subprocess.run([sys.executable, "-I", "-B", "-c", bootstrap,
                               str(root / ENTRYPOINT), skill, "--request", str(request),
                               "--input", str(source), "--output", str(output)],
                              stdin=subprocess.DEVNULL, check=False)
        path = output / "result.json"
        if set(os.listdir(output)) != {"result.json"}:
            raise ValueError("fusion_unexpected_analyzer_outputs")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_REPORT_BYTES:
                raise ValueError("fusion_analyzer_output_invalid")
            envelope = stream.read(MAX_REPORT_BYTES + 1)
        if len(envelope) > MAX_REPORT_BYTES:
            raise ValueError("fusion_analyzer_output_invalid")
        report = json.loads(envelope)
        expected_exit = 1 if report.get("status") == "failed" else 0
        if done.returncode != expected_exit or report.get("artifacts") != []:
            raise ValueError("fusion_analyzer_result_inconsistent")
    return report


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SKILLS:
        raise ValueError("fusion_skill_required")
    skill = sys.argv[1]
    root = Path("/input/fusion")
    request = Path("/input/fusion-request.json")
    raw = request.read_bytes()
    payload = json.loads(raw)
    bundle_raw = (root / "bundle.json").read_bytes()
    bundle = json.loads(bundle_raw)
    runtime, ready = runtime_identity(root, bundle)
    if ready:
        report = run_analyzer(root, skill, request)
    else:
        report = {"status": "unsupported", "findings": [], "evidence": [], "locations": [],
                  "coverage": [], "limitations": ["fusion_worker_environment_mismatch"],
                  "artifacts": [], "execution": {"skill": skill,
                  "version": bundle["skills"][skill], "inputs": payload["inputs"],
                  "options": payload["options"]}}
    report["worker"] = {"schema": "citrus.fusion-worker/1", "environment": runtime,
                        "request_sha256": hashlib.sha256(raw).hexdigest(),
                        "bundle_sha256": hashlib.sha256(bundle_raw).hexdigest()}
    encoded = json.dumps(report, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError("fusion_analyzer_result_size")
    # Readable by the existing capability-dropped OCI collector after execution.
    fd = os.open("/outputs/result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)


if __name__ == "__main__":
    main()
