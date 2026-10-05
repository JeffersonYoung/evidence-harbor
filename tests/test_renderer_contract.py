"""Renderer service/proxy guards; no claim of a live Chromium sandbox test."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from deploy.renderer.app import app
from deploy.renderer.egress_proxy import handle


def test_renderer_requires_auth_and_cannot_weaken_policy(monkeypatch):
    monkeypatch.setenv("RENDERER_API_KEY", "fixture-renderer-secret")
    with TestClient(app) as client:
        payload = {"url": "https://example.org/", "required_egress_policy": "public-dns-pinned-v1"}
        assert client.post("/render", json=payload).status_code == 401
        headers = {"Authorization": "Bearer fixture-renderer-secret"}
        assert (
            client.post("/render", json={**payload, "block_websockets": False}, headers=headers).status_code
            == 422
        )
        assert (
            client.post("/render", json={**payload, "url": "http://127.0.0.1/"}, headers=headers).status_code
            == 422
        )
        assert (
            client.post(
                "/render", json={**payload, "url": "https://user:secret@example.org/"}, headers=headers
            ).status_code
            == 422
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_bytes",
    [
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n",
        b"GET http://localhost/secrets HTTP/1.1\r\n\r\n",
        b"POST http://example.org/ HTTP/1.1\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
    ],
)
async def test_proxy_rejects_private_and_non_read_requests(request_bytes):
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(request_bytes)
        await writer.drain()
        result = await asyncio.wait_for(reader.read(), timeout=3)
        writer.close()
        await writer.wait_closed()
        assert result.startswith(b"HTTP/1.1 403")


@pytest.mark.asyncio
async def test_proxy_dns_timeout_releases_connection_and_bounds_resolver_capacity(monkeypatch):
    import threading
    import time

    from deploy.renderer import egress_proxy as proxy

    released = threading.Event()
    entered = threading.Event()

    def stalled_dns(url):
        entered.set()
        released.wait(timeout=2)
        raise OSError('test DNS unavailable')

    monkeypatch.setattr(proxy, 'validate_url', stalled_dns)
    monkeypatch.setattr(proxy, 'DNS_TIMEOUT_SECONDS', 0.03)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(proxy, '_DNS_SLOTS', slots)
    monkeypatch.setattr(proxy, 'semaphore', asyncio.Semaphore(1))
    server = await asyncio.start_server(proxy.handle, '127.0.0.1', 0)
    try:
        async with server:
            started = time.monotonic()
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(b'CONNECT example.org:443 HTTP/1.1\r\n\r\n')
            await writer.drain()
            result = await asyncio.wait_for(reader.read(), timeout=1)
            writer.close()
            await writer.wait_closed()
            assert result.startswith(b'HTTP/1.1 403')
            assert entered.is_set() and time.monotonic() - started < 1
            assert not proxy.semaphore.locked()
            with pytest.raises(proxy.FetchError, match='capacity'):
                await proxy.resolve_connect('https://example.org/')
    finally:
        released.set()
        # Wait for the real resolver callback before monkeypatch restores globals.
        for _ in range(100):
            if slots.acquire(blocking=False):
                slots.release()
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail('Resolver capacity was not released')
