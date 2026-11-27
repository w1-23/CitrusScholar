"""Trusted Host identity and the Selfcorpus HMAC wire contract.

Sign exact origin-form request target (including any query) and body bytes.
Only the trusted Host may turn authenticated session claims into an Actor.
This module is standard-library only and can be reused without the HTTP server.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

TIMESTAMP_HEADER = 'X-Corpus-Timestamp'
CLAIMS_HEADER = 'X-Corpus-Claims'
SIGNATURE_HEADER = 'X-Corpus-Signature'
MAX_CLOCK_SKEW_SECONDS = 60
MAX_CLAIMS_HEADER = 8192

# Shared wire limits. Ingestion allows the Sources 16 MiB decoded upload cap
# plus Base64 expansion and JSON metadata. Other routes use the smaller cap.
DEFAULT_PORT = 8061
MAX_BODY_BYTES = 256 * 1024
MAX_INGEST_BODY_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
INGEST_TARGET = '/api/v1/ingestion/requests'
VERIFIED_SHARED_TARGET = '/api/v1/ingestion/verified-shared'
BASE_SOURCES = ('cqa_literature', 'cqa_methods', 'cqa_deng_book', 'cqa_translated', 'cqa_ucr_varieties')


class AuthenticationError(ValueError):
    """Invalid, missing, or expired trusted Host credentials."""


_REFERENCE = re.compile(r'[A-Za-z0-9._@-]{1,128}')


def _reference(value: object) -> bool:
    return isinstance(value, str) and _REFERENCE.fullmatch(value) is not None


@dataclass(frozen=True)
class Actor:
    subject: str
    projects: Mapping[str, str] = field(default_factory=dict)
    shared: bool = True
    governor: bool = False
    base_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _reference(self.subject) or not isinstance(self.projects, Mapping):
            raise ValueError('invalid actor identity')
        if type(self.shared) is not bool or type(self.governor) is not bool:
            raise ValueError('invalid actor access flag')
        projects = dict(self.projects)
        if not all(_reference(ref) and access in ('read', 'write')
                   for ref, access in projects.items()):
            raise ValueError('invalid project claims')
        object.__setattr__(self, 'projects', MappingProxyType(projects))
        sources = self.base_sources
        if (not isinstance(sources, (tuple, list)) or
                any(not isinstance(s, str) or s not in BASE_SOURCES for s in sources) or
                len(set(sources)) != len(sources)):
            raise ValueError('invalid base source grants')
        object.__setattr__(self, 'base_sources', tuple(sorted(sources)))

    def can_read(self, scope_ref: str) -> bool:
        if scope_ref == 'shared':
            return self.shared
        if scope_ref == f'personal:{self.subject}':
            return True
        return (scope_ref.startswith('project:') and
                self.projects.get(scope_ref[len('project:'):]) in ('read', 'write'))

    def can_write(self, scope_ref: str) -> bool:
        if scope_ref == 'shared':
            return self.shared and self.governor
        if scope_ref == f'personal:{self.subject}':
            return True
        return (scope_ref.startswith('project:') and
                self.projects.get(scope_ref[len('project:'):]) == 'write')


def secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode('utf-8')
    if not isinstance(secret, bytes) or not secret:
        raise ValueError('Selfcorpus HMAC secret is required')
    return secret


def _message(method: str, target: str, body: bytes | bytearray, timestamp: str, claims: str) -> bytes:
    if not re.fullmatch(r'[A-Z]+', method) or not target.startswith('/') or not all(
            33 <= ord(character) <= 126 for character in target):
        raise ValueError('invalid HTTP method or target')
    return '\n'.join(('selfcorpus-hmac-v1', method, target, timestamp, claims,
                      hashlib.sha256(body).hexdigest())).encode('ascii')


def signed_headers(secret: str | bytes, method: str, target: str, body: bytes, actor: Actor,
                   *, now: float | None = None) -> dict[str, str]:
    """Return new HMAC headers for an already authenticated Host actor."""
    if not isinstance(actor, Actor):
        raise TypeError('expected an Actor from the trusted Host')
    timestamp = str(int(time.time() if now is None else now))
    data = {'subject': actor.subject, 'projects': dict(actor.projects),
            'shared': actor.shared, 'governor': actor.governor}
    if actor.base_sources:
        data['base_sources'] = actor.base_sources
    raw_claims = json.dumps(data, sort_keys=True, separators=(',', ':')).encode('ascii')
    claims = base64.urlsafe_b64encode(raw_claims).rstrip(b'=').decode('ascii')
    if len(claims) > MAX_CLAIMS_HEADER:
        raise ValueError('too many actor claims')
    signature = hmac.digest(secret_bytes(secret), _message(method, target, body, timestamp, claims),
                            'sha256').hex()
    return {TIMESTAMP_HEADER: timestamp, CLAIMS_HEADER: claims, SIGNATURE_HEADER: signature}


def _single_header(headers, name: str) -> str:
    if hasattr(headers, 'get_all'):
        values = headers.get_all(name, [])
    else:
        values = [value for key, value in headers.items() if key.lower() == name.lower()]
    if len(values) != 1 or not isinstance(values[0], str):
        raise AuthenticationError('invalid Host authentication')
    return values[0]


def verify_signed_actor(secret: str | bytes, method: str, target: str, body: bytes | bytearray,
                        headers, *, now: float | None = None) -> Actor:
    """Authenticate exact bytes before decoding signed Host claims."""
    key = secret_bytes(secret)
    timestamp = _single_header(headers, TIMESTAMP_HEADER)
    claims = _single_header(headers, CLAIMS_HEADER)
    signature = _single_header(headers, SIGNATURE_HEADER)
    try:
        if (not re.fullmatch(r'[0-9]{1,12}', timestamp) or
                not 0 < len(claims) <= MAX_CLAIMS_HEADER or
                not re.fullmatch(r'[A-Za-z0-9_-]+', claims) or
                not re.fullmatch(r'[0-9a-f]{64}', signature)):
            raise ValueError('invalid credential format')
        if abs(int(time.time() if now is None else now) - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
            raise ValueError('expired credential')
        expected = hmac.digest(key, _message(method, target, body, timestamp, claims), 'sha256').hex()
        if not hmac.compare_digest(expected, signature):
            raise ValueError('invalid signature')
        data = json.loads(base64.b64decode(claims + '=' * (-len(claims) % 4), altchars=b'-_',
                                           validate=True))
        required = {'subject', 'projects', 'shared', 'governor'}
        if (not isinstance(data, dict) or not required <= data.keys() or
                data.keys() - required - {'base_sources'}):
            raise ValueError('invalid actor claims')
        return Actor(**data)
    except (ValueError, TypeError, binascii.Error, RecursionError):
        raise AuthenticationError('invalid Host authentication') from None
