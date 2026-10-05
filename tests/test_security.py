"""SSRF regressions run real URL and transport policy against deterministic peers."""
import io
import socket

import pytest

from backend import security


def dns(*addresses):
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM,
                 socket.IPPROTO_TCP, '', (address, port)) for address in addresses]
    return resolve


@pytest.mark.parametrize('address', [
    '127.0.0.1', '0.0.0.0', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
    '100.64.0.1', '192.0.2.1', '198.51.100.1', '203.0.113.1', '224.0.0.1', '240.0.0.1',
    '::1', '::', 'fc00::1', 'fe80::1', 'ff02::1', '::ffff:127.0.0.1', '::ffff:10.0.0.1',
])
def test_non_public_ip_denied(address):
    assert security.is_public_address(address) is False
    host = f'[{address}]' if ':' in address else address
    with pytest.raises(security.UnsafeURLError):
        security.validate_url(f'http://{host}/')


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'ftp://example.com/data', 'gopher://example.com/',
    'http://localhost/', 'http://a.localhost/', 'http://database.internal/',
    'http://host.local/', 'http://host.test/', 'http://host.invalid/',
    'http://metadata/', 'http://user:password@example.com/', 'http://example.com:22/',
    'http://example.com/path\r\nX-Header: bad', 'http://example.com\\@127.0.0.1/',
    'http://[fe80::1%25eth0]/',
])
def test_unsafe_url_form_denied_without_network(url):
    with pytest.raises(security.UnsafeURLError):
        security.validate_url(url, resolver=lambda *a, **kw: pytest.fail('Unsafe URL reached DNS'))


@pytest.mark.parametrize('addresses', [('127.0.0.1',), ('93.184.216.34', '10.0.0.1'),
                                        ('2606:4700:4700::1111', '::1')])
def test_dns_private_or_mixed_public_private_answer_denied(addresses):
    with pytest.raises(security.UnsafeURLError):
        security.validate_url('https://research.example.org/report', resolver=dns(*addresses))


def test_url_normalization_and_exact_vetted_addresses():
    vetted = security.validate_url('https://Research.Example.org:443/report?q=policy#footnote',
                                  resolver=dns('93.184.216.34'))
    assert vetted.host == 'research.example.org'
    assert vetted.addresses == ('93.184.216.34',)
    assert vetted.url == 'https://research.example.org/report?q=policy'
    assert vetted.target == '/report?q=policy'


class Response(io.BytesIO):
    def __init__(self, body=b'', status=200, headers=()):
        super().__init__(body)
        self.status, self.headers = status, headers

    def getheaders(self):
        return list(self.headers)


@pytest.fixture
def transport(monkeypatch):
    responses, connections, requests = [], [], []

    class Connection:
        sock = None

        def __init__(self, vetted, address, timeout):
            connections.append((vetted, address))

        def request(self, *args, **kwargs):
            requests.append((args, kwargs))

        def getresponse(self):
            assert responses, 'Unexpected network request'
            return responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(security, '_PinnedHTTPConnection', Connection)
    monkeypatch.setattr(security.socket, 'getaddrinfo', dns('93.184.216.34'))
    return responses, connections, requests


@pytest.mark.parametrize('destination', ['http://127.0.0.1/admin', 'http://169.254.169.254/latest/meta-data/',
                                         'http://[::1]/private'])
def test_redirect_cannot_reach_private_host(transport, destination):
    responses, connections, _ = transport
    responses.append(Response(status=302, headers=[('Location', destination)]))
    with pytest.raises(security.UnsafeURLError):
        security.safe_fetch('http://research.example.org/')
    assert len(connections) == 1


def test_redirect_dns_is_revalidated(transport, monkeypatch):
    responses, connections, _ = transport
    calls = []

    def rebinding_dns(host, port, **kwargs):
        calls.append(host)
        return dns('93.184.216.34' if len(calls) == 1 else '127.0.0.1')(host, port)

    monkeypatch.setattr(security.socket, 'getaddrinfo', rebinding_dns)
    responses.append(Response(status=302, headers=[('Location', '/next')]))
    with pytest.raises(security.UnsafeURLError):
        security.safe_fetch('http://research.example.org/')
    assert calls == ['research.example.org', 'research.example.org']
    assert len(connections) == 1


def test_public_relative_redirect_and_raw_payload_succeed(transport):
    responses, connections, requests = transport
    payload = b'<article>Preserved evidence</article>'
    responses.extend([Response(status=302, headers=[('Location', '/paper')]),
                      Response(payload, headers=[('Content-Type', 'text/html; charset=utf-8')])])
    result = security.safe_fetch('https://research.example.org/')
    assert result.data == payload
    assert result.url == 'https://research.example.org/paper'
    assert result.media_type == 'text/html'
    assert [address for _, address in connections] == ['93.184.216.34', '93.184.216.34']
    assert requests[1][0] == ('GET', '/paper')


@pytest.mark.parametrize('body,headers', [(b'12345', []), (b'', [('Content-Length', '5')])])
def test_fetch_enforces_declared_and_streamed_size_limits(transport, body, headers):
    transport[0].append(Response(body, headers=headers))
    with pytest.raises(security.FetchError, match='size limit'):
        security.safe_fetch('https://research.example.org/', max_bytes=4)


def test_fetch_rejects_unsupported_compression(transport):
    transport[0].append(Response(b'compressed', headers=[('Content-Encoding', 'gzip')]))
    with pytest.raises(security.FetchError, match='Compressed'):
        security.safe_fetch('https://research.example.org/')


def test_https_cannot_downgrade(transport):
    transport[0].append(Response(status=302, headers=[('Location', 'http://research.example.org/plain')]))
    with pytest.raises(security.UnsafeURLError, match='downgrade'):
        security.safe_fetch('https://research.example.org/')


def test_authenticated_request_never_forwards_credentials_on_redirect(transport):
    transport[0].append(Response(status=302, headers=[('Location', 'https://other.example.org/')]))
    with pytest.raises(security.UnsafeURLError, match='Authenticated redirects'):
        security.safe_fetch('https://research.example.org/', headers={'Authorization': 'Bearer test-token'})
    assert len(transport[1]) == 1


@pytest.mark.parametrize('header', ['Host', 'Cookie', 'Proxy-Authorization', 'Transfer-Encoding'])
def test_caller_cannot_override_security_transport_headers(transport, header):
    with pytest.raises(ValueError, match='Unsafe'):
        security.safe_fetch('https://research.example.org/', headers={header: 'unsafe'})
    assert transport[1] == []


def test_transport_connects_to_numeric_vetted_address(monkeypatch):
    vetted = security.validate_url('http://research.example.org/', resolver=dns('93.184.216.34'))
    targets = []

    class Socket:
        def getpeername(self):
            return ('93.184.216.34', 80)

    def connect(target, timeout):
        targets.append(target)
        return Socket()

    monkeypatch.setattr(security.socket, 'create_connection', connect)
    connection = security._PinnedHTTPConnection(vetted, vetted.addresses[0], 2)
    connection.connect()
    assert targets == [('93.184.216.34', 80)]


def test_transport_rejects_peer_mismatch(monkeypatch):
    vetted = security.validate_url('http://research.example.org/', resolver=dns('93.184.216.34'))
    closed = []

    class Socket:
        def getpeername(self):
            return ('127.0.0.1', 80)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(security.socket, 'create_connection', lambda *args: Socket())
    connection = security._PinnedHTTPConnection(vetted, vetted.addresses[0], 2)
    with pytest.raises(security.UnsafeURLError, match='peer'):
        connection.connect()
    assert closed == [True]


def test_browser_renderer_fails_closed():
    with pytest.raises(security.FetchError, match='isolated-egress'):
        security.render_with_playwright('https://research.example.org/')
