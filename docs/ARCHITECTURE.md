# EvidenceHarbor architecture

## Product and authority boundaries

EvidenceHarbor is a self-hosted, continuous research workbench. It separates source acquisition, parsing, retrieval, claims, and publication so that a later source update cannot silently alter an earlier citation. Its database is the authority; Markdown is a portable projection, a model response is a proposal, and an agent's private conversation is never authoritative state.

The implementation is one Next.js/React/TypeScript client and one FastAPI domain backend, not multiple competing products. SQLAlchemy and Alembic manage the relational schema. SQLite supports offline development and evaluation; PostgreSQL supports production transactions, full-text indexes and optional pgvector. Temporal is the durable queue and schedule authority. An inline local worker is a convenience with explicitly weaker crash recovery.

## Execution topology

1. The browser sends same-origin requests to the Next.js proxy. An HttpOnly, SameSite session cookie is forwarded as backend authorization; session tokens are never returned in browser JSON. Cross-site mutations are rejected
2. FastAPI derives workspace and role from authentication, validates inputs, and invokes transport-independent domain services
3. A request transaction commits an operation and its dispatch outbox together. The API can return an operation ID before acquisition or research finishes
4. The dispatcher starts a deterministic Temporal workflow. Worker activities execute network/database/object-store operations and record durable results. Source-watch configuration is reconciled into Temporal schedules
5. Local or S3-compatible storage holds SHA-256-addressed immutable bytes. Database records link captures, representations, blocks, evidence and document revisions
6. MCP clients run the official SDK stdio bridge and call the same authorized REST API. They do not receive direct database or filesystem access

The Compose development topology uses separate database roles/databases for application and Temporal, distinct API/worker/dispatcher services, loopback host bindings, and no default usable passwords. Public internet hosting additionally needs TLS, trusted proxy settings, operational monitoring and deployment review.

## Persistent entities and invariants

A project belongs to a workspace. Every resource read/write checks that workspace; client-supplied scope cannot broaden permission. Sources are deduplicated using project, normalized locator, access scope and representation variant. Source classification is immutable and defaults to internal.

Acquisition commits raw Source/Capture identity before parsing; failed parse/quality gates leave raw bytes visible without any research document or search index. Retry and reprocess consume the saved capture rather than revisiting a live URL. A capture records actual bytes, content hash, media type, acquisition metadata and the preceding capture. Fetch observations record attempts and unchanged checks without inventing another source version. A representation fixes a capture, parser/configuration signature, normalized text and quality metadata. Structural blocks record offsets and available page/bounding-box locators. Optional index generations fix a representation, embedding model and dimensions.

Evidence stores the exact capture/block, block-local Unicode code-point offsets, quote and quote hash. Validation re-reads saved content; an evidence ID cannot be moved to a new parser result. Database triggers and ORM guards defend immutable resources from accidental mutation. They do not defend against a malicious privileged database operator.

A mutable document points to its current projection; immutable document versions preserve previous reports. Proposals carry claims, evidence relations, target document and base version. Publishing requires editor authority, evidence checks and compare-and-swap version agreement. Concurrent first publication is also guarded. Selected-claim review produces a human-approved subset; manually locked headings prevent unreviewed replacement of protected sections. Exact locator validity does not establish semantic entailment.

Scholarly discovery adds durable work identities and immutable alias/provenance observations. Bibliographic metadata and abstracts stay leads until actual content is acquired. Saved-reading links verify capture/representation integrity and recorded source provenance; fulltext availability is separate from completion of reading. Abstract-backed claims must explicitly retain abstract scope.

## Acquisition and preprocessing

Text, saved HTML/PDF uploads and public URLs enter the same operation path. Imported original URLs are declared provenance, not proof of an application fetch. Public URL transport rejects unsafe schemes, credentials, private/reserved addresses, non-web ports and unsafe redirects. DNS results are bounded, all addresses must be public, and the actual socket is pinned to a vetted address while TLS verifies the hostname.

RSS/Atom and bounded sitemaps discover URLs. Discovered titles/snippets are unverified leads. A lead becomes evidence only after normal acquisition, saved-byte parsing and locator validation. Feed cursors and queued ingestion operations commit together. Allowed domains and maximum item counts bound discovery.

The declarative pipeline uses a registered, ordered set of typed stages. YAML cannot import or execute arbitrary code. Versioned pipeline revisions support sample processing and comparison without publishing a new representation. Parsing occurs in a resource-bounded subprocess with offline model settings and credentials removed. This is resource isolation, not an OS-level network sandbox.

HTML uses offline extraction and never executes saved scripts. Pypdf provides real text/page extraction. Optional Docling adapters can provide layout, tables, OCR and bounding boxes when the operator provisions the required local assets. Missing precision is reported rather than fabricated. A bounded OOXML DOCX parser reads offline paragraphs and table cells, preserving canonical locators without invented pages or bounding boxes. It rejects ZIP resource hazards, external relationships and tracked revisions rather than fetching resources or silently flattening unsupported edits. Output quality and source-map gates must succeed before publication.

The optional renderer runs separately from the API. Its internal network can reach a public-only forward proxy; context-wide WebSockets/service workers are blocked and WebRTC is restricted to proxy traffic. CONNECT DNS resolution has a bounded worker/admission pool and client deadline. This code and its contract tests do not establish the actual container/browser egress boundary: live hostile-page acceptance remains required before enabling it.

## Retrieval and research

Chinese and English lexical indexing/querying use a consistent tokenizer. PostgreSQL GIN full-text search is the production lexical path. Optional pgvector generations implement exact cosine retrieval and reciprocal-rank fusion with lexical hits. `local_hash` is deterministic feature hashing, not a learned semantic model. Neural embedding quality and ANN capacity require deployment-specific evaluation.

Research configuration fixes provider/model/effort and bounded search, document, tool, duration, output-token and cost limits. The offline provider is deterministic extractive research. External model/search adapters require explicit enablement, credentials, reviewed evidence policy and permitted classifications. Source snippets cannot authorize tools, schedules or data sharing. Contact regex redaction is a limited convenience, not comprehensive DLP.

Paid calls reserve spend durably before transmission. A cached completed response can support recovery. An uncertain remote result is not silently reissued: the run stops for operator review rather than spending again. Pricing-based reservations and estimates are distinguished from provider invoices and measured token usage. Empirical subagent trials supplied in the public case study were controlled external executions, not proof that application provider credentials were configured.

### Progressive agent reading

MCP project takeover uses `/projects/{id}/context`, with bounded SQL column projections rather than eager document/proposal/operation bodies. Per-collection counts and pagination, a current report baseline and separate question listing support larger workspaces. The legacy project route remains for existing UI compatibility. MCP document reads default to an 8,000-code-point window (32,000 maximum), with optional immutable version selection and structural block targeting. Subsequent reads pin the returned version and follow explicit document-absolute continuation offsets. The API preserves full reads when no range arguments are supplied, keeping the existing UI contract. Cropped blocks preserve canonical IDs, coordinates and full-block hash separately from the visible-text hash; tokenizer text is omitted so it cannot leak unseen content. Evidence creation uses original block-local offsets, obtained by adding the visible block window's local start to the quote's position. EOF, unknown versions/blocks and invalid ranges have explicit behavior, and every read remains workspace-scoped. This bounds returned reading content; the current implementation still loads the document and its blocks before projecting the window, so it is not database-streaming pagination.

## Continuous updates and delivery

Source watches support enabled/paused HTTP, RSS/Atom and sitemap checks, classification, pipeline selection, cadence and bounded incremental research settings. Schedules use overlap skipping. Matching parser/configuration signatures permit content-change comparison; parser changes are not presented as source changes. Changes can create review questions and opt-in research runs, but never automatically publish reports.

Dispatch redelivery reuses stable workflow IDs. Explicit manual retry after terminal failure increments a persisted generation so a new workflow is possible. Project notifications use a separate transactional outbox, bounded retries and at-least-once delivery. Receivers must deduplicate event IDs. No default third-party notification recipient is configured.

Agent distribution includes Codex/Claude plugin manifests and an OpenClaw bridge. The nine MCP tools acquire, search/read, register evidence and propose updates. They exclude publication, administrative schedule changes, arbitrary shell/SQL and credentials. Host installation and a researcher-role API credential remain operator-controlled steps.

## Portability, recovery and operations

Project ZIP exports contain originals by content hash, relational identifiers, immutable citation anchors, pipeline revisions, non-runtime source-watch configuration and readable INDEX/BRIEF/REPORT/QUESTIONS/CHANGELOG/SOURCES projections. Account/provider credentials, notification destinations, connector cursors and live schedule state are excluded. Source content and provenance can still contain confidential data, so exports are private research artifacts. The archive is not an automatic importer or full deployment backup.

Local snapshot/restore verifies database/object integrity and supports relocation of the content root. Production recovery must coordinate application PostgreSQL, Temporal persistence/visibility and object storage plus separately protected operator secrets. A restored test system must not reconnect notification dispatchers to production destinations. Full production restoration with pending work remains an explicit acceptance task.

Diagnostics expose scoped health and failure states. Operators should monitor operation age, parser failures, missing/corrupt blobs, uncertain paid reservations, outbox/dispatch backlog, authorization failures and storage growth. SSO/MFA, recovery UI, retention/legal-hold tooling, production paging and capacity SLOs are not implemented product guarantees.

## Acceptance evidence

See [validation](VALIDATION.md), [coverage](coverage.md), [security](security.md), [operations](operations.md) and [three-domain empirical results](empirical/REPORT.md). Real PostgreSQL/pgvector and Temporal tests were executed directly without Docker. Docker/Chromium isolation, live S3 service, production OCR assets, credentialed application external providers and coordinated production restoration remain separately identified limits.
