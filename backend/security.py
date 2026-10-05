"""Public-web-only HTTP transport with DNS pinning and redirect revalidation.

No proxy environment variables, browser credentials, or cookies are used. Every
resolved address must be globally routable. The exact vetted IP is used for the
TCP connection, while TLS verifies the original hostname (avoids DNS rebinding).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

# A fixed-size resolver pool bounds concurrency and wall time even if system DNS stalls.
_DNS_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="public-url-dns")


class UnsafeURLError(ValueError):
    pass


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class VettedURL:
    url: str
    scheme: str
    host: str
    port: int
    target: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class FetchResult:
    data: bytes
    url: str
    media_type: str
    status: int
    headers: dict[str, str]

    @property
    def content(self) -> bytes:
        return self.data


def is_public_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not (parsed.is_multicast or parsed.is_reserved or parsed.is_loopback
                                    or parsed.is_link_local or parsed.is_unspecified)


def _url_syntax(url: str) -> VettedURL:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise UnsafeURLError("A URL of at most 8192 characters is required")
    if any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise UnsafeURLError("URL contains control characters, whitespace, or backslashes")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise UnsafeURLError("Only HTTP and HTTPS URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeURLError("URL credentials are forbidden")
        if not parsed.hostname or "%" in parsed.hostname:
            raise UnsafeURLError("URL hostname is invalid")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURLError("Malformed URL") from exc
    if port not in {80, 443}:
        raise UnsafeURLError("Only public web ports 80 and 443 are allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".test", ".invalid")):
        raise UnsafeURLError("Local and reserved hostnames are forbidden")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal:
        addresses = (str(literal),)
        if not is_public_address(str(literal)):
            raise UnsafeURLError("Destination is a private, reserved, or non-public address")
    else:
        if "." not in host:
            raise UnsafeURLError("Single-label hostnames are forbidden")
        # Numeric dotted forms that are not canonical IP literals are ambiguous
        # across resolvers (e.g. octal IPv4), so reject rather than normalize them.
        if re.fullmatch(r"[0-9.]+", host):
            raise UnsafeURLError("Noncanonical numeric IP address is forbidden")
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise UnsafeURLError("Noncanonical numeric IP address is forbidden")
        addresses = ()
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = display_host + (f":{port}" if port != default_port else "")
    target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    # http.client must receive an ASCII request target; percent encoding belongs to caller.
    try:
        target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise UnsafeURLError("Non-ASCII URL paths must be percent encoded") from exc
    normalized = urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))
    return VettedURL(normalized, parsed.scheme.lower(), host, port, target, addresses)


def validate_source_config_url(url: str) -> str:
    """Offline configuration validation only; this does not authorize a network fetch.

    Obvious private/reserved literals and unsafe syntax are rejected without DNS.
    Runtime safe_fetch still resolves every address and pins a vetted public peer.
    """
    return _url_syntax(url).url


def validate_url(url: str, *, resolver=None) -> VettedURL:
    syntax = _url_syntax(url)
    addresses = syntax.addresses
    if not addresses:
        try:
            results = (resolver or socket.getaddrinfo)(syntax.host, syntax.port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise FetchError("URL hostname could not be resolved") from exc
        addresses = tuple(dict.fromkeys(row[4][0] for row in results))
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise UnsafeURLError("Destination resolves to a private, reserved, or non-public address")
    return VettedURL(syntax.url, syntax.scheme, syntax.host, syntax.port, syntax.target, addresses)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, vetted: VettedURL, address: str, timeout: float):
        super().__init__(vetted.host, vetted.port, timeout=timeout)
        self.vetted, self.address = vetted, address

    def connect(self):
        # address is numeric, so this does not re-resolve the requested hostname.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        self.transport_socket = self.sock
        peer = self.sock.getpeername()[0]
        if ipaddress.ip_address(peer) != ipaddress.ip_address(self.address) or not is_public_address(peer):
            self.sock.close()
            raise UnsafeURLError("Connected peer does not match vetted public address")
        if self.vetted.scheme == "https":
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.vetted.host)
            self.transport_socket = self.sock


def safe_fetch(url: str, *, max_bytes: int = 25 * 1024 * 1024, timeout: float = 20,
               max_redirects: int = 4, method: str = "GET", body: bytes | None = None,
               headers: Mapping[str, str] | None = None) -> FetchResult:
    if method not in {"GET", "POST"}:
        raise ValueError("Unsupported request method")
    if not (0 < max_bytes <= 100 * 1024 * 1024) or not (0 < timeout <= 120):
        raise ValueError("Fetch bounds are invalid")
    if method == "POST" and max_redirects:
        # Secret-bearing provider requests must never be forwarded to another origin.
        max_redirects = 0
    deadline = time.monotonic() + timeout
    request_headers = {"User-Agent": "EvidenceWorkspace/1.0", "Accept-Encoding": "identity", "Accept": "*/*"}
    for name, value in (headers or {}).items():
        if name.lower() in {"host", "cookie", "connection", "proxy-authorization", "transfer-encoding"}:
            raise ValueError("Unsafe caller-supplied transport header")
        if "\r" in name + value or "\n" in name + value:
            raise ValueError("Invalid transport header")
        request_headers[name] = value
    current = url
    for redirect in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("Request timed out")
        resolution = _DNS_POOL.submit(validate_url, current)
        try:
            vetted = resolution.result(timeout=remaining)
        except FutureTimeout as exc:
            resolution.cancel()
            raise FetchError("URL resolution exceeded the request time limit") from exc
        if time.monotonic() >= deadline:
            raise FetchError("Request timed out")
        connection = None
        response = None
        watchdog = None
        expired = threading.Event()
        try:
            connection = _PinnedHTTPConnection(vetted, vetted.addresses[0], max(0.1, deadline - time.monotonic()))
            def expire_transport(connection=connection, expired=expired):
                expired.set()
                transport = getattr(connection, "transport_socket", None) or getattr(connection, "sock", None)
                if transport is not None:
                    try:
                        transport.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    transport.close()
            # A watchdog also bounds TLS/header slowloris traffic, where per-read
            # socket timeouts alone can be reset indefinitely by a trickle of bytes.
            watchdog = threading.Timer(max(0.01, deadline - time.monotonic()), expire_transport)
            watchdog.daemon = True
            watchdog.start()
            connection.request(method, vetted.target, body=body, headers=request_headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                location = response_headers.get("location")
                if redirect == max_redirects or not location:
                    raise FetchError("Redirect limit exceeded or missing redirect location")
                following = urljoin(vetted.url, location)
                if vetted.scheme == "https" and urlsplit(following).scheme != "https":
                    raise UnsafeURLError("HTTPS downgrade redirect is forbidden")
                # Do not carry authorization to redirected sites even for GET adapters.
                if any(name.lower() == "authorization" for name in request_headers):
                    raise UnsafeURLError("Authenticated redirects are forbidden")
                current = following
                continue
            if not 200 <= response.status < 300:
                raise FetchError(f"Remote server returned HTTP {response.status}")
            encoding = response_headers.get("content-encoding", "identity").lower()
            if encoding not in {"", "identity"}:
                raise FetchError("Compressed transfer content is unsupported; identity encoding was requested")
            length = response_headers.get("content-length")
            if length:
                try:
                    declared_size = int(length)
                except ValueError as exc:
                    raise FetchError("Invalid response Content-Length") from exc
                if declared_size < 0 or declared_size > max_bytes:
                    raise FetchError("Response exceeds the size limit")
            chunks, count = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError("Request timed out")
                if connection.sock:
                    connection.sock.settimeout(max(0.1, remaining))
                reader = getattr(response, "read1", response.read)
                chunk = reader(min(64 * 1024, max_bytes - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > max_bytes:
                    raise FetchError("Response exceeds the size limit")
                chunks.append(chunk)
            if expired.is_set() or time.monotonic() > deadline:
                raise FetchError("Request timed out")
            return FetchResult(b"".join(chunks), vetted.url,
                               response_headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower(),
                               response.status, response_headers)
        except (UnsafeURLError, FetchError):
            raise
        except (OSError, http.client.HTTPException) as exc:
            if expired.is_set():
                raise FetchError("Request timed out") from exc
            raise FetchError("Public-web request failed") from exc
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
    raise FetchError("Redirect limit exceeded")


# Stable convenience name for callers.
fetch_url = safe_fetch


def render_with_playwright(url: str) -> bytes:
    """Call an explicitly configured, audited isolated-egress rendering service.

    Never launches a browser in this process. The separate service must run
    Playwright with all traffic behind a public-only DNS-pinning egress proxy,
    no ambient network or credentials, blocked WebSockets and service workers,
    and bounded runtime/output. Attestation is an operator deployment assertion,
    not something this application can independently establish.
    """
    endpoint = os.environ.get("ISOLATED_RENDERER_URL", "").strip()
    credential = os.environ.get("ISOLATED_RENDERER_API_KEY", "").strip()
    attested = os.environ.get("ISOLATED_RENDERER_EGRESS_ATTESTED", "").lower() in {"1", "true", "yes"}
    if not endpoint or not credential or not attested:
        raise FetchError("Browser rendering is disabled: an audited isolated-egress renderer is required")
    if urlsplit(endpoint).scheme != "https":
        raise FetchError("Isolated renderer must use an HTTPS endpoint")
    vetted = validate_url(url)
    payload = {"url": vetted.url, "initial_vetted_addresses": list(vetted.addresses),
               "required_egress_policy": "public-dns-pinned-v1", "max_bytes": 25 * 1024 * 1024,
               "timeout_seconds": 30, "block_service_workers": True, "block_websockets": True}
    result = safe_fetch(endpoint, method="POST", max_redirects=0, timeout=40,
                        headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json", "Accept": "text/html"},
                        body=json.dumps(payload).encode("utf-8"), max_bytes=25 * 1024 * 1024)
    if result.headers.get("x-egress-policy") != "public-dns-pinned-v1" or result.media_type not in {"text/html", "application/xhtml+xml"}:
        raise FetchError("Renderer did not confirm the required isolated-egress contract")
    return result.data
