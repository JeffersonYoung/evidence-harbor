"""Optional Playwright renderer. Requires the isolated Compose network below."""

import asyncio
import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from backend.security import validate_source_config_url

app = FastAPI(docs_url=None, redoc_url=None)
slots = asyncio.Semaphore(2)


class RenderInput(BaseModel):
    url: str
    required_egress_policy: str
    initial_vetted_addresses: list[str] = Field(default_factory=list)
    max_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=25 * 1024 * 1024)
    timeout_seconds: int = Field(default=30, ge=1, le=30)
    block_service_workers: bool = True
    block_websockets: bool = True


@app.post("/render")
async def render(payload: RenderInput, authorization: str = Header(default="")):
    key = os.getenv("RENDERER_API_KEY", "")
    if not key or not hmac.compare_digest(authorization, "Bearer " + key):
        raise HTTPException(401, "Unauthorized")
    if (
        payload.required_egress_policy != "public-dns-pinned-v1"
        or not payload.block_service_workers
        or not payload.block_websockets
    ):
        raise HTTPException(422, "Required egress policy cannot be weakened")
    try:
        url = validate_source_config_url(payload.url)
    except ValueError as exc:
        raise HTTPException(422, "Invalid source URL") from exc
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    async with slots:
        try:
            async with asyncio.timeout(payload.timeout_seconds + 5), async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    chromium_sandbox=True,
                    proxy={"server": "http://egress:3128", "bypass": ""},
                    args=[
                        "--disable-quic",
                        "--disable-dev-shm-usage",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                )
                try:
                    context = await browser.new_context(
                        service_workers="block",
                        accept_downloads=False,
                        ignore_https_errors=False,
                        java_script_enabled=True,
                    )
                    page = await context.new_page()
                    requests = 0

                    async def route(request_route):
                        nonlocal requests
                        requests += 1
                        target = request_route.request.url
                        if requests > 80 or not target.startswith(("http://", "https://")):
                            await request_route.abort()
                            return
                        try:
                            validate_source_config_url(target)
                        except ValueError:
                            await request_route.abort()
                            return
                        await request_route.continue_()

                    await context.route("**/*", route)
                    await context.route_web_socket("**/*", lambda socket: socket.close())
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=payload.timeout_seconds * 1000
                    )
                    await page.wait_for_timeout(750)
                    content = (await page.content()).encode()
                    if len(content) > payload.max_bytes:
                        raise HTTPException(413, "Rendered DOM exceeds limit")
                finally:
                    await browser.close()
        except (PlaywrightError, TimeoutError) as exc:
            raise HTTPException(502, "Isolated browser failed or timed out") from exc
    return Response(content, media_type="text/html", headers={"X-Egress-Policy": "public-dns-pinned-v1"})
