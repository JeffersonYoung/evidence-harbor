# Verification record

This record distinguishes executable product tests from unperformed deployment acceptance. It accompanies the reproducible commands, not a blanket production-readiness certification.

## Final local verification (2026-10-05)

- Default Python collection: **443 tests: 429 passed, 14 skipped** (59.96 seconds). Skips: 7 PostgreSQL/pgvector, 5 Temporal, 2 live S3 tests; these are opt-in, not failures or passes
- Separate real PostgreSQL17.11/pgvector0.8.0 + Temporal CLI1.9.1/Server1.32.0 runner: **29 passed** (54.52 seconds). This includes 12 opt-in database/Temporal tests and 17 tests also exercised by the default suite; do not add both counts as unique tests
- Frontend component/security: **23 passed**; production Next-to-FastAPI HTTP scenarios: **24 passed**
- Python agent-distribution tests: **3 passed**; OpenClaw Node tests: **4 passed**
- Ruff, compileall, TypeScript, Next production build and Prettier checks: **passed**

Final additions include durable scholarly discovery and reviewed metadata corrections, explicit abstract/fulltext scope, archive-before-parse failed-capture recovery, safe DOCX parsing, and replay-gated Temporal attempt/terminal lifecycle with real worker-loss recovery. Prior portable export and progressive-reading coverage is retained. The proxy partial-publication test was corrected to unlock/rebase the existing report with its current version, preserving the single-report/CAS contract. This is local verification, not remote GitHub CI or deployment acceptance. The backend and UI/context releases were published and passed hosted CI; the reviewed-abstract follow-on requires its own hosted run after publication.

## Reviewed abstract correction

Five additional tests cover editor/CAS authorization, nonempty/size/source requirements, immutable provider observations, re-intake stability, export, Unicode window hashes, evidence isolation and the actual opt-in MCP editor transport. The UI and production proxy exercise reviewed abstract attribution and original-provider preservation. The added Chromium scenario is ready for hosted CI; local Chromium remains blocked as documented below. Reviewed abstracts remain metadata and cannot become full-text evidence. No new migration is required; existing head is `0009`.

## Scholarly MCP acceptance

- Eight official stdio MCP → authenticated HTTP → actual FastAPI/SQLite tests use temporary test-owned credentials and verify default15/opt-in16 tool discovery, role/workspace denials, deduplication, progressive record hashes, provenance/scope gates and editor CAS
- Four transport-contract tests verify payload bounds, disabled editor mode and fail-closed API capability negotiation
- Shared scholarly request schemas, generated default/editor client schemas and OpenClaw package contents were verified; no persistent user credentials or real model keys were created
- This proves disposable authenticated transport/domain parity. The production study originally used REST; no authenticated MCP run against its private corpus is claimed here

## Executed

- Full Python unit/domain/FastAPI/security suite, including real HTML/text/PDF extraction and immutable evidence, database mutation guards, budgets, human publication/CAS, accounts, source connectors, bounded discovery, backup/relocation, MCP and S3 protocol-boundary tests
- Actual PostgreSQL17.11 + pgvector0.8.0: Chinese/English FTS, concurrent updates/redelivery, cached paid-result recovery and real vector-index/hybrid retrieval API
- Actual Temporal CLI1.9.1 / Server1.32.0: durable queue without a worker, activity retries, duplicate dispatch acknowledgement, schedule reconciliation/pause, terminal workflow failure followed by explicit API retry and generation1 workflow success
- Next.js production build, strict TypeScript, formatting, component/security and real HTTP-through-Next-to-FastAPI checks including login/logout401, reader403, CSRF, exact raw downloads, Unicode/repeated-quote handling, proposals/CAS, selected claims, pipeline sample diff and offline-safe watch configuration
- Agent distribution schema/transport tests; actual Codex CLI marketplace inventory check in a disposable configuration, not installation into a user's account
- Three-domain saved-source empirical run and controlled model/effort experiment documented separately in docs/empirical

The counts above cover the final code revision; additional documentation and packaging changes do not assert a deployed service.

The backend extension commit `e29b89dffb536bc89d14c361da02df10a52fa07a` passed all four [hosted CI jobs](https://github.com/JeffersonYoung/evidence-harbor/actions/runs/37262828801), including 407 Python tests, 29 durable-suite tests, 18 Chromium UI scenarios and 2 live S3 tests. Those results validate the scholarly/DOCX/archive/lifecycle backend release. The unified scholarly UI/context release `868b388280318da1d951b28262b42ba5845fc1ef` also passed all four hosted jobs, including18 original and11 scholarly Chromium scenarios. The subsequent public report commit `6e97f447d8f1c983be5af998e417e71987fd8263` retained green CI. The expanded scholarly MCP release `fc8c442dc1bb63f1678332186b275475861a3ca4` passed all four [hosted jobs](https://github.com/JeffersonYoung/evidence-harbor/actions/runs/37273739000), including 424 Python tests and all 29 browser scenarios. The current reviewed-abstract correction requires its own hosted run after publication. Explicit service variables and named no-skip gates prevent unavailable integrations from silently producing green acceptance results.

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
