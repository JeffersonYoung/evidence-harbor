# Verification record

This record distinguishes executable product tests from unperformed deployment acceptance. It accompanies the reproducible commands, not a blanket production-readiness certification.

## Final local verification (2026-10-05)

- Default Python collection: **315 tests: 303 passed, 12 skipped** (24.21 seconds). Skips: 7 PostgreSQL/pgvector, 3 Temporal, 2 live S3 tests; these are opt-in, not failures or passes
- Separate real PostgreSQL17.11/pgvector0.8.0 + Temporal CLI1.9.1/Server1.32.0 runner: **12 passed** (20.74 seconds). This includes 10 opt-in database/Temporal tests and 2 tests also exercised by the default suite; do not add both counts as unique tests
- Frontend component/security: **16 passed**; production Next-to-FastAPI HTTP scenarios: **20 passed**
- Python agent-distribution tests: **3 passed**; OpenClaw Node tests: **3 passed**
- Ruff, compileall, TypeScript, Next production build and Prettier checks: **passed**

Final additions include portable project configuration/Markdown projections, bounded renderer CONNECT DNS resolution and version-pinned progressive MCP/API reading (24 focused Unicode/range/identity regressions). The proxy partial-publication test was corrected to unlock/rebase the existing report with its current version, preserving the single-report/CAS contract. This is local verification, not remote GitHub CI or deployment acceptance. Public repository upload and hosted CI results must be independently verified.

## Executed

- Full Python unit/domain/FastAPI/security suite, including real HTML/text/PDF extraction and immutable evidence, database mutation guards, budgets, human publication/CAS, accounts, source connectors, bounded discovery, backup/relocation, MCP and S3 protocol-boundary tests
- Actual PostgreSQL17.11 + pgvector0.8.0: Chinese/English FTS, concurrent updates/redelivery, cached paid-result recovery and real vector-index/hybrid retrieval API
- Actual Temporal CLI1.9.1 / Server1.32.0: durable queue without a worker, activity retries, duplicate dispatch acknowledgement, schedule reconciliation/pause, terminal workflow failure followed by explicit API retry and generation1 workflow success
- Next.js production build, strict TypeScript, formatting, component/security and real HTTP-through-Next-to-FastAPI checks including login/logout401, reader403, CSRF, exact raw downloads, Unicode/repeated-quote handling, proposals/CAS, selected claims, pipeline sample diff and offline-safe watch configuration
- Agent distribution schema/transport tests; actual Codex CLI marketplace inventory check in a disposable configuration, not installation into a user's account
- Three-domain saved-source empirical run and controlled model/effort experiment documented separately in docs/empirical

The counts above cover the final code revision; additional documentation and packaging changes do not assert a deployed service.

Hosted CI is configured to run these checks again, including real Chromium UI flows and a disposable S3-compatible service. Those jobs are pending publication/execution and must not be reported as passed from local results. Explicit service variables and no-skip checks prevent an unavailable integration from silently producing a green acceptance result.

## Not executed / verified blockers

- Docker itself is absent. Compose YAML parses, Dockerfiles/config are provided, but a Compose image-build/runtime pass is not claimed
- Chromium fails before navigation with `process_singleton_posix.cc:297 socket() failed: Operation not permitted`, including same-namespace API+Next+browser and a writable HOME. No screenshot or visual browser acceptance is claimed
- The official MinIO direct and archived binary endpoints returned HTTP410. Building the official RELEASE.2025-10-15T17-29-55Z source with a temporary official Debian Go toolchain succeeded. The service then failed startup on `netlinkrib: operation not permitted`. This limitation was not bypassed. `scripts/run_s3_integration.py` is ready for an ordinary host; actual S3 service acceptance remains pending
- Docling with deployed OCR/table model assets, the real isolated renderer network, external model/search credentials and coordinated production PostgreSQL+Temporal+S3 disaster recovery were not available for live acceptance
- Local feature-hash embeddings are a deterministic baseline, not measured neural semantic-search quality

## Commands

```sh
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
pytest -q
ruff check backend scripts tests deploy/renderer integrations
python -m compileall -q backend scripts deploy/renderer
python scripts/run_integration.py --postgres-bin /path/to/postgresql/bin
python scripts/run_s3_integration.py --minio-binary /path/to/minio
python -m unittest discover -s integrations/tests -v
node --test integrations/tests/openclaw.test.mjs
cd apps/web && npm ci && npm run typecheck && npm run build && npm run format:check
cd ../.. && bash apps/web/scripts/integration.sh
```

Real-service tests skip explicitly if their test environment is not configured. Default-suite success does not convert those skips into passes. Sources/providers requiring credentials remain disabled until the operator supplies reviewed configuration.
