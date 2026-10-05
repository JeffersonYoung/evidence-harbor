# Operations and recovery

## Process boundaries

The API writes domain state and dispatch records in one transaction. The dispatcher starts a deterministic Temporal workflow and only then acknowledges the dispatch. Worker activities perform all non-deterministic I/O. A crash between workflow start and acknowledgement reuses the existing workflow. Manual retry of a terminal failure increments a persisted dispatch generation, so it cannot be mistaken for the previous completed workflow.

Source-watch configuration is reconciled into Temporal schedules; the agent has no scheduler-write tool. Schedule overlap is skipped to prevent concurrent checks. Change analysis compares representations only under matching parser/configuration signatures; parser changes are not labelled as source-content changes. Human publication remains required after incremental research.

Webhooks are at-least-once. Deduplicate `X-EvidenceHarbor-Event-ID`, not delivery timestamps. A destination without a target URL is pull-only. Bounded retries use exponential backoff and surface terminal failures.

## Local snapshots

```sh
python scripts/backup_local.py create var/research.sqlite var/objects /safe/path/workspace-backup.zip
python scripts/backup_local.py restore /safe/path/workspace-backup.zip /new/empty/path
```

Restore refuses an existing destination. It verifies hashes and rejects unsafe archive paths. Start with DATABASE_URL pointing at the restored workspace.sqlite and OBJECTS_DIR at restored objects, then run `alembic upgrade head`. Content-hash resolution permits relocation without rewriting immutable evidence. Do not expose the backup ZIP publicly.

## Production backup set

Back up these as one recoverable system:

1. PostgreSQL research database, roles/grants and schema migration revision
2. Temporal persistence and visibility databases, plus server configuration
3. Content-addressed object storage and any S3 object-version/retention configuration
4. Operator configuration and encrypted secrets through the organization's secret manager, not in project exports

Use PostgreSQL-native backups/PITR and object-store versioned snapshots. Coordinate a restore point or stop writers before a simple full snapshot. Pending operations and dispatch/outbox IDs must be restored with the business data. Unreferenced immutable blobs are safe to retain; never garbage-collect solely by age while a backup/restore might still reference them.

Restore into a fresh isolated environment, verify raw-object checksums and evidence locators, start the database/API, then Temporal and workers/dispatcher. Confirm that pending jobs resume without duplicate captures/report revisions. Do not point a restored test dispatcher at production notification destinations.

## Monitor

- Oldest pending/running operation age and counts by kind/status
- Source failure rate; parser quality/failure diagnostics
- Missing/corrupt blob count and reference validation failures
- Research queue and uncertain paid-call reservations
- Dispatch/outbox attempts and terminal failures
- External provider usage and price estimates versus configured ceilings
- Login failures/rate-limit events and authorization denials without logging passwords/tokens

The API exposes scoped `/v1/diagnostics`; operational dashboards, paging integrations, a retention/garbage-collection service and capacity SLOs require deployment-specific setup. Establish OCR concurrency and object/version growth limits with a representative corpus rather than assuming a fixed document-count capacity.

## Verified versus pending

Direct ephemeral PostgreSQL/pgvector and real Temporal integration tests are available in `scripts/run_integration.py`; they require no Docker. The authoring environment cannot run Docker itself and cannot complete Chromium visual acceptance. Live S3, externally credentialed model/search services, Docling OCR assets and an isolated browser service remain explicitly pending environment acceptance.
