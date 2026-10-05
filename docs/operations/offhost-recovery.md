# Off-host recovery and declared durable runs

A same-host copy can be lost with the live database. An S3 URL also does not prove a separate failure domain. The operator must establish independently recoverable storage and access, retain the recovery manifest identifier outside the live host, and test a native restore in another environment. The [2026-10-05 incident](2026-10-05-environment-loss.md) left the 24-hour study acceptance failed/incomplete. This tooling does not recover that missing private state.

## What the utility actually does

`python scripts/recovery.py` transfers a bounded, complete **operator-prepared** recovery set through the existing explicit-credential S3 adapter. It neither runs `pg_dump` nor administers Temporal, creates credentials/buckets, changes retention, collects ambient environment variables, or restores a database automatically. The database engines currently supported by the recovery-set contract are PostgreSQL research storage and Temporal persistence/visibility. SQLite local snapshots remain a separate utility and cannot satisfy declared durable admission.

Stop API writers, workers, dispatcher and Temporal writes. Produce native backups of all databases at that quiesced recovery point, and copy/snapshot all referenced immutable objects. Keep every native artifact in a dedicated component directory. Include reviewed non-secret configuration (versions, migration revision, Temporal namespaces/task queues, storage mapping and deployment instructions). Preserve actual secrets separately through an approved secret manager; do not put `.env`, private keys, tokens or credential exports in the configuration directory. Native database dumps themselves contain private user data and account state, so the whole set requires private access and operator-managed encryption/retention. This CLI assumes the backup destination's encryption and access policies are already reviewed; it does not change them.

The required component names are `research_database`, `temporal_persistence`, `temporal_visibility`, `objects` and `configuration`. Each must contain at least one regular file. For a shared Temporal database, retain the same native backup under both named components and document the topology; content chunks deduplicate. Empty object stores need an explicit inventory file declaring zero referenced objects. A manifest is a declaration of the selected set, not proof that the operator dumped every required database or object.

Example input, stored privately (replace paths and identifiers):

```json
{
  "format": "evidenceharbor-recovery-set-v1",
  "engine": "postgresql-temporal",
  "run_id": "study-run-2026-10",
  "snapshot_id": "checkpoint-001",
  "snapshot_at": "2026-10-05T14:00:00+00:00",
  "source_failure_domain": "research-host-volume-group",
  "consistency": "writers-stopped",
  "operator": "responsible operator",
  "quiescence_evidence": "Private change record with stopped services and snapshot time",
  "schema_revision": "0009",
  "release_commit": "reviewed full commit identifier",
  "configuration_secrets_excluded": true,
  "components": {
    "research_database": "/private/snapshot/research",
    "temporal_persistence": "/private/snapshot/temporal",
    "temporal_visibility": "/private/snapshot/visibility",
    "objects": "/private/snapshot/objects",
    "configuration": "/private/snapshot/configuration"
  }
}
```

Only use an existing approved S3 account/destination. The tool reuses `S3_ENDPOINT_URL`, `S3_REGION` and the explicit `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`/optional `S3_SESSION_TOKEN` configuration; no metadata-service credential discovery occurs. Set `RECOVERY_S3_BUCKET` and `RECOVERY_S3_PREFIX` deliberately. Do not put credentials in command arguments or policy/receipt files. New credentials, persistent access or new destinations require the operator's normal approval process.

```sh
python scripts/recovery.py upload /private/snapshot-spec.json /private/receipt-001.json
python scripts/recovery.py verify MANIFEST_SHA256 /private/reverified-001.json
python scripts/recovery.py download MANIFEST_SHA256 /fresh/nonexistent/restore-inputs /private/download-001.json
```

All chunks are immutable content-addressed objects, read back and hashed; the manifest is committed last. An interrupted upload can leave unreferenced chunks but emits no successful complete receipt. Files are streamed in 8 MiB chunks, with limits of 100,000 files, 100 GiB total and a 16 MiB manifest. Inputs changing during transfer are rejected. Upload pins every ancestor with descriptor-relative no-follow traversal, rejects nonregular files before transmission, and requires a POSIX host supporting those operations. Configuration files are limited to 1 MiB each and common credential patterns are rejected; this is a guardrail, not a replacement for secret review. Remote path traversal, duplicate paths, oversized sets, missing chunks and hash/size mismatches fail verification. Download stages into a fresh directory and installs it using atomic no-replace rename after verification (Linux/Windows; unsupported platforms fail closed). It never extracts nested archives, executes remote content or overwrites an existing database/directory. This verifies backup bytes, not the internal validity or completeness of opaque native dumps.

## Native restore and admission

Use the downloaded inputs to perform native PostgreSQL and Temporal restores into an isolated, fresh environment with production notifications/provider calls disabled. Follow the deployed server versions' documented restore procedures. Verify research DB/migration state, Temporal histories, every raw-object digest referenced by the DB, evidence locators and genuine pending-job resumption without duplicate publication. Record the commands, checks, exact manifest hash and failure-domain evidence in a private restore record. A same-host temporary-directory test proves neither infrastructure independence nor disaster recovery.

The operator writes a separate `evidenceharbor-restore-attestation-v1` report with `run_id`, the exact `manifest_sha256`, `backup_failure_domain`, `restore_failure_domain`, `operator`, timezone-aware `tested_at`, private `evidence_reference`, `passed: true`, and a `checks` object containing `research_database`, `temporal_history`, `raw_objects`, `evidence_locators`, `pending_job_recovery`, all true. These are explicit operator attestations; the tool does not fabricate them or independently audit the infrastructure. A report for a different set does not pass.

A policy uses format `evidenceharbor-durability-policy-v1` and contains `run_id`, `source_failure_domain`, distinct `backup_failure_domain`, `operator`, `independence_evidence`, `independent_failure_domain_attested: true`, `maximum_backup_age_seconds`, `maximum_restore_test_age_seconds`, and `destination` with exact `bucket`, `prefix`, and HTTPS `endpoint` matching the receipt. Backup freshness applies to both the operator-declared snapshot time and the remote verification time; re-reading an old backup cannot refresh its data age. Freshness limits must be between 60 seconds and 31 days. Choose them from the run's recovery objectives; this tool does not choose acceptable data loss. HTTP/loopback destinations cannot pass durable admission, even though loopback HTTP remains available for disposable transport tests.

```sh
python scripts/recovery.py preflight /private/policy.json MANIFEST_SHA256 /private/restore-report.json /private/admission-receipt.json
```

Preflight rereads and hashes the whole remote set before checking the policy and independent native-restore attestation. A receipt alone, object-store availability, or a green CI test is insufficient. Preserve the manifest identifier, policy, receipts, restore evidence and continuation ledger outside the live host as well. These metadata are not signed certificates; protect them from tampering and do not treat operator assertions as machine proof of failure-domain independence.

For a run explicitly declared durable, set `DURABLE_CONTINUOUS_RUN=true`, `DURABILITY_POLICY`, `DURABILITY_RECEIPT` and `DURABILITY_RESTORE_REPORT` to those private files. PostgreSQL with Temporal worker mode is required. API/worker/dispatcher configuration startup fails closed if any required evidence is missing, stale, mismatched, partial or same-domain. The startup check validates saved evidence; it does not reread remote data. Run the full CLI preflight immediately before launch and periodically under the operator's supervisor. Startup admission is not continuous monitoring: if a receipt later expires or the backup service fails, the supervisor must pause the acceptance claim/run and alert the operator. Normal development without the explicit declaration is unchanged.

No independent-host native restore has been performed for the lost study. Disposable fake-S3 and hosted loopback-S3 transport tests establish adapter behavior only. No paid service or persistent access is created by these tests.
