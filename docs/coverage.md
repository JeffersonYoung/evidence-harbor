# Implementation and acceptance matrix

This is an implementation inventory, not a claim that every deployment adapter has been exercised against a live service. Automated checks distinguish exact reference integrity from semantic support for a conclusion.

| Area | Implemented | Acceptance / remaining boundary |
|---|---|---|
| Versioned asset model | Source identity scopes/variants; previous capture links; observations; immutable representations, blocks, index generations, evidence and report versions | ORM and SQL-trigger mutation tests; raw tampering rejected; unchanged input deduplicated |
| Original storage | SHA-256 content addressing; atomic no-overwrite local writes; S3 adapter; raw download as attachment; ZIP project export | Raw Source/Capture committed before parse; failed originals remain downloadable/exportable and retries reuse archived bytes. Local and S3 protocol tests; live service status tracked separately |
| HTML extraction | Offline Trafilatura plus explicit builtin fallback; include/exclude CSS; extraction metadata | Real fixture parsing; archived scripts never execute |
| DOCX | Bounded OOXML ZIP/XML parser, offline paragraphs/table cells, exact Unicode offsets | Synthetic real packages and isolated parser tested; external relationships, tracked changes, encrypted/macro packages rejected; no page/layout precision claimed |
| Scholarly discovery | Durable DOI/arXiv/title-author identities, immutable observations/aliases, editor-reviewed CAS display corrections, scoped saved-reading links | 1000-record synthetic API intake remains unread; provider metadata cannot grant fulltext eligibility; actual corpus acceptance reported separately |
| PDF | Pypdf text/page extraction; optional Docling layout, tables, local OCR assets and bboxes | Actual text PDFs tested, including empirical papers; Docling/OCR model deployment not live-tested |
| Dynamic browser | Bundled non-root Playwright HTTP service, public-only CONNECT proxy and separate internal Docker network; context-wide WebSocket/service-worker blocking | Service/proxy guard tests; actual Chromium/Docker isolation acceptance blocked by environment, not claimed deployed |
| Parser isolation | Separate bounded subprocess, CPU/address-space/output/wall timeout; offline model flags, credentials removed | Resource-limit adapter tests; not a replacement for OS/container network sandboxing |
| Pipelines | Versioned safe YAML/JSON, registered typed stages, normalization/contact redaction, source-map/quality/index gates; sample runs/diff | Mandatory stage sequence is deliberate; arbitrary user code and a free-form DAG designer are not implemented |
| Retrieval | Same jieba query/index tokenizer; PostgreSQL GIN FTS; pgvector exact cosine generations; model/dimension verification; RRF endpoint | Real PostgreSQL/pgvector tests; local hash vectors are NOT semantic embeddings; no semantic recall benchmark claimed |
| Evidence | Version/block/offset/hash, page/bbox where available; contradictory/supporting/limiting links; source classifications | Strict programmatic integrity checks; bounded Unicode/version/block reads preserve original citation coordinates; semantic entailment remains a model/human judgment |
| Research | Bounded plans/configuration, local extractive and external-model providers, usage/output storage, proposal CAS, immutable revisions, manual heading locks, selected-claim review | External providers need credentials/consent/pricing; uncertain paid attempts fail closed, never silently spend again |
| Continuous operation | Temporal ingestion/research/source-sync workflows, HTTP/RSS/Atom/sitemap schedules, change/impact records, review questions and opt-in incremental runs | Real Temporal durable queue, retry, scheduling, legacy replay, heartbeat worker-loss recovery and pause tests; source novelty/importance is not guaranteed |
| Dispatch/notifications | Atomic operation dispatch records, stable workflow IDs and explicit retry generations; project outbox and webhook backoff | At-least-once, not exactly-once; receivers deduplicate event IDs; no notification third party configured in this build |
| Accounts | Password/scrypt, hashed/revocable sessions, throttling, workspace+role checks across read/write paths | No anonymous admin fallback in normal web mode; SSO/MFA/password-recovery UI not implemented |
| Agent integration | Official MCP stdio SDK, 15 default evidence-first tools plus one explicitly opt-in editor metadata tool; Codex/Claude Code/OpenClaw installation assets | Package/schema/transport tests; explicit host approval/installation still required; no publish/admin MCP tool |
| UI | One Chinese-first responsive Next.js workspace, version reader, citations, proposal review, project/question/run views, account login, diagnostics | Production build/type checks and HTTP proxy tests; full Chromium visual interaction acceptance blocked by the execution environment |
| Recovery | Migrations, portable export, local snapshot/verified fresh restore; PostgreSQL/Temporal restore runbook | Local backup restoration tested; live disaster recovery of a production S3+Temporal deployment remains an operator acceptance task |
| Operations | Health, scoped diagnostics, process separation, Compose, CI checks, explicit failure states | Docker command unavailable in authoring environment; services verified directly, not a claimed Compose end-to-end pass |

## Deliberate boundaries

- No unbounded autonomous loop, automatic report publication, account credential sharing, arbitrary YAML execution or private-network fetch
- No claim that all pages, scanned PDFs, equations or tables parse correctly; quality flags/failures are surfaced
- Plain Markdown exports are projections. Editing an exported file does not overwrite the database or a report revision
- No graph database, second task queue, second independent scheduler, external agent-private chat state as authority, or multi-product backend dependency
- Raw third-party empirical corpora and repeated model outputs are excluded from the public repository
- Source licensing/retention policies and per-user deletion/privacy obligations require deployment policy. Normal immutable write protection is not a complete retention/legal-hold subsystem
- First-party source subscriptions support individual HTTP/PDF sources, RSS/Atom feeds and bounded sitemap discovery. Connector-specific authenticated crawling and project-release API cursors need additional dedicated adapters

See current test commands in the README and the eventual empirical report for measured model/effort comparisons. The model experiment is a small controlled case study, not a general ranking of model quality.
