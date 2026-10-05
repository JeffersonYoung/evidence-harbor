# Frontend validation

Validated on 2026-10-04 UTC against the current local FastAPI implementation, using throwaway SQLite data and admin/reader test accounts. No production credentials or records were used.

## Passed

- `npm run typecheck`: strict TypeScript check
- `npm run build`: Next.js 16.3.8 optimized standalone build
- `npm test`: 21 component-rendering and source-security tests
- `bash scripts/integration.sh`: 23 real HTTP checks through the production Next proxy to FastAPI
- `npm audit --omit=dev`: 0 reported production vulnerabilities, including 0 high and 0 critical

### Component and security coverage

Safe React escaping of source scripts, HTML tags, event handlers, and JavaScript links; exact quote highlighting with emoji/supplementary CJK/ZWJ sequences and distinct NFC/NFD combining sequences; safe heading rendering; honest operation statuses; evidence-required manual proposals; bounded local research defaults; internal classification defaults; true empty states; server-only credentials; explicit single-user gating; loopback defaults.

### Integration coverage

1. Anonymous GET/POST denied despite a configured server API token
2. Login token stripped from JSON and stored in HttpOnly, SameSite=Strict cookie
3. Cross-origin and cross-site mutations denied
4. Project creation and retrieval
5. Text ingestion, idempotency, operation polling, immutable captures
6. Original capture downloaded byte-for-byte with attachment headers
7. Full-text search returns stable block references
8. Exact quote verification and document/block evidence locators
9. Evidence-backed proposal publication to report v1
10. CAS conflict returns 409 and preserves the pending proposal/current report
11. Version history retrieval and protected heading locks
12. Partial claim acceptance publishes only selected claims
13. Bounded local research produces events and an evidence-linked proposal
14. Multipart file ingestion
15. Non-empty ZIP export with attachment headers
16. Real database/worker diagnostics
17. Pipeline registry, immutable config save, sample-run quality and diff
18. Offline-safe source-watch create, role-scoped configuration, retrieval, update and pause
19. Logout revocation and anonymous 401 after sign-out
20. Reader can read but cannot create a project

Machine-readable output is generated at `test-results/proxy-integration.json`. Audit JSON is generated at `test-results/npm-audit-production.json`. These runtime files are intentionally ignored by Git; this summary is retained with the source.

## Not verified here

- Actual browser rendering, responsive screenshots, and interactive Playwright assertions
- Live public URL ingestion or scheduled fetching from remote source watches in this restricted network environment
- Temporal-driven recurring source checks and continuous research execution
- External model/search providers or paid API calls
- Docker image build/runtime (the application production build was verified)

### Browser blocker, explicitly reproduced

The supplied Playwright suite could not launch `/usr/bin/chromium`. The failure occurred **before navigation**:

`chrome/browser/process_singleton_posix.cc:297 Check failed: . socket() failed: Operation not permitted (1)`

The same failure persisted with a writable test HOME, approved escalation, and a final bundled attempt with FastAPI, Next.js, and Chromium in one exec/network namespace. The normal Playwright headless flags already included `--disable-dev-shm-usage`; no restriction-bypass flags were attempted. The separate cloud browser also rejected the loopback app with `ERR_BLOCKED_BY_CLIENT`.

No screenshot or browser test pass is claimed. To verify these in a supported local/CI Chromium environment:

```bash
npm ci
npm run build
RUN_BROWSER_TESTS=true CHROMIUM_PATH=/usr/bin/chromium bash scripts/integration.sh
```

### Source-watch configuration and runtime fetching

Source-watch configuration has been strengthened to be offline-safe and is covered by the passing proxy integration suite: create, retrieve, update and pause are verified. Configuration does not perform a network fetch. The prior configuration-time DNS blocker is resolved. Actual remote fetches, Temporal scheduling, and continuous research execution remain separate network/runtime-dependent stages and are not claimed as verified here.

### Exact Unicode evidence anchors

The reader passes the saved evidence's block-local `start_offset` through to quote highlighting. It treats that value as Unicode code points, converts the prefix with `Array.from(text)` to obtain the correct UTF-16 position, and verifies the exact quote there. Repeated identical quotes therefore resolve to the stored occurrence, including after emoji, supplementary CJK and combining marks. An invalid explicit offset produces no quote highlight; first-match lookup is used only when no offset is supplied. Four targeted anchor tests cover these cases.

## Scholarly and raw-archive UI follow-on

Separate paginated scholarly leads show metadata/abstract/fulltext-available scope without implying papers were read. Detail views preserve provider originals, provenance, immutable review history and editor-only CAS corrections. Failed parses expose retained capture inspection and original downloads without a ready document. The additional real proxy cases cover 1,000-record pagination, correction/history/CAS/role gates and failed-capture raw integrity.

`tests/scholarly-flow.mjs` adds 10 interactive scenarios plus a browser-runtime-error check, invoked by the existing hosted browser step after `browser-flow.mjs`. It covers paging, safe text, corrections/conflicts, request errors/retry, raw download, mobile layout and interrupted navigation. Local Chromium still fails at launch in the managed authoring sandbox; these new interactive scenarios are pending the follow-on hosted run. The earlier backend release independently passed its original18 Chromium scenarios. No new UI screenshot is claimed until the hosted runner produces it.
