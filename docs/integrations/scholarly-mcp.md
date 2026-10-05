# Scholarly MCP parity and authority

The original nine MCP tools retain their names and arguments. The default server now exposes **15 tools**: those nine plus six scholarly tools. An explicitly enabled editor mode exposes a sixteenth tool. Client manifests and schema snapshots declare this expanded surface; tool presence never grants an API role.

## Default scholarly tools

| Tool | Shared REST/domain path | Bounds and purpose |
|---|---|---|
| `list_discovered_works` | GET `/v1/discovered-works?view=agent` | Default 20, maximum 50 records; metadata/scope only, no full abstracts |
| `read_discovered_work` | GET `/v1/discovered-works/{id}?view=agent` | Current reviewed display values, abstract window (default 2000/max 8000 code points), per-collection record pages (default 10/max 50) |
| `read_scholarly_record` | GET `/v1/discovered-works/{id}/records/{kind}/{record_id}` | Immutable observation, reading or metadata-review JSON windows, default 2000/max 16000 code points, full/window SHA-256 hashes |
| `intake_discovered_works` | POST `/v1/discovered-works/batch?view=agent` | 1–20 items, maximum 256000 serialized UTF-8 bytes; same DOI/arXiv/title-author deduplication |
| `record_scholarly_observation` | POST `/v1/discovered-works/{id}/observations?view=agent` | One bounded observation; reuses the intake domain and refuses an identity that resolves to another/new work |
| `link_scholarly_reading` | POST `/v1/discovered-works/{id}/readings` | Same saved-byte/representation/provenance/scope validation as REST; no metadata-only promotion |

All reads remain workspace-scoped. Intake, observation append and reading links require the existing researcher role. Reader credentials cannot write. Unknown or foreign IDs return normal API failures; there is no datastore fallback. The transport verifies the authenticated `scholarly-mcp-v1` capability before using these routes, preventing an old API from ignoring `view=agent` and returning an unbounded legacy response or accepting a mutation before the compatibility failure is noticed. Upgrade the API before updating its MCP clients.

The bounded views are projections, not a second business engine. Shared Pydantic request schemas, domain identity/provenance rules, authorization and database transactions remain authoritative. Legacy REST views used by the UI remain compatible.

## Observations, screening and corrections

For screening or review audit:

1. Read the work and obtain an original observation ID
2. Reconstruct that immutable record's canonical JSON through `read_scholarly_record`, following `reading_range.next_offset`; verify `record_sha256` if storing it locally
3. Preserve the original observation's `payload` identity fields and provenance, and add the intended bounded audit metadata
4. Call `record_scholarly_observation` with the existing work ID and updated payload; the response identifies the saved observation/hash

The real-study conventions include `metadata.title_abstract_screen`, `metadata.title_abstract_screen_adjudication` and `metadata.substantive_review`. They can record protocol/version/hash, rationale, ordinal, status and links to prior observations. These are untrusted audit data, not executable instructions or automatic eligibility/state grants. Exact repeats deduplicate. Append does not change reviewed canonical display values or claim a full-paper read.

A reading link still requires an actual acquired source document with verified immutable bytes and matching recorded provenance. `fulltext_reviewed` attests that the artifact contains full text; it does not assert complete reading. State the sections/windows actually inspected in `review_note`. Abstract-only evidence remains abstract-scoped.

## Explicit editor mode

By default, `review_discovered_work_metadata` is absent. An operator may explicitly set `EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS=true` for the MCP process and restart it. The tool additionally requires an existing editor-role API credential and the normal `expected_revision`, reason and source/evidence linkage. A researcher credential still gets 403 even when the tool is visible. Stale revisions still get 409. Original provider observations remain immutable.

The optional editor tool also accepts `changes.abstract` (1–100000 Unicode code points). Omit it to leave the abstract unchanged; empty or whitespace-only replacements are rejected. The current display carries `abstract_display_scope=abstract_metadata`, `abstract_evidence_eligible=false`, the displayed-text SHA-256 and the introducing review’s revision, reason and source/evidence attribution. Later title-only reviews retain that abstract attribution. Provider-original text remains available separately and in immutable observations/export. Progressive abstract reads hash the reviewed display and each returned window.

A replacement never creates a document, capture or evidence and never grants full-text availability or completed reading. Record source-version uncertainty in the review reason: a linked preprint abstract is not automatically the verified abstract of a journal version. The operator UI uses the same editor/CAS endpoint. This additive JSON-overlay change needs no migration beyond existing head `0009`.

Codex's generated configuration permits this optional environment variable to be forwarded when present. For Claude, set the literal flag in the MCP server's `env` object only when intentionally enabling this mode; the default template does not require an unset optional variable. OpenClaw enables the optional registration only when its Gateway process has the flag, and forwards it through both bridge stages. Its separate `editor-tool-schemas.json` is included in the source package.

Enabling the flag creates no credentials and grants no role. Supplying or expanding persistent access remains a separate operator security decision. No MCP mode exposes report publication, administrative schedules, credential management, shell or arbitrary SQL.

## Verification boundary

The repository tests run official MCP stdio sessions, authenticated loopback HTTP and the real FastAPI/SQLite domain with temporary test-owned roles. They verify default/opt-in discovery, intake/deduplication, screening append/rollback, all record-window hashes, actual reading-link gates, reader/workspace denial and editor CAS failure. Contract tests cover transport budgets and fail-closed API version negotiation. OpenClaw registration/schema/bridge tests cover both modes.

This is authenticated transport/domain parity in a disposable fixture, not a claim that the production research corpus was accessed over MCP, that persistent credentials were created, or that every third-party agent host was installed. The real-study corpus initially used REST; its original nine-tool inventory probe was not equivalent to this authenticated acceptance suite.
