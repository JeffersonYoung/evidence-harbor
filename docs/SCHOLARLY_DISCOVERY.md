# Scholarly discovery, availability and evidence scope

Discovery is a durable bibliography, not a claim that papers were read. This distinction applies even when a provider supplies an abstract, an open-access flag or a PDF URL.

## Intake and identity

`POST /v1/discovered-works/batch` accepts a project ID and 1–200 items. Each item supports:

- `title`, `authors` (name strings), optional `year`
- `doi`, `arxiv_id`, optional `source_url`
- `abstract`, `access_status`: `unknown`, `open_access`, `restricted` or `unavailable`
- `metadata`: bounded JSON for provider record IDs, response hashes, acquisition timestamps, exact query URLs, topic tags and `source_locators` (exact URL strings)

At least one DOI, arXiv ID or title plus author is required. DOI URL/prefix/case variants normalize to one key. arXiv version suffixes normalize to a work identity, while the original submitted version remains in the observation. Title plus normalized first author is a weaker fallback key. A strong identifier can add aliases to an existing work; conflicting strong identities fail with409 rather than silently merging. A conflicting item rolls back the whole batch. Concurrent unique-key collisions also fail409 and can be retried as a complete batch.

The first display title stays stable. Title variants and original provenance are retained in immutable, hash-deduplicated observations; repeated identical observations do not accumulate. Alias, observation and saved-reading records reject ORM and raw SQL mutation. Access status is provider metadata, not proof that access succeeded.

`GET /v1/discovered-works?project_id=…&offset=0&limit=100` provides bounded pagination (maximum200). `GET /v1/discovered-works/{id}` adds aliases, observations and saved-reading links. Normal workspace authorization applies to every path. Intake requires a researcher role.

Intake creates no capture, representation, block, document or evidence. `content_scope` is `metadata_only` or `abstract`, and `evidence_eligible` and `fulltext_ready` remain false. Arbitrary metadata fields such as `fulltext_ready: true` cannot change those derived fields.

## Reviewed display corrections

Provider observations are immutable; known wrong display metadata need not stay wrong. An editor can submit `POST /v1/discovered-works/{id}/metadata-reviews` with `expected_revision`, `changes` (title/authors/year/venue), a reason, and a source URL or verified evidence IDs. The append-only review records the server-derived reviewer identity/role. Stale revisions fail409, and researchers cannot apply corrections. Current catalogue and export projections show the reviewed values; `provider_display`, original observations and review history preserve what changed and why. Later provider intake cannot silently overwrite the review. This does not rewrite DOI/arXiv identities or imply that the entire bibliography has been validated.

## Saved content and explicit promotion

Use the ordinary ingestion/upload pipeline to save actual bytes. It accepts `content_scope=unspecified|abstract|fulltext`; the declaration is preserved with the immutable capture. Metadata-only material must use discovery intake instead of document ingestion. An unspecified saved document is not automatically called scholarly full text.

Link a completed source document with:

`POST /v1/discovered-works/{id}/readings`

Body fields: `document_id`, `content_scope` (`abstract` or `fulltext`), `review_note`, and `fulltext_reviewed=true` for fulltext links. The last flag attests that the operator inspected the saved artifact and verified that it contains full text; it does not attest that every paragraph was read.

The server verifies same workspace/project, source-document identity, immutable representation hash and actual saved-byte integrity. It also binds the capture's source URL/provenance to a recorded source locator or canonical DOI/arXiv alias. A boolean without that acquired content and provenance cannot promote a lead. Known abstract-only content cannot be upgraded to full text. A reading link pins the exact capture and representation, and repeats are idempotent; it does not follow later source updates.

`fulltext_ready=true` means an acquired, parsed, provenance-bound artifact has been explicitly identified as full text. `fulltext_read` remains false because the system does not certify completion of reading. Research reports must separately state which sections/windows were actually inspected. The software cannot prove semantic document completeness merely from a caller's declaration, filename, page count or an open-access label.

## Abstract-only citations

Saving an abstract through ingestion permits exact, hash-verified evidence only within that declared scope. Evidence locators and retrieval results retain `content_scope`. A proposal using abstract evidence must explicitly set the claim's `scope` to `abstract`; otherwise validation rejects it. Research adapters transmit the abstract-only scope and mark resulting claims with that limitation. This is a programmatic provenance boundary, not an automatic semantic-entailment judgment.

## Archive-first processing and failures

Acquisition commits the immutable Source/Capture and original bytes before parsing. The operation exposes `result_json.capture_id`, `source_id` and `archival_complete` even if later parsing or quality gates fail. No research document, block, index or evidence is published until processing succeeds.

Authorized users can inspect `/v1/captures/{id}`, download `/v1/captures/{id}/raw`, and export the project including failed captures. Capture inspection includes processing operations/errors and a derived `processing_ready` flag. Parser failure observations are separate records; captures are never mutated into another parser result.

Retry of an archived ingestion reads its stored bytes instead of fetching the original URL again. `/v1/captures/{id}/reprocess` can apply an explicit bounded pipeline configuration, including parser memory/time settings, without requiring a collector file or live website. Missing/corrupt archive bytes fail closed. Successful acquisitions made during web research persist independently if later research finds no answer or a model/proposal step fails; that does not publish a report or make an unparsed capture searchable.

## Deployment

Stop writers/workers, back up the deployment, run `alembic upgrade head` (scholarly schema revisions0008–0009), then restart API, workers and dispatcher against the same database/object store. No credentials, provider calls or schedules are created by the migration. A project export preserves scholarly works, aliases, observations and pinned reading links; private provenance and abstracts belong in private exports, not public source distributions.

## Agent interface

The [scholarly MCP interface](integrations/scholarly-mcp.md) wraps these same domain routes with bounded metadata/record windows, intake and append-only screening observations, and validated saved-reading links. Default researcher tools cannot overwrite reviewed canonical metadata. An explicit editor-mode tool uses the existing editor authorization and expected-revision contract. There is no separate agent datastore or alternate readiness policy.
