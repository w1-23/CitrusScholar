"""Public HTTP wire transport and retrieval policy."""
from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, unquote_plus, urljoin, urlsplit, urlunsplit

from ..errors import CorpusError
from ._robots import MIN_ORIGIN_INTERVAL, _Robots


MAX_ROBOTS_BYTES = 512 * 1024
MAX_REDIRECTS = 5
FETCH_TIMEOUT = 30.0
REQUEST_TIMEOUT = 8.0


USER_AGENT = 'SelfCorpus/1.0'
_ROBOTS_TTL = 3600.0
_MAX_ORIGINS = 2048


_AUTH_QUERY_KEYS = frozenset({'access_token', 'auth_token', 'id_token', 'api_key', 'apikey', 'jwt', 'sessionid'})


_REDIRECTS = 5
FETCH_TIMEOUT = 30.0
REQUEST_TIMEOUT = 8.0
MIN_ORIGIN_INTERVAL = 0.5
USER_AGENT = 'SelfCorpus/1.0'
_ROBOTS_TTL = 3600.0
_MAX_ORIGINS = 2048
_MEDIA = frozenset({
    'application/pdf', 'text/html', 'application/xhtml+xml', 'text/plain',
    'text/markdown', 'text/xml', 'application/xml', 'application/jats+xml',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
})
_AUTH_QUERY_KEYS = frozenset({'access_token', 'auth_token', 'id_token', 'api_key', 'apikey', 'jwt', 'sessionid'})
_MEDIA_ALIASES = {'application/x-pdf': 'application/pdf', 'text/x-markdown': 'text/markdown'}
_REDIRECTS = {301, 302, 303, 307, 308}
_DNS_WORKERS = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix='corpus-dns')
_DNS_SLOTS = threading.BoundedSemaphore(8)


_HOST = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_TLS = ssl.create_default_context()
_PUBLIC_V6 = ipaddress.IPv6Network('2000::/3')


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    content: bytes
    url: str


def _canonical_url(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) > 8192 or any(
            c == '\\' or ord(c) <= 32 or 127 <= ord(c) <= 159 for c in value
        ):
            raise ValueError
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or p.username is not None:
            raise ValueError
        if p.port not in {None, 443 if p.scheme == 'https' else 80} or not p.hostname:
            raise ValueError
        raw_host = p.hostname.removesuffix('.')
        if '%' in raw_host:
            raise ValueError
        if ':' in raw_host:
            host = '[' + str(ipaddress.IPv6Address(raw_host)) + ']'
        else:
            host = raw_host.encode('idna').decode('ascii').lower()
            if len(host) > 253 or not all(_HOST.fullmatch(label) for label in host.split('.')):
                raise ValueError
            if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.home.arpa')):
                raise ValueError
        path = quote(p.path or '/', safe="/%:@!$&'()*+,;=-._~")
        # Leave interpretation of encoded characters to the origin, but disallow
        # ambiguous traversal paths that may be normalized after robots checks.
        decoded_path = unquote(path).replace('\\', '/')
        if any(part in {'.', '..'} for part in decoded_path.split('/')) or path.startswith('//'):
            raise ValueError
        keys = {unquote_plus(piece.partition('=')[0]).lower() for piece in re.split('[&;]', p.query)}
        if any(key in _AUTH_QUERY_KEYS or key.startswith(('x-amz-', 'x-goog-')) for key in keys):
            raise ValueError
        query = quote(p.query, safe="/%?:@!$&'()*+,;=-._~")
        return urlunsplit((p.scheme, host, path, query, ''))
    except (ValueError, UnicodeError):
        raise CorpusError('invalid_url', 'Use a public HTTP(S) URL without credentials or a custom port.') from None


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise CorpusError('source_timeout', 'The source retrieval time limit was reached.', 504, True)
    return left


def _public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if ip.version == 6 and (ip not in _PUBLIC_V6 or ip.sixtofour or ip.teredo):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _resolve(host: str, port: int, deadline: float) -> list[tuple]:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _public_ip(host):
            raise CorpusError('source_target_blocked', 'Source resolves to a non-public address.', 403)
        return ([(socket.AF_INET, (host, port))] if ip.version == 4
                else [(socket.AF_INET6, (host, port, 0, 0))])
    if not _DNS_SLOTS.acquire(blocking=False):
        raise CorpusError('source_dns_busy', 'Source DNS capacity is busy. Try again later.', 503, True)
    try:
        lookup = _DNS_WORKERS.submit(socket.getaddrinfo, host + '.', port, 0, socket.SOCK_STREAM)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    lookup.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        records = lookup.result(timeout=_remaining(deadline))
    except concurrent.futures.TimeoutError:
        lookup.cancel()
        raise CorpusError('source_timeout', 'Source DNS lookup timed out.', 504, True) from None
    except OSError:
        raise CorpusError('source_dns_failed', 'Source hostname could not be resolved.', 502, True) from None
    addresses = list(dict.fromkeys((family, addr) for family, _, _, _, addr in records))
    if not addresses or any(
        f not in {socket.AF_INET, socket.AF_INET6} or not _public_ip(a[0]) or a[1] != port
        or (f == socket.AF_INET6 and a[3] != 0) for f, a in addresses
    ):
        raise CorpusError('source_target_blocked', 'Source resolves to a non-public address.', 403)
    return addresses


def _interrupt(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _request(url: str, max_bytes: int, deadline: float) -> HTTPResponse:
    """One GET without redirects or proxies. Use the validated hostname for TLS and Host."""
    p = urlsplit(url)
    host = p.hostname
    secure = p.scheme == 'https'
    port = 443 if secure else 80
    deadline = min(deadline, time.monotonic() + REQUEST_TIMEOUT)
    addresses = _resolve(host, port, deadline)
    sock = None
    timer = None
    conn = http.client.HTTPConnection(host, port)
    try:
        for index, (family, address) in enumerate(addresses[:4]):
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(_remaining(deadline) / max(1, min(4, len(addresses)) - index))
                sock.connect(address)
                break
            except OSError:
                sock.close()
                sock = None
        if sock is None:
            raise OSError('Could not connect to public address')
        sock.settimeout(_remaining(deadline))
        if secure:
            sock = _TLS.wrap_socket(sock, server_hostname=host)
        # Socket idle timeouts alone allow a slow drip to keep the response open.
        timer = threading.Timer(_remaining(deadline), _interrupt, args=(sock,))
        timer.daemon = True
        timer.start()
        conn.sock = sock
        conn.request('GET', p.path + ('?' + p.query if p.query else ''), headers={
            'Host': p.netloc, 'User-Agent': USER_AGENT, 'Accept': '*/*',
            'Accept-Encoding': 'identity', 'Connection': 'close',
        })
        with conn.getresponse() as response:
            headers = {k.lower(): v for k, v in response.getheaders()}
            body = b''
            if 200 <= response.status < 300:
                if len(response.headers.get_all('Content-Length', [])) > 1:
                    raise CorpusError('source_invalid_response', 'The source sent ambiguous length headers.', 502)
                encodings = response.headers.get_all('Content-Encoding', ['identity'])
                if any(value.strip().lower() != 'identity' for value in encodings):
                    raise CorpusError('source_encoding', 'The source did not return an uncompressed body.', 502)
                length = headers.get('content-length')
                if length is not None and (not re.fullmatch(r'[0-9]{1,18}', length) or int(length) > max_bytes):
                    raise CorpusError('source_too_large', 'The source exceeds the allowed size.', 413)
                body = response.read(max_bytes + 1)
                _remaining(deadline)
                if len(body) > max_bytes:
                    raise CorpusError('source_too_large', 'The source exceeds the allowed size.', 413)
                if length is not None and len(body) != int(length):
                    raise CorpusError('source_invalid_response', 'The source body was truncated.', 502, True)
            _remaining(deadline)
            return HTTPResponse(response.status, headers, body, url)
    except TimeoutError:
        raise CorpusError('source_timeout', 'Source HTTP request timed out.', 504, True) from None
    except ssl.SSLCertVerificationError:
        raise CorpusError('source_tls_failed', 'The source TLS certificate could not be verified.', 502) from None
    except (OSError, http.client.HTTPException, ValueError):
        _remaining(deadline)
        raise CorpusError('source_network_failed', 'Could not retrieve the source over HTTP.', 502, True) from None
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()
        if sock is not None:
            sock.close()


def _retry_delay(raw: str) -> float:
    try:
        if raw.isascii() and raw.isdigit() and len(raw) < 15:
            return max(60.0, float(raw))
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(60.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, IndexError, OverflowError):
        return 60.0


class PublicHTTP:
    """Policy wrapper. A replacement wire transport is intended only for trusted tests."""

    def __init__(self, transport=None):
        self.transport = transport or _request
        self._lock = threading.Lock()
        self._robots: dict[str, tuple[float, _Robots]] = {}
        self._robots_loading: dict[str, threading.Event] = {}
        self._requests: dict[str, float] = {}
        self._busy: set[str] = set()

    def _hop(self, url: str, max_bytes: int, deadline: float, interval: float) -> HTTPResponse:
        origin = urlsplit(url).netloc
        while True:
            with self._lock:
                now = time.monotonic()
                remaining = _remaining(deadline)
                wait = self._requests.get(origin, 0) - now
                if origin in self._busy:
                    wait = max(0.05, wait)
                if wait >= remaining:
                    raise CorpusError('source_rate_limited', 'The source origin is temporarily rate limited.', 429, True)
                if wait <= 0:
                    if len(self._requests) >= _MAX_ORIGINS:
                        self._requests = {k: v for k, v in self._requests.items() if v > now or k in self._busy}
                    if len(self._requests) >= _MAX_ORIGINS and origin not in self._requests:
                        raise CorpusError('source_rate_limited', 'Outbound source capacity is busy.', 429, True)
                    self._requests[origin] = now + interval
                    self._busy.add(origin)
                    break
            time.sleep(min(wait, remaining))
        delay = 0.0
        try:
            result = self.transport(url, max_bytes, deadline)
            if result.status == 429 or result.status == 503 and 'retry-after' in result.headers:
                delay = _retry_delay(result.headers.get('retry-after', ''))
            return result
        finally:
            with self._lock:
                self._requests[origin] = max(self._requests[origin], time.monotonic() + max(interval, delay))
                self._busy.remove(origin)

    def _get_robots(self, url: str, deadline: float) -> _Robots:
        p = urlsplit(url)
        origin = p.scheme + '://' + p.netloc
        while True:
            with self._lock:
                cached = self._robots.get(origin)
                if cached and cached[0] > time.monotonic():
                    return cached[1]
                pending = self._robots_loading.get(origin)
                if pending is None:
                    if len(self._robots_loading) >= _MAX_ORIGINS:
                        raise CorpusError('source_rate_limited', 'Outbound source capacity is busy.', 429, True)
                    self._robots_loading[origin] = threading.Event()
                    break
            if not pending.wait(_remaining(deadline)):
                raise CorpusError('source_timeout', 'Timed out waiting for the robots policy.', 504, True)
        try:
            return self._load_robots(origin, p.netloc, deadline)
        finally:
            with self._lock:
                self._robots_loading.pop(origin).set()

    def _load_robots(self, origin: str, host: str, deadline: float) -> _Robots:
        response = self._follow(origin + '/robots.txt', MAX_ROBOTS_BYTES, deadline, check_robots=False)
        if response.status in {404, 410}:
            policy = _Robots()
        elif response.status == 200:
            if response.headers.get('content-type', '').split(';')[0].strip().lower() in {'text/html', 'application/xhtml+xml'}:
                raise CorpusError('robots_unavailable', 'The source returned HTML instead of robots.txt.', 502, True)
            policy = _Robots.parse(response.content)
        elif response.status in {401, 403}:
            policy = _Robots((('/', False),))
        elif response.status == 429:
            raise CorpusError('source_rate_limited', 'Source robots.txt returned HTTP 429.', 429, True)
        else:
            raise CorpusError('robots_unavailable', f'Source robots.txt returned HTTP {response.status}.', 502, True)
        with self._lock:
            if policy.interval > MIN_ORIGIN_INTERVAL:
                self._requests[host] = max(self._requests.get(host, 0), time.monotonic() + policy.interval)
            if len(self._robots) >= _MAX_ORIGINS and origin not in self._robots:
                self._robots.pop(next(iter(self._robots)))
            self._robots[origin] = (time.monotonic() + _ROBOTS_TTL, policy)
        return policy

    def _follow(self, url: str, max_bytes: int, deadline: float, *, check_robots: bool) -> HTTPResponse:
        url = _canonical_url(url)
        seen = set()
        for _ in range(MAX_REDIRECTS + 1):
            if url in seen:
                break
            seen.add(url)
            policy = self._get_robots(url, deadline) if check_robots else _Robots()
            if not policy.allows(url):
                raise CorpusError('robots_denied', 'The source robots.txt disallows retrieval.', 403)
            response = self._hop(url, max_bytes, deadline, policy.interval)
            if response.status not in _REDIRECTS:
                return response
            location = response.headers.get('location')
            if not location:
                break
            if any(ord(c) <= 32 or 127 <= ord(c) <= 159 or c == '\\' for c in location):
                raise CorpusError('invalid_url', 'The source sent an invalid redirect URL.')
            target = _canonical_url(urljoin(url, location))
            if url.startswith('https:') and not target.startswith('https:'):
                raise CorpusError('source_insecure_redirect', 'An HTTPS source redirected to insecure HTTP.', 403)
            url = target
        raise CorpusError('source_redirects', 'The source exceeded the redirect limit or sent an invalid redirect.', 502)

    def get(self, url: str, max_bytes: int, deadline: float) -> HTTPResponse:
        response = self._follow(url, max_bytes, deadline, check_robots=True)
        if response.status == 200:
            return response
        if response.status in {401, 402, 403, 407, 451}:
            raise CorpusError('source_access_denied', f'The source denied public access (HTTP {response.status}).', 403)
        if response.status in {404, 410}:
            raise CorpusError('source_not_found', f'The source was not found (HTTP {response.status}).', 404)
        if response.status == 429:
            raise CorpusError('source_rate_limited', 'The source returned HTTP 429. Try again later.', 429, True)
        raise CorpusError('source_http_error', f'The source returned HTTP {response.status}.', 502,
                          response.status >= 500 or response.status == 408)


_DEFAULT_HTTP = PublicHTTP()
