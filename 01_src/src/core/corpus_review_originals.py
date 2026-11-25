"""Read review originals from the configured archive, never the recorded host."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat

QUEUE_DIRNAME = "_review_queue"
ARCHIVE_DIRNAME = "archive"
_DIR = re.compile(r"[0-9A-Za-z\u4e00-\u9fff._-]{1,80}")
_SHA256 = re.compile(r"[0-9a-f]{64}", re.I)
_TEXT_TYPES = {".md", ".md2", ".txt", ".csv"}
_MEDIA_TYPES = {
    ".md": "text/markdown", ".md2": "text/markdown", ".txt": "text/plain",
    ".csv": "text/csv", ".pdf": "application/pdf", ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_ORIGINAL_NAMES = {"paper.md", "paper.md2", "paper.txt"} | {
    "original" + suffix for suffix in _MEDIA_TYPES
}


def original_media_type(filename: str) -> str:
    return _MEDIA_TYPES[Path(filename).suffix]


def is_text_original(filename: str) -> bool:
    return Path(filename).suffix in _TEXT_TYPES


def _archive_entry(recorded_path: str) -> tuple[str, str]:
    # Examine the recorded spelling before Path can normalize '.' or turn a
    # traversal into an apparently valid basename. Only this exact suffix is
    # relocatable, and its old host prefix is never read or followed.
    parts = recorded_path.split("/")
    if (len(parts) < 5 or parts[0] or "\\" in recorded_path or "\x00" in recorded_path
            or any(part in {"", ".", ".."} for part in parts[1:])
            or parts[-4:-2] != [QUEUE_DIRNAME, ARCHIVE_DIRNAME]
            or not _DIR.fullmatch(parts[-2]) or parts[-1] not in _ORIGINAL_NAMES):
        raise FileNotFoundError("review original unavailable")
    return parts[-2], parts[-1]


def read_original(data_root: Path, recorded_path: str, raw_sha256: str, raw_bytes: int,
                  *, max_bytes: int, include_data: bool = False) -> tuple[str, bytes]:
    """Verify the exact entry under the trusted root and optionally return its bytes."""
    folder, filename = _archive_entry(recorded_path)
    parts = (QUEUE_DIRNAME, ARCHIVE_DIRNAME, folder, filename)
    if raw_sha256 and not _SHA256.fullmatch(raw_sha256):
        raise FileNotFoundError("review original unavailable")
    # Rows already naming the current archive retain old no-digest
    # compatibility. Any relocation needs the recorded raw SHA-256.
    if not raw_sha256 and recorded_path != str(data_root.joinpath(*parts)):
        raise FileNotFoundError("review original unavailable")

    # Pin each directory before opening the next and use the same descriptor
    # for validation and serving. In particular, never reopen a verified path.
    try:
        with ExitStack() as stack:
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(data_root, dir_flags)
            stack.callback(os.close, directory)
            for part in parts[:-1]:
                directory = os.open(part, dir_flags, dir_fd=directory)
                stack.callback(os.close, directory)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(filename, flags, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= max_bytes
                        or (raw_bytes and raw_bytes != info.st_size)):
                    raise FileNotFoundError("review original unavailable")
                if include_data:
                    data = stream.read(max_bytes + 1)
                    size, digest = len(data), hashlib.sha256(data).hexdigest()
                elif raw_sha256:
                    data = b""
                    size, digest = _hash_source(stream, max_bytes)
                else:
                    return filename, b""
                if size != info.st_size or (raw_sha256 and digest != raw_sha256.lower()):
                    raise FileNotFoundError("review original unavailable")
                return filename, data
    except (OSError, AttributeError, NotImplementedError):
        raise FileNotFoundError("review original unavailable") from None


def _hash_source(stream, max_bytes: int) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    while block := stream.read(min(1024 * 1024, max_bytes + 1 - size)):
        size += len(block)
        if size > max_bytes:
            break
        digest.update(block)
    return size, digest.hexdigest()
