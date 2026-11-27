"""Upload, DOI, and public URL entry points for corpus ingestion."""
from __future__ import annotations

import base64
import binascii
import time
from urllib.parse import quote

from ..errors import CorpusError
from ._documents import MAX_BYTES, Source, _clean_string, _media_type, _now, _received_media, _validate_content
from ._doi import MAX_METADATA_BYTES, normalize_doi, resolve_doi
from ._http import (FETCH_TIMEOUT, MAX_REDIRECTS, MAX_ROBOTS_BYTES, REQUEST_TIMEOUT, HTTPResponse, PublicHTTP,
                    _canonical_url, _DEFAULT_HTTP)
from ._robots import MIN_ORIGIN_INTERVAL
from ._verified_text import KIND as VERIFIED_TEXT, read_verified_text, validate_spec as validate_verified_spec

__all__ = ['Source', 'Sources', 'normalize_doi', 'resolve_doi', 'PublicHTTP', 'HTTPResponse',
           'MAX_BYTES', 'MAX_METADATA_BYTES', 'MAX_ROBOTS_BYTES', 'MAX_REDIRECTS',
           'FETCH_TIMEOUT', 'REQUEST_TIMEOUT', 'MIN_ORIGIN_INTERVAL']


class Sources:
    def __init__(self, http: PublicHTTP | None = None):
        self.http = http or _DEFAULT_HTTP

    def fetch(self, spec: dict) -> Source:
        if not isinstance(spec, dict):
            raise CorpusError('invalid_source', 'A source object is required.')
        kind = spec.get('kind')
        if kind == 'upload':
            return self._upload(spec)
        if kind == 'doi':
            return resolve_doi(spec.get('doi'), http=self.http)
        if kind == 'url':
            return self._url(spec)
        if kind == VERIFIED_TEXT:
            return read_verified_text(spec)
        raise CorpusError('invalid_source', 'Unsupported source kind.')

    @staticmethod
    def _upload(spec: dict) -> Source:
        media = _media_type(spec.get('media_type'))
        filename = _clean_string(spec.get('filename'))
        if not filename or len(filename) > 255 or filename in {'.', '..'} or any(
            ord(c) < 32 or 127 <= ord(c) <= 159 or c in '/\\' for c in filename
        ):
            raise CorpusError('invalid_upload', 'Supply a plain upload filename of at most 255 characters.')
        encoded = spec.get('content_b64')
        if not isinstance(encoded, str) or not encoded:
            raise CorpusError('invalid_upload', 'Upload content must be a nonempty base64 string.')
        if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
            raise CorpusError('source_too_large', 'The upload exceeds the allowed size.', 413)
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise CorpusError('invalid_upload', 'Invalid base64 upload content.') from None
        if len(content) > MAX_BYTES:
            raise CorpusError('source_too_large', 'The upload exceeds the allowed size.', 413)
        _validate_content(content, media)
        metadata = {'filename': filename, 'coverage': 'provided_document'}
        uri = 'upload:' + quote(filename, safe='')
        if spec.get('doi') is not None:
            metadata['doi'] = normalize_doi(spec['doi'])
        return Source(content, media, uri, metadata, 'User-provided upload.', _now(), None, 'upload')

    def _url(self, spec: dict) -> Source:
        note = _clean_string(spec.get('access_note'))
        if not note or len(note) > 2000:
            raise CorpusError('invalid_access_note', 'Provide an access note of at most 2,000 characters.')
        url = _canonical_url(spec.get('url'))
        response = self.http.get(url, MAX_BYTES, time.monotonic() + FETCH_TIMEOUT)
        media = _received_media(response)
        _validate_content(response.content, media)
        metadata = {'requested_url': url, 'coverage': 'public_document'}
        return Source(response.content, media, response.url, metadata, note, _now(), response.status, 'url')
