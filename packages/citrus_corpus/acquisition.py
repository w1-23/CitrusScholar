"""Direct single-work acquisition with bounded, explicitly allowed destinations."""
import http.client
import ipaddress
import socket
import ssl
import subprocess
import sys
from time import monotonic
from urllib.parse import quote, urljoin, urlsplit

from .extract import source_format
from .prepare import MAX_SOURCE_BYTES, PreparationError
from .queue import target_url


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _resolve(host, timeout):
    script = ("import socket,sys; "
              "print(*{row[4][0] for row in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)},sep='\\n')")
    try:
        result = subprocess.run([sys.executable, "-I", "-c", script, host], check=True,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError from exc
    except subprocess.CalledProcessError as exc:
        raise OSError("dns_resolution_failed") from exc
    return {ipaddress.ip_address(value) for value in result.stdout.splitlines()}


def _request(url, remaining):
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or "." not in host or host.endswith((".localhost", ".local", ".internal")):
        raise PreparationError("network_scope_denied")
    addresses = _resolve(host, remaining())
    if not addresses or any(not address.is_global for address in addresses):
        raise PreparationError("network_scope_denied")
    address = min(addresses, key=lambda item: (item.version, int(item)))
    conn = _PinnedHTTPS(host, str(address), remaining())
    try:
        route = parsed.path + ("?" + parsed.query if parsed.query else "")
        conn.request("GET", route, headers={"Accept-Encoding": "identity", "User-Agent": "CitrusScholar-Corpus/1"})
        connection_socket = conn.sock
        connection_socket.settimeout(remaining())
        with conn.getresponse() as response:
            if response.status in {301, 302, 303, 307, 308}:
                return response.getheader("Location"), None, None
            if response.status != 200:
                raise PreparationError("network_unavailable" if response.status in {429, 502, 503, 504}
                                       else "source_http_denied")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise PreparationError("source_encoding_unsupported")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_SOURCE_BYTES):
                raise PreparationError("source_size_invalid")
            result = bytearray()
            while len(result) <= MAX_SOURCE_BYTES:
                connection_socket.settimeout(remaining())
                block = response.read1(min(65536, MAX_SOURCE_BYTES + 1 - len(result)))
                if not block:
                    if not result or (length is not None and int(length) != len(result)):
                        raise PreparationError("source_incomplete")
                    return None, bytes(result), response.getheader("Content-Type", "")
                result.extend(block)
            raise PreparationError("source_size_invalid")
    finally:
        conn.close()


class ExplicitAcquirer:
    def __init__(self, redirect_hosts=(), *, transport=_request, allowed=lambda: True):
        self.redirect_hosts = frozenset(str(host).lower() for host in redirect_hosts)
        self.transport = transport
        self.allowed = allowed

    def __call__(self, request):
        if request.get("kind") != "explicit":
            raise PreparationError("network_scope_denied")
        doi = request.get("doi", "")
        start = target_url(request.get("url") or "https://doi.org/" + quote(doi, safe="/"))
        current = start
        issued = {start}
        deadline = monotonic() + 20

        def remaining():
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise TimeoutError
            return min(4, seconds)

        try:
            for _ in range(3):
                if not self.allowed() or urlsplit(current).scheme != "https":
                    raise PreparationError("network_scope_denied")
                location, data, media = self.transport(current, remaining)
                if location is None:
                    if not self.allowed():
                        raise PreparationError("network_scope_denied")
                    if data is None:
                        raise PreparationError("source_redirect_denied")
                    return data, source_format(urlsplit(current).path, media)
                target = target_url(urljoin(current, location))
                host = urlsplit(target).hostname
                if (target in issued or host in {"doi.org", "dx.doi.org"}
                        or host not in self.redirect_hosts):
                    raise PreparationError("source_redirect_denied")
                issued.add(target)
                current = target
        except (TimeoutError, socket.timeout) as exc:
            raise PreparationError("network_timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise PreparationError("network_unavailable") from exc
        raise PreparationError("source_redirect_denied")
