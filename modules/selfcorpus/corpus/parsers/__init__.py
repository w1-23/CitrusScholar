"""Public source parser router. Adapter configuration is server-side only."""

import codecs
import json
import math
import os
import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..errors import CorpusError
from .docx import parse_docx
from .html import parse_html
from .markdown import parse_markdown, parse_text
from .model import Block, Parsed, build
from .pdf import grobid_endpoint, parse_pdf
from .xml import local, parse_article, read_xml

if TYPE_CHECKING:
    from ..sources import Source

__all__ = ["Block", "Parsed", "ParserRouter"]

_MAX_INPUT = 64 * 1024 * 1024
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)
_HTML_MARKERS = re.compile(rb"\s*(?:<!doctype\s+html\b|<html\b|<head\b|<body\b)", re.I)
_ENCODED_XML_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)


def _decode(data: bytes, media_type: str, *, html: bool = False) -> tuple[str, list[str]]:
    if data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        declared = _CHARSET.search(media_type)
        if not declared and html:
            declared = re.search(r"<meta\b[^>]*?charset\s*=\s*[\"']?([\w.:-]+)",
                                 data[:8192].decode("ascii", errors="ignore"), re.I)
        encoding = declared[1] if declared else "utf-8"
    try:
        value = data.decode(encoding)
        notes = []
    except (UnicodeError, LookupError):
        value = data.decode("utf-8-sig", errors="replace")
        notes = ["text_decoding_had_errors"]
    if "\x00" in value:
        raise ValueError("Text document contains binary data")
    return value.replace("\r\n", "\n").replace("\r", "\n"), notes


def _metadata_only(source: "Source") -> Parsed:
    metadata = dict(source.metadata)
    if source.content and source.media_type.split(";", 1)[0].strip().lower() == "application/json":
        try:
            data = json.loads(source.content)
        except (UnicodeError, ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            metadata = {**data, **metadata}
    output = build("metadata-only/1", [], metadata, warnings=["source_has_no_available_full_text"])
    abstract = metadata.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        return replace(output, structured={**output.structured, "abstract": abstract})
    return output


class ParserRouter:
    def __init__(self, *, grobid_url: str | None = None, docling: bool | None = None,
                 docling_artifacts_path: str | None = None, timeout: float = 30):
        url = os.getenv("SELFCORPUS_GROBID_URL") if grobid_url is None else grobid_url
        self._grobid = grobid_endpoint(url) if url else None
        local = os.getenv("SELFCORPUS_DOCLING_ARTIFACTS") if docling_artifacts_path is None else docling_artifacts_path
        if docling is None:
            docling = bool(docling_artifacts_path) or os.getenv("SELFCORPUS_DOCLING", "").lower() in {"1", "true", "yes"}
        if docling and not local:
            raise ValueError("Enabled Docling requires a configured local artifacts directory")
        self._docling = local if docling else None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Parser timeout must be a finite positive number")
        self._timeout = timeout

    def parse(self, source: "Source") -> Parsed:
        mime = source.media_type.split(";", 1)[0].strip().lower()
        suffix = PurePosixPath(urlsplit(source.uri).path).suffix.lower()
        head = source.content[:32]
        if (mime.startswith("image/") or suffix in {".svg", ".png", ".jpeg", ".jpg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".avif", ".heic"}
                or head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                or head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise CorpusError("image_not_indexed", "Image files are not added to the corpus.", 415)
        if len(source.content) > _MAX_INPUT:
            raise CorpusError("document_too_large", "Document exceeds the parser byte limit.", 413)
        if source.metadata.get("fulltext") is False or source.metadata.get("coverage") == "metadata_only":
            return _metadata_only(source)
        try:
            parsed = self._parse(source)
        except (ValueError, OSError) as exc:
            # XML/HTML decoder and archive exceptions can embed untrusted data.
            raise CorpusError("invalid_document", "Document could not be parsed safely.", 422) from exc
        metadata = {**source.metadata, **parsed.metadata}
        if source.metadata.get("doi"):
            metadata["doi"] = source.metadata["doi"]
        return replace(parsed, metadata=metadata)

    def _parse(self, source: "Source") -> Parsed:
        data = source.content
        content_type = source.media_type.split(";", 1)[0].strip().lower()
        suffix = PurePosixPath(urlsplit(source.uri).path).suffix.lower()
        prefix = data[:1024].removeprefix(codecs.BOM_UTF8).lstrip()
        if prefix.startswith(b"%PDF-") or content_type in {"application/pdf", "application/x-pdf"}:
            if not prefix.startswith(b"%PDF-"):
                raise ValueError("Invalid PDF signature")
            return parse_pdf(data, self._grobid, self._docling, self._timeout)
        if content_type == _DOCX or (suffix == ".docx" and prefix.startswith(b"PK\x03\x04")):
            return parse_docx(data)
        if content_type == "application/xhtml+xml":
            root = read_xml(data)
            if local(root.tag).lower() != "html":
                raise ValueError("XHTML root element is not html")
            value, warnings = _decode(data, source.media_type, html=True)
            return parse_html(value, warnings)
        if content_type in {"application/xml", "text/xml"} or content_type.endswith("+xml") or (
                suffix in {".xml", ".tei", ".jats", ".nxml"} and (prefix.startswith(b"<") or data.startswith(_ENCODED_XML_BOMS))):
            return parse_article(read_xml(data))
        if content_type == "text/html" or _HTML_MARKERS.match(prefix) or (
                suffix in {".html", ".htm"} and prefix.startswith(b"<")):
            return parse_html(*_decode(data, source.media_type, html=True))
        if content_type in {"text/markdown", "text/x-markdown"} or (
                content_type in {"", "text/plain", "application/octet-stream"} and suffix in {".md", ".markdown"}):
            return parse_markdown(*_decode(data, source.media_type))
        if content_type == "text/plain" or (content_type in {"", "application/octet-stream"} and suffix == ".txt"):
            return parse_text(*_decode(data, source.media_type))
        raise CorpusError("unsupported_media_type", "Document media type is not supported.", 415)
