# EvidenceHarbor Web / 证据港前端

Real Next.js 16 / React 19 / TypeScript application. All project content is read from the FastAPI service through a same-origin server proxy; there are no hard-coded project records or simulated operation statuses.

## Run locally

```bash
npm ci
cp .env.example .env.local
# Start the Python API separately, normally on 127.0.0.1:8000.
npm run dev
```

Open `http://127.0.0.1:3000`. Create a password account with the repository's operator-only account command (`python -m backend.auth`) before signing in. The UI has no public registration and no default credentials.

Production verification:

```bash
npm run typecheck
npm test
npm run build
npm start
```

The provided Dockerfile uses Next standalone output and a non-root runtime. Bind the Compose host port to `127.0.0.1:3000:3000` for private operation. Behind a properly configured HTTPS gateway, enforce your network access policy and set forwarded protocol correctly so session cookies are Secure. This application is not a public unauthenticated proxy.

## Authentication and deployment boundary

- Browser calls are only to `/api/*`; Next forwards to the configured `API_BASE_URL` at `/v1/*`.
- `/api/auth/login` consumes the backend access token server-side and sets a `HttpOnly; SameSite=Strict` session cookie. Its JSON response never exposes the token.
- Cookie sessions take precedence over any operator credential. By default, anonymous and signed-out requests to protected resources receive 401 even if an `API_TOKEN` environment variable exists.
- `SINGLE_USER_MODE=true` explicitly enables the server-only `API_TOKEN` fallback. Use only for a deliberate private loopback deployment. It must never be enabled as a public or multi-user default.
- No `NEXT_PUBLIC_API_TOKEN` exists. Client components do not import server credentials.
- Writes reject cross-origin `Origin` headers and cross-site `Sec-Fetch-Site`. Proxy routes are allowlisted and redirects are rejected.
- Backend roles remain authoritative. Write denial is presented as a clear 403 permission message; the UI does not pretend permission has been granted.
- Source/report HTML is never interpreted. Content is rendered as React text with a tiny structural renderer; there is no `dangerouslySetInnerHTML`, iframe, or raw document script execution.

## Implemented user workflows

- Password sign-in, sign-out, role display and permission-denial states
- Project creation and navigation, responsive sidebar, keyboard labels and native modal focus management
- URL, text, and file ingestion with idempotency keys, source classification (public/internal/sensitive), operation polling, failure details, retry, and saved pipeline selection for URL/text ingestion
- Project library and debounced full-text search, original document reader, stable block navigation, exact-quote capture
- Versioned report reader, original-file downloads, and editable manual heading locks
- Question capture and local or explicitly configured external research with model, reasoning, document/search/tool/time/output/cost limits
- Evidence-backed manual proposals, scope/limitations, per-claim acceptance, supporting/contradicting/limiting evidence labels
- Review-and-publish with compare-and-swap version conflict handling, protected-section conflict handling, and no automatic overwrites
- Run/event history, downloadable ZIP project exports
- Live diagnostics, declarative YAML/JSON pipeline revisions, registry inspection, original-capture sample runs with quality/gate/warning and text-diff output
- HTTP/RSS/Atom/Sitemap source watch configuration, bounded feed item/domain limits, pause/resume, interval editing, optional continuous local research, manual enqueue, and explicit distinction between saved config and synchronized Temporal scheduling

The UI defaults to local evidence compilation, not external AI. External provider calls require the server operator's enablement, a nonzero explicit budget, and a visible consent checkbox. No model credential is entered or saved through the UI.

## Verification

`npm test` runs twenty-one component-rendering and security-invariant checks. `npm run typecheck` and `npm run build` validate the actual production application.

The real HTTP integration suite launches a throwaway SQLite backend plus the production Next server in a single network namespace, creates isolated admin/reader accounts with a random secret, exercises all calls through the Next proxy, and writes `test-results/proxy-integration.json`:

```bash
npm run build
bash scripts/integration.sh
```

It covers anonymous/login/logout boundaries, HttpOnly cookies, cross-origin rejection, permission denial, project creation, text/file ingestion, idempotency, operations, search, verified evidence, proposal publication, CAS conflicts, historical versions, locks, partial acceptance, research events, export and diagnostics. Temporary test data are created under `/tmp`; production data are never used.

A Playwright browser suite is also supplied. Install its bundled browser with `npx playwright install --with-deps chromium`; omit `CHROMIUM_PATH` to use that browser, or specify an existing official Chromium installation. Failure screenshots and a Playwright trace are retained in `test-results/`:

```bash
RUN_BROWSER_TESTS=true CHROMIUM_PATH=/usr/bin/chromium bash scripts/integration.sh
```

This adds UI flows, real DOM assertions, browser runtime-error detection, exact-quote highlighting, mobile overflow/navigation checks and screenshots. In the initial managed execution environment Chromium could not create the required process socket and the cloud browser blocked loopback URLs. Therefore browser rendering and screenshots were **not verified there**; the browser suite is not represented as passed. It should be run in CI or a supported local Chromium environment before exposing this build to users.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `API_BASE_URL` | `http://127.0.0.1:8000` | FastAPI origin, server-only |
| `SINGLE_USER_MODE` | `false` | Explicit private operator-token mode |
| `API_TOKEN` | unset | Used only with explicit single-user mode |
| `PORT` | `3000` | Standalone Docker runtime port |

No remote font, image CDN, or other browser-side third-party asset is required. The Chinese editorial type stack falls back to the device's installed serif font.

See `VALIDATION.md` for exact passed checks and environment-limited stages. Offline-safe source-watch configuration is covered by the HTTP suite; live fetching and scheduled execution are not claimed as verified.

Optional research web discovery is available through the backend API; the current research form does not expose web-discovery/search-provider controls. External model selection in the UI should not be confused with external source discovery.

### Scholarly leads and retained failed originals

The library has separate saved-document and scholarly-lead views. Leads are paginated; metadata-only, abstract-only and fulltext-available states remain distinct from completion of reading. The detail inspector shows current reviewed metadata, original provider observations and immutable correction history. Editors/admins can submit source-backed corrections with conflict protection; readers retain inspection access without correction controls.

A failed parse with an archived capture offers original inspection and download in activity history. The absence of a research document does not hide retained raw bytes, and downloading an original does not mark it read or evidence-ready.
