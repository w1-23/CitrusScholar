"""Shared source record, document media and provenance helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..errors import CorpusError
from ._http import HTTPResponse


MAX_BYTES = 16 * 1024 * 1024


_MEDIA = frozenset({
    'application/pdf', 'text/html', 'application/xhtml+xml', 'text/plain',
    'text/markdown', 'text/xml', 'application/xml', 'application/jats+xml',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
})


_MEDIA_ALIASES = {'application/x-pdf': 'application/pdf', 'text/x-markdown': 'text/markdown'}


@dataclass(frozen=True)
class Source:
    content: bytes
    media_type: str
    uri: str
    metadata: dict
    access_note: str
    retrieved_at: str
    http_status: int | None
    kind: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _media_type(value: str) -> str:
    if not isinstance(value, str):
        raise CorpusError('unsupported_media_type', 'A supported media type is required.', 415)
    media = value.split(';', 1)[0].strip().lower()
    media = _MEDIA_ALIASES.get(media, media)
    if media not in _MEDIA and not re.fullmatch(r'(?:application|text)/[a-z0-9._-]+\+xml', media):
        raise CorpusError('unsupported_media_type', 'The source media type is unsupported.', 415)
    return media


def _validate_content(content: bytes, media: str) -> None:
    if not content.strip():
        raise CorpusError('source_empty', 'The source does not contain a document.', 422)
    if media == 'application/pdf' and not re.match(rb'%PDF-\d\.\d', content[:8]):
        raise CorpusError('source_media_mismatch', 'The supplied document does not have a PDF header.', 422)


def _received_media(response: HTTPResponse) -> str:
    header = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if header in {'', 'application/octet-stream'} and re.match(rb'%PDF-\d\.\d', response.content[:8]):
        return 'application/pdf'
    return _media_type(header)


def _clean_string(value) -> str:
    if not isinstance(value, str):
        return ''
    return ''.join('\ufffd' if 0xD800 <= ord(c) <= 0xDFFF else c for c in value).strip()
