"""Immutable Markdown and metadata for the independent literature corpus.

The intake adapter verifies source bytes before parsing. This module normalizes
and stores Markdown and enforces identities and byte hashes on reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import chunks, rights

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_CHARS = chunks.MAX_DOCUMENT_CHARS
MAX_METADATA_BYTES = 256 * 1024
MAX_ARCHIVE_METADATA_BYTES = 64 * 1024
MAX_HEADER_CHARS = 512
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp|file)://[^\s<>'\"]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WINPATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s<>'\"]+")
_POSIXPATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:[^/\s]+/)+[^/\s]+")
_CRED_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
    r"password|passwd|authorization)\s*[:=]\s*\S+")


class PreparationError(RuntimeError):
    """A bounded, path-free preparation failure code."""

    def __init__(self, code: str):
        self.code = str(code or "preparation_failed")
        super().__init__(self.code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identifier(value: object, kind: str) -> str:
    text = str(value or "").strip()
    if (not text or text in {".", ".."} or ".." in text
            or not _ID_RE.fullmatch(text)):
        raise ValueError(f"{kind}_invalid")
    return text


def public_text(value: object, limit: int = MAX_HEADER_CHARS,
                keep_newlines: bool = False) -> str:
    """Bound and redact text that is safe to persist or return."""
    text = "".join(ch for ch in str(value or "")
                   if ch in "\n\t" or ord(ch) >= 32)
    text = _URL_RE.sub("[url]", text)
    text = _CRED_RE.sub("[redacted]", text)
    text = _WINPATH_RE.sub("[path]", text)
    text = _POSIXPATH_RE.sub("[path]", text)
    text = _IPV4_RE.sub("[ip]", text)
    if keep_newlines:
        text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    else:
        text = " ".join(text.split())
    return text[:max(0, int(limit))]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bounded(path: Path, maximum: int) -> bytes:
    if path.is_symlink():
        raise PreparationError("candidate_path_invalid")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PreparationError("candidate_source_unavailable") from exc
    if size > maximum:
        raise PreparationError("document_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PreparationError("candidate_source_unavailable") from exc
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PreparationError("document_too_large")
    return data


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise PreparationError("unsupported_source_format")
    if not text.strip():
        raise PreparationError("empty_document")
    if len(text) > MAX_DOCUMENT_CHARS:
        raise PreparationError("document_too_large")
    return text if text.endswith("\n") else text + "\n"


def _read_license(path: Path, digest: str) -> dict[str, Any]:
    try:
        record = json.loads(_read_bounded(path, MAX_ARCHIVE_METADATA_BYTES).decode("utf-8"))
    except (PreparationError, OSError, UnicodeDecodeError, ValueError):
        return {}
    if (not isinstance(record, dict) or record.get("sha256_of_saved_bytes") != digest
            or not isinstance(record.get("url"), str)
            or rights.evaluate_source_rights(record)["status"] != "trusted"):
        return {}
    source = record.get("license_evidence")
    if source == "ncbi_pmc_oa" or (source == rights.PMC_CLOUD_EVIDENCE and rights.PMC_CLOUD_METADATA_URL.fullmatch(record["url"])):
        return record
    return {}


def _document_dir(documents_root: Path, document_id: str, version_id: str) -> Path:
    document_id = identifier(document_id, "document_id")
    version_id = identifier(version_id, "version_id")
    try:
        root = Path(documents_root).resolve(strict=False)
    except OSError as exc:
        raise PreparationError("document_path_invalid") from exc
    if root.is_symlink():
        raise PreparationError("document_path_invalid")
    target = root / document_id / version_id
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise PreparationError("document_path_invalid") from exc
    if root not in resolved.parents or resolved.name != version_id:
        raise PreparationError("document_path_invalid")
    parent = root / document_id
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise PreparationError("document_path_invalid")
    if target.is_symlink():
        raise PreparationError("document_path_invalid")
    return target


def _canonical_section_path(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:chunks.MAX_SECTIONS]:
        text = public_text(item, chunks.MAX_SECTION_NAME)
        if text:
            result.append(text)
    return result


def read_document_files(documents_root: Path, document_id: str,
                        version_id: str) -> tuple[dict[str, Any], str]:
    """Read and fully validate one immutable version directory."""
    target = _document_dir(documents_root, document_id, version_id)
    if not target.exists():
        raise FileNotFoundError("document_missing")
    if not target.is_dir():
        raise ValueError("document_integrity_invalid")
    markdown_path = target / "fulltext.md"
    metadata_path = target / "metadata.json"
    for path in (markdown_path, metadata_path):
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("document_missing")
    try:
        markdown_bytes = _read_bounded(markdown_path, MAX_SOURCE_BYTES)
        metadata_bytes = _read_bounded(metadata_path, MAX_METADATA_BYTES)
    except PreparationError as exc:
        raise ValueError("document_integrity_invalid") from exc
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        markdown = markdown_bytes.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("document_integrity_invalid") from exc
    if not isinstance(metadata, dict):
        raise ValueError("document_integrity_invalid")
    try:
        schema_version = int(metadata.get("schema_version", 0))
        char_count = int(metadata.get("char_count", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError("document_integrity_invalid") from exc
    if schema_version != chunks.SCHEMA_VERSION:
        raise ValueError("document_integrity_invalid")
    if (str(metadata.get("document_id") or "") != document_id
            or str(metadata.get("version_id") or "") != version_id):
        raise ValueError("document_integrity_invalid")
    digest = str(metadata.get("content_sha256") or "").casefold()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("document_integrity_invalid")
    if _sha256_bytes(markdown_bytes) != digest or char_count != len(markdown):
        raise ValueError("document_integrity_invalid")
    if not markdown.strip():
        raise ValueError("document_integrity_invalid")
    metadata = dict(metadata)
    evidence = _read_license(target / "license.json", digest)
    if evidence:
        metadata.update(license=rights.license_token(evidence["license"]),
                        license_evidence=evidence["license_evidence"], license_source_url=evidence["url"])
    metadata["section_path"] = _canonical_section_path(metadata.get("section_path"))
    return metadata, markdown


def _write_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_document(documents_root: Path, document_id: str, version_id: str,
                   metadata: dict[str, Any], markdown: str) -> dict[str, Any]:
    """Create the immutable version atomically, or reuse an identical one."""
    target = _document_dir(documents_root, document_id, version_id)
    if target.exists():
        try:
            existing, _ = read_document_files(documents_root, document_id, version_id)
        except (FileNotFoundError, ValueError) as exc:
            raise PreparationError("document_version_conflict") from exc
        if str(existing.get("content_sha256") or "") != str(
                metadata.get("content_sha256") or ""):
            raise PreparationError("document_version_conflict")
        return existing
    root = Path(documents_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    markdown_bytes = markdown.encode("utf-8")
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)
                      + "\n").encode("utf-8")
    if len(metadata_bytes) > MAX_METADATA_BYTES:
        raise PreparationError("metadata_too_large")
    staging = root / (".tmp-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    try:
        _write_file(staging / "fulltext.md", markdown_bytes)
        _write_file(staging / "metadata.json", metadata_bytes)
        _sync_dir(staging)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.rename(staging, target)
        except OSError as exc:
            if not target.exists():
                raise PreparationError("document_write_failed") from exc
            existing, _ = read_document_files(documents_root, document_id, version_id)
            if str(existing.get("content_sha256") or "") != str(
                    metadata.get("content_sha256") or ""):
                raise PreparationError("document_version_conflict") from exc
            return existing
        _sync_dir(target.parent)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return metadata
