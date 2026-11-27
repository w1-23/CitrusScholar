"""Thin standard-library client for trusted Host-side calls to Selfcorpus.

Derive the Actor from the authenticated Host session and permissions. Never
construct it from a model-generated tool argument or JSON request body.
"""

from __future__ import annotations

import http.client
import json
from urllib.parse import quote, urlsplit

from .auth import (DEFAULT_PORT, INGEST_TARGET, MAX_BODY_BYTES, MAX_INGEST_BODY_BYTES,
                   MAX_RESPONSE_BYTES, VERIFIED_SHARED_TARGET, Actor, secret_bytes, signed_headers)

DEFAULT_URL = f'http://127.0.0.1:{DEFAULT_PORT}'
_PREFIX = '/api/v1'


class HostClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


def _segment(reference: str) -> str:
    if not isinstance(reference, str) or reference in ('', '.', '..'):
        raise ValueError('expected a nonempty resource reference')
    return quote(reference, safe='')


class HostClient:
    def __init__(self, secret: str | bytes, base_url: str = DEFAULT_URL, *, timeout: float = 10.0):
        address = urlsplit(base_url)
        if (address.scheme not in ('http', 'https') or not address.hostname or
                '@' in address.netloc or
                address.path not in ('', '/') or address.query or address.fragment):
            raise ValueError('base_url must be an HTTP(S) origin without credentials')
        if not 0 < timeout < float('inf'):
            raise ValueError('timeout must be positive and finite')
        self._secret = secret_bytes(secret)
        self._connection = (http.client.HTTPSConnection if address.scheme == 'https'
                            else http.client.HTTPConnection)
        self._host, self._port, self.timeout = address.hostname, address.port, timeout

    def _call(self, method: str, path: str, *, actor: Actor | None, payload: dict | None = None) -> dict:
        target = _PREFIX + path
        body = b'' if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                                       separators=(',', ':')).encode('utf-8')
        ingestion = method == 'POST' and target in (INGEST_TARGET, VERIFIED_SHARED_TARGET)
        limit = MAX_INGEST_BODY_BYTES if ingestion else MAX_BODY_BYTES
        if len(body) > limit:
            raise HostClientError('request_too_large', 'Request exceeds the service limit.', status=413)
        headers = {'Accept': 'application/json'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        if actor is not None:
            headers.update(signed_headers(self._secret, method, target, body, actor))
        connection = self._connection(self._host, self._port, timeout=self.timeout)
        try:
            connection.request(method, target, body=body if payload is not None else None,
                               headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException):
            raise HostClientError('service_unavailable', 'Selfcorpus is unavailable.',
                                  status=503, retryable=True) from None
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise HostClientError('invalid_response', 'Selfcorpus response is too large.', status=502)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, RecursionError):
            data = None
        if 200 <= status < 300 and isinstance(data, dict):
            return data
        if 400 <= status < 600 and isinstance(data, dict):
            error = data.get('error')
            if (isinstance(error, dict) and isinstance(error.get('code'), str) and
                    isinstance(error.get('message'), str) and type(error.get('retryable')) is bool):
                raise HostClientError(error['code'], error['message'], status=status,
                                      retryable=error['retryable'])
        raise HostClientError('invalid_response', 'Selfcorpus returned an unexpected response.', status=502)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if not isinstance(actor, Actor):
            raise TypeError('expected an Actor from the trusted Host')
        return actor

    def health(self) -> dict:
        return self._call('GET', '/health', actor=None)

    def submit(self, actor: Actor, spec: dict, scope_ref: str, idempotency_key: str) -> dict:
        return self._call('POST', '/ingestion/requests', actor=self._actor(actor), payload={
            'spec': spec, 'scope_ref': scope_ref, 'idempotency_key': idempotency_key})

    def submit_verified_shared(self, actor: Actor, spec: dict, idempotency_key: str) -> dict:
        """Submit an official verified original for shared publication, not manual upload."""
        return self._call('POST', VERIFIED_SHARED_TARGET[len(_PREFIX):], actor=self._actor(actor), payload={
            'spec': spec, 'idempotency_key': idempotency_key})

    def request(self, actor: Actor, ref: str) -> dict:
        return self._call('GET', f'/ingestion/requests/{_segment(ref)}', actor=self._actor(actor))

    def search(self, actor: Actor, query: str, scope: dict, filters: dict | None = None,
               limit: int = 10) -> dict:
        return self._call('POST', '/retrieval/search', actor=self._actor(actor), payload={
            'query': query, 'scope': scope, 'filters': {} if filters is None else filters, 'limit': limit})

    def document(self, actor: Actor, ref: str, chunks: bool = False) -> dict:
        route = f'/documents/{_segment(ref)}' + ('/chunks' if chunks else '')
        return self._call('GET', route, actor=self._actor(actor))

    def generation(self, actor: Actor, ref: str) -> dict:
        return self._call('GET', f'/index-generations/{_segment(ref)}', actor=self._actor(actor))

    def proposal(self, actor: Actor, request_ref: str, approve: bool | None = None) -> dict:
        payload = {'request_ref': request_ref}
        if approve is not None:
            payload['approve'] = approve
        return self._call('POST', '/publication/proposals', actor=self._actor(actor), payload=payload)

    def withdraw(self, actor: Actor, scope_ref: str, document_ref: str) -> dict:
        return self._call('POST', f'/scopes/{_segment(scope_ref)}/withdraw', actor=self._actor(actor),
                          payload={'document_ref': document_ref})

    def rollback(self, actor: Actor, scope_ref: str, generation_ref: str) -> dict:
        return self._call('POST', f'/scopes/{_segment(scope_ref)}/rollback', actor=self._actor(actor),
                          payload={'generation_ref': generation_ref})
