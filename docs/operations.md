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

## Durable attempt state and worker loss

New workflows use the replay-gated `operation-lifecycle-v2` activity contract. Attempt failures remain `retrying` with no completed timestamp; a workflow terminal-failure decision invokes an idempotent finalization activity. Deterministic parse, pipeline, unsafe-URL and client/domain validation errors are nonretryable; transient errors remain bounded by the workflow retry policy. Heartbeats start immediately and repeat every10seconds, with a30-second heartbeat timeout for new operations. Finalization itself retries at most5times with a2-minute attempt limit; a persistent database outage remains a visible recovery problem.

`GET /v1/operations/{id}/execution` reads Temporal's actual workflow status. Unknown/unavailable service state never implies completion. The ordinary operation endpoint identifies whether its status is an inline operation or a durable activity attempt. Explicit retry in durable mode requires a verified terminal failed/cancelled/timed-out workflow; it cannot start another generation merely because an activity attempt failed. On resume, stale completed timestamps are cleared.

Already-scheduled legacy activity commands retain their original timeout and failure contract, including the20-minute operation timeout. Replay markers do not retroactively shorten timers. Read the live execution endpoint when diagnosing these histories. The acceptance suite replays a real legacy history and kills only a disposable worker to verify genuine30-second heartbeat recovery without clock changes.

Deploy immutable release directories or container images. Do not run a persistent worker from a checkout being edited: Temporal's workflow sandbox can load newer workflow definitions while the process still has old activity registrations. Stop/restart all API/worker/dispatcher processes together against the reviewed release, apply migrations against the persistent database, and preserve the database/object-store/Temporal identities. A mixed-code deployment is not a valid parser or recovery acceptance run.

## Upgrade to schema head0009

For an existing deployment, first stop writers, workers and the dispatcher and take a coordinated database/object-store backup (include Temporal persistence when recovering the full service). Install a reviewed immutable release, run `alembic upgrade head` with that deployment's database configuration, verify `alembic current` reports0009, then restart API/workers/dispatcher together and check pending operation execution state and evidence integrity. Keep the previous release and verified backup until acceptance is complete. Do not apply an irreversible schema downgrade to recover research provenance.

Hosted CI uses disposable test databases. A green GitHub workflow does not back up, migrate or deploy the user's production database; those remain explicit operator deployment steps.

### Bounded reprocessing for demanding documents

The registered `extract` stage defaults to `parser_memory_mb: 1024` and `parser_timeout_seconds: 30`. An operator can submit an explicit safe pipeline to `/v1/captures/{id}/reprocess`, for example2048 MiB and60seconds for a saved PDF that exceeded the default. These remain bounded settings within the registry's supported128–8192 MiB and1–180second ranges, under the same isolated parser and source-map/quality gates. They do not raise global defaults or authorize unrestricted execution. Allocate corresponding worker/container capacity and bounded concurrency; a host limit may still be lower than the requested parser budget. Preserve the failed attempt and capture hash when comparing results.

## Off-host durability admission

See [off-host recovery](operations/offhost-recovery.md) for complete quiesced recovery-set upload, checksum verification, fresh-directory download and explicit durable-run preflight. A local backup, project export, S3 endpoint or green CI alone does not establish an independent recoverable system. Declaring `DURABLE_CONTINUOUS_RUN=true` requires a fresh matching remote receipt and separate native-restore/failure-domain attestations at process startup.

The [2026-10-05 environment-loss incident](operations/2026-10-05-environment-loss.md) left private study state unrestored and the 24-hour acceptance failed/incomplete. Historical short-run tests and published reports do not change that result.
