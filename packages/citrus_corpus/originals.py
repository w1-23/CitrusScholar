"""Read authorized inbox entries and preserve exact acquired bytes."""
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
from tempfile import NamedTemporaryFile

from .prepare import MAX_SOURCE_BYTES, PreparationError, _sync_dir
from .queue import SHA256


def inbox_relative(path):
    value = PurePosixPath(str(path or ""))
    if (not value.parts or value.is_absolute() or ".." in value.parts
            or len(str(value)) > 1024 or "\x00" in str(value)):
        raise ValueError("inbox_path_invalid")
    return value


def read_inbox(root, relative):
    parts = inbox_relative(relative).parts
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stream = stack.enter_context(os.fdopen(descriptor, "rb"))
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise PreparationError("source_size_invalid")
        return _bounded(stream)


def _bounded(stream):
    data = stream.read(MAX_SOURCE_BYTES + 1)
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise PreparationError("source_size_invalid")
    return data


def _stage(root, name, *parts):
    with NamedTemporaryFile(dir=root, delete=False) as stream:
        try:
            for part in parts:
                stream.write(part)
            stream.flush()
            os.fsync(stream.fileno())
            staged = Path(stream.name)
            try:
                (root / name).hardlink_to(staged)
                _sync_dir(root)
            except FileExistsError:
                pass
        finally:
            Path(stream.name).unlink(missing_ok=True)


def archive(root, data):
    digest = sha256(data).hexdigest()
    _stage(root, digest, data)
    archived = read_archive(root, digest)
    if archived != data:
        raise PreparationError("original_integrity_invalid")
    return digest


def read_archive(root, digest):
    if not SHA256.fullmatch(digest or ""):
        raise ValueError("source_sha256_invalid")
    data = read_inbox(root, digest)
    if sha256(data).hexdigest() != digest:
        raise PreparationError("original_integrity_invalid")
    return data


def capture(root, job_id, acquire):
    """Resume a completed acquisition from a durable, atomic source receipt."""
    name = "capture-" + job_id
    path = root / name
    if not path.exists():
        data, fmt = acquire()
        digest = sha256(data).hexdigest()
        receipt = json.dumps({"format": fmt, "sha256": digest}, separators=(",", ":")).encode() + b"\n"
        _stage(root, name, receipt, data)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES + 512:
            raise PreparationError("source_size_invalid")
        receipt = json.loads(stream.readline(512))
        data = _bounded(stream)
        if sha256(data).hexdigest() != receipt.get("sha256"):
            raise PreparationError("original_integrity_invalid")
        return data, receipt["format"]
