"""Public-only DNS-pinned forward proxy for an isolated browser network.

CONNECT is allowed only to public port 443. Plain HTTP is read-only and uses the
same bounded transport as archive ingestion. Never run as an unrestricted proxy.
"""

import asyncio
import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor

from backend.security import FetchError, safe_fetch, validate_url

MAX_TUNNEL_BYTES = 32 * 1024 * 1024
semaphore = asyncio.Semaphore(16)
DNS_TIMEOUT_SECONDS = 5
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="renderer-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)


async def resolve_connect(url):
    # System DNS cannot be forcibly interrupted. Limit both running and queued
    # resolvers; timed-out clients release connection slots immediately, while
    # resolver capacity is released only when the actual system call returns.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise FetchError("Renderer DNS capacity exhausted")
    try:
        future = _DNS_POOL.submit(validate_url, url)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=DNS_TIMEOUT_SECONDS)


async def relay(reader, writer):
    transferred = 0
    while data := await asyncio.wait_for(reader.read(65536), timeout=30):
        transferred += len(data)
        if transferred > MAX_TUNNEL_BYTES:
            raise ValueError("Tunnel byte limit exceeded")
        writer.write(data)
        await writer.drain()


async def handle(reader, writer):
    remote_writer = None
    try:
        async with semaphore:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(header) > 32768:
                raise ValueError("Headers too large")
            first, *_lines = header.decode("latin1").split("\r\n")
            method, target, version = first.split(" ")
            if version not in {"HTTP/1.1", "HTTP/1.0"}:
                raise ValueError("Invalid protocol")
            if method == "CONNECT":
                # URL parsing rejects userinfo, ambiguous IPs and all non-web ports.
                vetted = await resolve_connect("https://" + target + "/")
                if vetted.port != 443 or vetted.target != "/":
                    raise ValueError("CONNECT requires port 443")
                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(vetted.addresses[0], 443), timeout=10
                )
                if ipaddress.ip_address(remote_writer.get_extra_info("peername")[0]) != ipaddress.ip_address(
                    vetted.addresses[0]
                ):
                    raise ValueError("Pinned peer mismatch")
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                tasks = [
                    asyncio.create_task(relay(reader, remote_writer)),
                    asyncio.create_task(relay(remote_reader, writer)),
                ]
                _done, pending = await asyncio.wait(tasks, timeout=35, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            elif method == "GET":
                if not target.startswith("http://"):
                    raise ValueError("Only absolute HTTP GET is supported")
                result = await asyncio.to_thread(
                    safe_fetch, target, max_bytes=16 * 1024 * 1024, timeout=25, max_redirects=0
                )
                writer.write(
                    (
                        "HTTP/1.1 200 OK\r\nContent-Type: "
                        + result.media_type
                        + "\r\nContent-Length: "
                        + str(len(result.data))
                        + "\r\nConnection: close\r\n\r\n"
                    ).encode()
                    + result.data
                )
                await writer.drain()
            else:
                raise ValueError("Method denied")
    except (
        TimeoutError,
        FetchError,
        ValueError,
        OSError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        if not writer.is_closing():
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            try:
                await writer.drain()
            except OSError:
                pass
    finally:
        if remote_writer:
            remote_writer.close()
        writer.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 3128, limit=32768)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
