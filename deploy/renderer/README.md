# Optional isolated renderer service

This is a concrete Playwright HTTP service and public-only DNS-pinning forward proxy, not a browser embedded in the API. It is optional and has not been Docker/browser deployment-tested in the authoring environment.

Build from this directory with `docker compose up --build`. Supply a renderer-specific RENDERER_API_KEY through the deployment secret mechanism. Put a trusted TLS reverse proxy in front of the loopback-only gateway port8088, then configure the main API's ISOLATED_RENDERER_URL=https://your-reviewed-host/render, ISOLATED_RENDERER_API_KEY and ISOLATED_RENDERER_EGRESS_ATTESTED=true. The main client refuses insecure/self-signed HTTPS rather than disabling certificate checks.

The browser container is attached only to an internal Docker network. Only the egress proxy has internet access; it validates and pins public CONNECT destinations. The ingress gateway only proxies authenticated /render. Browser requests/subresources are bounded, WebSockets/service workers/downloads are blocked, ephemeral browser contexts have no saved user credentials, and the container is non-root with memory/PID limits. The service requires Chromium's sandbox; it does not fall back to --no-sandbox if host user namespaces/seccomp disallow it.

Before attesting, validate in your actual Docker/network environment: browser direct egress fails, loopback/private/cloud metadata redirects fail, DNS rebinding fails, service-worker/WebSocket and malicious subresource attempts fail, valid public HTTPS renders, output/CPU/memory/time limits hold, and the gateway is not publicly exposed without trusted TLS/rate limits. Do not add host networking, privileged mode, SYS_ADMIN or an unrestricted network to make the browser work. Official Playwright images are development bases, not a security guarantee for hostile websites; a hardened production browser sandbox requires operator review.

Sources: [Playwright Docker](https://playwright.dev/python/docs/docker), [browser contexts](https://playwright.dev/python/docs/browser-contexts).
