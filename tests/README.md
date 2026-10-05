# Regression suite

Run the self-contained suite from the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check tests
```

The default suite needs no external model account, paid service, or live website.
It uses isolated SQLite databases and temporary immutable object directories.
Text, HTML (including real Trafilatura extraction), and generated two-page text
PDFs go through the actual parsers, bounded parser subprocess, pipeline, database,
and HTTP API. External model and webhook HTTP boundaries are mocked only in the
explicit contract, delivery, and paid-call replay tests. Those tests assert actual
payload minimization, network denial, citation rejection, idempotency, budgets,
backoff, and state transitions; they do not establish provider availability.

## Covered invariants

- Project → text/HTML/PDF ingestion → search → exact Unicode quote/hash evidence →
  research event stream → staged proposal → reviewed publication → portable ZIP
- Workspace/role authorization, salted password sessions, logout, expiry and login
  attempt limits
- Immutable ORM resources and database-level UPDATE/DELETE triggers
- Content-addressed storage, original bytes, malformed source rejection, exact
  offsets, PDF page locators and explicit parser downgrade behavior
- Safe typed YAML stages, quality/index gates, privacy transforms and parser
  resource-isolation metadata
- SSRF-denied private/loopback/reserved addresses, mixed DNS, redirects and DNS
  rebinding; pinned TCP peer, size limits and authenticated redirect refusal
- Idempotent operations, changed/unchanged capture history, reprocessing without
  moving historical evidence, raw-corruption publication/export rejection
- Missing and cross-project citation denial, compare-and-swap revisions, immutable
  document history, partial claim acceptance and manually locked sections
- Source watches, precise change-review signals, bounded review-only follow-up,
  discovery leads kept distinct from saved evidence, webhook retries/backoff
- External evidence classification/sharing gates, contact redaction, explicit
  pricing and cost reservation, uncertain paid-call retry protection
- Actual SQLite/object backup and fresh relocated restore, including original
  evidence verification, archive hashes, path traversal and overwrite refusal
- MCP path/URL/token/redirect controls and absence of publication/admin tools

## Real service integration

These tests are skipped unless their service endpoints are explicitly provided:

```sh
EVIDENCEHARBOR_TEST_POSTGRES='postgresql+psycopg://...' \
EVIDENCEHARBOR_TEST_TEMPORAL='127.0.0.1:7233' \
.venv/bin/python -m pytest -q tests/test_postgres_domain.py \
  tests/test_vector_integration.py tests/test_temporal_integration.py
```

Use a disposable test database. The PostgreSQL domain tests create and drop a
unique private schema per test. They exercise real GIN/CJK retrieval, competing
CAS publications, concurrent worker redelivery and durable cached model-response
recovery without a duplicate provider call. The remaining integration tests
exercise real pgvector and Temporal behavior.

The repository also includes `scripts/run_integration.py` to start PostgreSQL and
Temporal and run those tests in one process/network namespace. Its CLI requires
`--postgres-bin` and can use `--temporal-cli` for existing official binaries.

A green default run with skipped integrations does not establish PostgreSQL,
pgvector, or Temporal health. Live paid models, OCR assets, browser-rendered sites,
and a production S3 account are not contacted by the default suite.

For an actual disposable local S3-compatible service, supply an official MinIO
binary to the dedicated harness:

```sh
.venv/bin/python scripts/run_s3_integration.py --minio-binary /path/to/minio
```

It creates temporary local test credentials and storage, runs
`tests/test_s3_integration.py`, and shuts the service down. These tests are skipped
in the ordinary suite unless `EVIDENCEHARBOR_TEST_S3` is configured. The separate
`test_s3_pipeline.py` exercises the complete API/pipeline/export path with only the
S3 protocol boundary replaced by a deterministic in-memory client; it does not
establish compatibility with a live S3 service.
