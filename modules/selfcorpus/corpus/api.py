"""HTTP boundary for the independent Selfcorpus application.

POST ingestion accepts up to 24 MiB of wire JSON, accommodating a 16 MiB
source uploaded as Base64. Other routes are capped at 256 KiB. Request
bodies must use a single Content-Length and arrive within a bounded time.
"""

from __future__ import annotations

import json
import math
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from urllib.parse import unquote

from .auth import (DEFAULT_PORT, MAX_BODY_BYTES, MAX_INGEST_BODY_BYTES,
                   MAX_RESPONSE_BYTES, AuthenticationError, secret_bytes, verify_signed_actor)
from .errors import CorpusError

API_PREFIX = '/api/v1'
DEFAULT_HOST = '127.0.0.1'
MAX_TARGET_BYTES = 4096
SOCKET_TIMEOUT = 5.0
BODY_TIMEOUT = 30.0
BODY_CHUNK_BYTES = 64 * 1024


def _bad_request(message: str) -> CorpusError:
    return CorpusError('invalid_request', message, status=400)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_nonfinite(_):
    raise ValueError('nonfinite JSON number')


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        return _reject_nonfinite(text)
    return number


def _check_unicode(payload: object) -> None:
    if isinstance(payload, str):
        # A JSON escape can produce a lone surrogate even after strict UTF-8 decoding.
        if not payload.isascii():
            payload.encode('utf-8')
    elif isinstance(payload, dict):
        for key, value in payload.items():
            _check_unicode(key)
            _check_unicode(value)
    elif isinstance(payload, list):
        for value in payload:
            _check_unicode(value)


def _json_object(body: bytes | bytearray) -> dict:
    try:
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object,
                             parse_float=_finite_float, parse_constant=_reject_nonfinite)
        _check_unicode(payload)
    except (ValueError, RecursionError):
        raise _bad_request('Request body must contain valid JSON.') from None
    if not isinstance(payload, dict):
        raise _bad_request('Request body must be a JSON object.')
    return payload


def _fields(payload: dict, required: tuple = (), optional: tuple = ()) -> None:
    if not set(required) <= payload.keys() or not payload.keys() <= set(required + optional):
        raise _bad_request('Request has missing or unsupported fields.')


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _path_parts(target: str) -> tuple[str, ...]:
    if (not target.startswith('/') or target.startswith('//') or len(target) > MAX_TARGET_BYTES or
            any(not 33 <= ord(character) <= 126 for character in target) or
            '?' in target or '#' in target or re.search(r'%(?![0-9A-Fa-f]{2})', target)):
        raise _bad_request('Invalid request target.')
    if not target.startswith(API_PREFIX + '/'):
        raise CorpusError('not_found', 'Endpoint not found.', status=404)
    try:
        parts = tuple(unquote(part, errors='strict') for part in target[len(API_PREFIX) + 1:].split('/'))
    except UnicodeDecodeError:
        raise _bad_request('Invalid request target.') from None
    if any(not part or part in ('.', '..') or '\\' in part or
           not all(character.isprintable() for character in part) for part in parts):
        raise _bad_request('Invalid request target.')
    return parts


def _dispatch(app, actor, method: str, parts: tuple[str, ...], body: bytes | bytearray) -> dict:
    first, count = parts[0], len(parts)
    if method == 'GET':
        if parts[:2] == ('ingestion', 'requests') and count == 3:
            return app.request(actor, parts[2])
        if first == 'documents' and count in (2, 3) and (count == 2 or parts[2] == 'chunks'):
            return app.document(actor, parts[1], chunks=count == 3)
        if first == 'index-generations' and count == 2:
            return app.generation(actor, parts[1])
        if parts[:2] == ('publication', 'proposals') and count == 3:
            return app.proposal(actor, parts[2])
    elif method == 'POST':
        payload = _json_object(body)
        if parts == ('ingestion', 'requests'):
            _fields(payload, ('spec', 'scope_ref', 'idempotency_key'))
            spec, scope_ref, key = (payload[name] for name in ('spec', 'scope_ref', 'idempotency_key'))
            if not isinstance(spec, dict) or not _nonempty_text(scope_ref) or not _nonempty_text(key):
                raise _bad_request('Expected spec object, scope_ref, and idempotency_key.')
            return app.submit(actor, spec, scope_ref, key)
        if parts == ('ingestion', 'verified-shared'):
            _fields(payload, ('spec', 'idempotency_key'))
            spec, key = payload['spec'], payload['idempotency_key']
            if not isinstance(spec, dict) or not _nonempty_text(key):
                raise _bad_request('Expected spec object and idempotency_key.')
            return app.submit_verified_shared(actor, spec, key)
        if parts == ('retrieval', 'search'):
            _fields(payload, ('query', 'scope'), ('filters', 'limit'))
            query, scope = payload['query'], payload['scope']
            filters, limit = payload.get('filters', {}), payload.get('limit', 10)
            if not isinstance(query, str) or not isinstance(scope, dict) or not isinstance(filters, dict):
                raise _bad_request('Expected query string and scope and filters objects.')
            if type(limit) is not int or limit < 1:
                raise _bad_request('Expected a positive integer limit.')
            _fields(scope, optional=('personal', 'project_refs', 'shared'))
            if any(type(scope.get(field, False)) is not bool for field in ('personal', 'shared')):
                raise _bad_request('Scope flags must be booleans.')
            refs = scope.get('project_refs', [])
            if not isinstance(refs, list) or any(not _nonempty_text(ref) for ref in refs):
                raise _bad_request('Expected scope.project_refs to be an array of references.')
            return app.search(actor, query, scope, filters, limit)
        if parts == ('publication', 'proposals'):
            _fields(payload, ('request_ref',), ('approve',))
            request_ref, approve = payload['request_ref'], payload.get('approve')
            if not _nonempty_text(request_ref) or ('approve' in payload and type(approve) is not bool):
                raise _bad_request('Expected request_ref and optional boolean approve.')
            return app.proposal(actor, request_ref, approve=approve)
        if first == 'scopes' and count == 3 and parts[2] in ('withdraw', 'rollback'):
            if parts[2] == 'withdraw':
                _fields(payload, ('document_ref',))
                if not _nonempty_text(payload['document_ref']):
                    raise _bad_request('Expected document_ref.')
                return app.withdraw(actor, parts[1], payload['document_ref'])
            _fields(payload, ('generation_ref',))
            if not _nonempty_text(payload['generation_ref']):
                raise _bad_request('Expected generation_ref.')
            return app.rollback(actor, parts[1], payload['generation_ref'])
    raise CorpusError('not_found', 'Endpoint not found.', status=404)


class _Handler(BaseHTTPRequestHandler):
    server_version = 'Selfcorpus'
    sys_version = ''

    def setup(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        super().setup()

    def log_message(self, *args):
        # Request targets and error inputs may contain private references.
        pass

    def _response(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
        if len(data) > MAX_RESPONSE_BYTES:
            raise CorpusError('response_too_large', 'Response exceeds the service limit.', status=500)
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)
        except OSError:
            # Client disconnected or its socket timed out.
            pass

    def _error(self, status: int, code: str, message: str, retryable: bool = False) -> None:
        try:
            self._response(status, {'error': {'code': code, 'message': message, 'retryable': retryable}})
        except (CorpusError, TypeError, ValueError, RecursionError):
            self._response(500, {'error': {'code': 'internal_error',
                                          'message': 'Unexpected service error.', 'retryable': False}})

    def send_error(self, code, message=None, explain=None):
        # Protocol errors are sanitized as well as application errors.
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._error(405, 'method_not_allowed', 'HTTP method is not supported.')
        else:
            self._error(code, 'invalid_request', 'Invalid HTTP request.')

    def _body(self, *, ingestion: bool) -> bytearray:
        if 'Transfer-Encoding' in self.headers:
            raise _bad_request('Transfer-Encoding is not supported.')
        lengths = self.headers.get_all('Content-Length', [])
        if (len(lengths) > 1 or lengths and
                not re.fullmatch(r'[0-9]{1,12}', lengths[0])):
            raise _bad_request('Invalid Content-Length.')
        if not lengths and self.command == 'POST':
            raise CorpusError('length_required', 'Content-Length is required.', status=411)
        size = int(lengths[0]) if lengths else 0
        limit = MAX_INGEST_BODY_BYTES if ingestion else MAX_BODY_BYTES
        if size > limit:
            raise CorpusError('request_too_large', 'Request exceeds the service limit.', status=413)
        if self.command == 'GET' and size:
            raise _bad_request('GET request bodies are not supported.')
        if self.command == 'POST':
            types = self.headers.get_all('Content-Type', [])
            if len(types) != 1 or types[0].split(';', 1)[0].strip().lower() != 'application/json':
                raise CorpusError('unsupported_media_type', 'Expected application/json.', status=415)
        body = bytearray()
        if size:
            deadline = monotonic() + BODY_TIMEOUT
            try:
                while len(body) < size:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise socket.timeout()
                    self.request.settimeout(min(SOCKET_TIMEOUT, remaining))
                    chunk = self.rfile.read1(min(BODY_CHUNK_BYTES, size - len(body)))
                    if not chunk:
                        raise _bad_request('Incomplete request body.')
                    body.extend(chunk)
            finally:
                self.request.settimeout(SOCKET_TIMEOUT)
        return body

    def _handle(self):
        try:
            # The HTTP parser may normalize self.path. Sign the actual wire target.
            target = self.requestline.split()[1]
            parts = _path_parts(target)
            ingestion = self.command == 'POST' and parts in (
                ('ingestion', 'requests'), ('ingestion', 'verified-shared'))
            body = self._body(ingestion=ingestion)
            app = self.server.corpus_app
            if self.command == 'GET' and parts == ('health',):
                result = app.health()
            else:
                actor = verify_signed_actor(self.server.corpus_secret, self.command, target,
                                            body, self.headers)
                result = _dispatch(app, actor, self.command, parts, body)
            status = 202 if ingestion else 200
            self._response(status, result)
        except AuthenticationError:
            self._error(401, 'unauthorized', 'Valid Host authentication is required.')
        except CorpusError as exc:
            self._error(exc.status, exc.code, str(exc), exc.retryable)
        except socket.timeout:
            self._error(408, 'request_timeout', 'Timed out reading the request.', True)
        except Exception:
            self._error(500, 'internal_error', 'Unexpected service error.')

    do_GET = _handle
    do_POST = _handle


class CorpusHTTPServer(ThreadingHTTPServer):
    """Loopback by default, with no App or server start on import."""

    def __init__(self, app, secret: str | bytes, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.corpus_app = app
        self.corpus_secret = secret_bytes(secret)
        super().__init__((host, port), _Handler)


def serve(app, secret: str | bytes, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the standalone HTTP API until interrupted."""
    with CorpusHTTPServer(app, secret, host, port) as server:
        server.serve_forever()
