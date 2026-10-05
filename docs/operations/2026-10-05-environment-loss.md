# Environment loss incident: 2026-10-05

**Status: 24-hour live acceptance FAILED / INCOMPLETE. Private service state has not been restored.** All times below are UTC. This report contains no credentials, private corpus content, or service endpoints.

## Observed incident and impact

At approximately 13:35 on 2026-10-05, after the execution environment reported ready, its available filesystem had reverted to a state consistent with an approximately 02:20 snapshot. Subsequent private quantitative-research PostgreSQL data, content-addressed objects, Temporal state, same-host backups, and the operational continuation ledger were absent. No permitted local recovery path was available. The underlying platform cause and exact loss boundary have not been established; the observed snapshot time is approximate.

The public source and v10 report survived in remote commit [`11294aa9054e86566eee648962abc4a042330349`](https://github.com/JeffersonYoung/evidence-harbor/commit/11294aa9054e86566eee648962abc4a042330349). Recovering those files establishes source/report availability only. It does not restore the private database, immutable raw objects, workflow histories, pending operations, or evidence provenance.

The 24-hour live acceptance cannot be certified from the surviving files or earlier disposable-service tests. The continuity objective failed, and the acceptance record is incomplete. Reacquiring papers or reconstructing a corpus would create new state; it cannot substitute for restoration or establish that the original identifiers, hashes, versions, and pending work survived.

## Architecture and documentation at the surviving revision

The [operations runbook](../operations.md) correctly identifies application PostgreSQL, Temporal persistence and visibility, object storage, and separately protected operator configuration/secrets as a coordinated recovery set. It requires an isolated restore and checks for pending-job resumption without duplicate captures or report revisions. [Architecture](../ARCHITECTURE.md#portability-recovery-and-operations) also distinguishes project exports from a complete deployment backup.

The executable [`backup_local.py`](../../scripts/backup_local.py) covers SQLite plus local objects. Its manifest records file hashes and sizes; restore checks archive paths, hashes, database integrity, and a fresh destination. The corresponding [tests](../../tests/test_backup.py) validate that scope. Neither this utility nor its passing tests establishes PostgreSQL/Temporal disaster recovery or survival of environment-wide loss. A same-host archive shares the failure domain of its source.

Concrete gaps at that revision:

- No executable complete-production backup/restore workflow or manifest linking application PostgreSQL, Temporal persistence/visibility, and every required object to one coordinated recovery point
- No mandatory off-host retention step, independently verified copy receipt, or explicit attestation that the destination survives loss of the source environment; a storage endpoint alone is insufficient evidence
- No separate restore-test attestation tying an actual isolated restore, integrity checks, and pending-work behavior to the exact retained backup
- No documented recovery-point/recovery-time objectives, backup cadence and retention ownership, or monitoring for stale, incomplete, or unverified backups
- No independently retained continuation/acceptance ledger with explicit criteria for an interrupted 24-hour run

## Corrective work and acceptance boundary

The planned remediation is an off-host backup CLI and runbook. Implementation and tests are separate work; this incident report does not claim they are complete or deployed. The required evidence is:

1. A complete, versioned manifest for application PostgreSQL, Temporal persistence and visibility, and objects, including hashes/sizes, schema and release versions, restore-point coordination, and required configuration references. Secrets must remain separately protected and outside public artifacts.
2. A verified retained copy and explicit failure-domain attestation stating why the destination remains available if the source environment disappears. A URL, successful upload, or local checksum alone does not prove that independence.
3. A separate restore-test attestation identifying the exact backup, isolated target, checks performed, outcomes, and remaining failures. Validate evidence/object integrity, original identities and versions, and pending workflow/outbox recovery without duplicate side effects. Keep production notification destinations disabled in the test target.
4. An independently retained acceptance ledger. Begin a new live acceptance window only after the required deployment and recovery prerequisites are met; preserve this failed/incomplete result separately.

Until those checks succeed, source recovery, fixture-based tests, and backup tooling are preparatory evidence, not successful restoration of the lost private service or completion of 24-hour live acceptance.
