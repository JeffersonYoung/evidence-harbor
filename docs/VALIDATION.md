# Verification record

This record distinguishes executable product tests from unperformed deployment acceptance. It accompanies the reproducible commands, not a blanket production-readiness certification.

## Final local verification (2026-10-05)

- Default Python collection: **421 tests: 407 passed, 14 skipped** (36.35 seconds). Skips: 7 PostgreSQL/pgvector, 5 Temporal, 2 live S3 tests; these are opt-in, not failures or passes
- Separate real PostgreSQL17.11/pgvector0.8.0 + Temporal CLI1.9.1/Server1.32.0 runner: **29 passed** (54.40 seconds). This includes 12 opt-in database/Temporal tests and 17 tests also exercised by the default suite; do not add both counts as unique tests
- Frontend component/security: **16 passed**; production Next-to-FastAPI HTTP scenarios: **20 passed**
- Python agent-distribution tests: **3 passed**; OpenClaw Node tests: **3 passed**
- Ruff, compileall, TypeScript, Next production build and Prettier checks: **passed**

Final additions include durable scholarly discovery and reviewed metadata corrections, explicit abstract/fulltext scope, archive-before-parse failed-capture recovery, safe DOCX parsing, and replay-gated Temporal attempt/terminal lifecycle with real worker-loss recovery. Prior portable export and progressive-reading coverage is retained. The proxy partial-publication test was corrected to unlock/rebase the existing report with its current version, preserving the single-report/CAS contract. This is local verification, not remote GitHub CI or deployment acceptance. The baseline was published and passed hosted CI; this follow-on extension requires its own hosted run after publication.

## Executed

- Full Python unit/domain/FastAPI/security suite, including real HTML/text/PDF extraction and immutable evidence, database mutation guards, budgets, human publication/CAS, accounts, source connectors, bounded discovery, backup/relocation, MCP and S3 protocol-boundary tests
- Actual PostgreSQL17.11 + pgvector0.8.0: Chinese/English FTS, concurrent updates/redelivery, cached paid-result recovery and real vector-index/hybrid retrieval API
- Actual Temporal CLI1.9.1 / Server1.32.0: durable queue without a worker, activity retries, duplicate dispatch acknowledgement, schedule reconciliation/pause, terminal workflow failure followed by explicit API retry and generation1 workflow success
- Next.js production build, strict TypeScript, formatting, component/security and real HTTP-through-Next-to-FastAPI checks including login/logout401, reader403, CSRF, exact raw downloads, Unicode/repeated-quote handling, proposals/CAS, selected claims, pipeline sample diff and offline-safe watch configuration
- Agent distribution schema/transport tests; actual Codex CLI marketplace inventory check in a disposable configuration, not installation into a user's account
- Three-domain saved-source empirical run and controlled model/effort experiment documented separately in docs/empirical

The counts above cover the final code revision; additional documentation and packaging changes do not assert a deployed service.

The previously published baseline commit `3139396fb7bc7933f39cea6ea41cf58be0576bca` passed all four [hosted CI jobs](https://github.com/JeffersonYoung/evidence-harbor/actions/runs/37261347785), including 18 Chromium UI scenarios and 2 live S3 tests. Those results validate that baseline, not the newer scholarly/DOCX/lifecycle extension. This extension's hosted run is pending publication. Explicit service variables and named no-skip gates prevent unavailable integrations from silently producing green acceptance results.

## Not executed / verified blockers

- Docker itself is absent. Compose YAML parses, Dockerfiles/config are provided, but a Compose image-build/runtime pass is not claimed
- Chromium fails before navigation with `process_singleton_posix.cc:297 socket() failed: Operation not permitted`, including same-namespace API+Next+browser and a writable HOME. Local browser acceptance is not claimed; baseline screenshots/UI acceptance were obtained separately on the hosted runner
- The official MinIO direct and archived binary endpoints returned HTTP410. Building the official RELEASE.2025-10-15T17-29-55Z source with a temporary official Debian Go toolchain succeeded. The service then failed startup on `netlinkrib: operation not permitted`. This limitation was not bypassed. `scripts/run_s3_integration.py` is ready for an ordinary host; local S3 service acceptance remains blocked; the published baseline separately passed its hosted S3 job
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
