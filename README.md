# EvidenceHarbor · 证据港

A self-hosted continuous research workspace: versioned source material, configurable preprocessing, immutable evidence, reviewed report revisions, and durable research jobs. 中文界面，支持中英文资料。

**The invariant:** a report cites the saved capture, representation and exact structural block that was actually read. Changing a source, parser or embedding model never silently changes old evidence.

## Run locally

Requirements: Python 3.12+, Node 22+, or Docker Compose. No model/search credentials are needed for the offline extractive research baseline.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export DATABASE_URL=sqlite:///./var/research.sqlite
export OBJECTS_DIR=./var/objects
mkdir -p var
alembic upgrade head
python -m backend.auth yourname --role admin
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
cd apps/web
npm ci
API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Open http://localhost:3000 and sign in. Account creation prompts for a password of at least 12 characters. There is no public signup or default password. `LOCAL_WORKER=true` (the local default) uses the same domain services in-process; it is **not durable Temporal execution**. Use the Compose/Temporal configuration below for recovery across worker restarts. SQLite is for local development and evaluations; PostgreSQL is the supported production database.

A deliberately insecure, loopback-only demonstration can set `DEMO_MODE=true`. Do not expose demo mode to a network. A browser static-token fallback also requires explicit `SINGLE_USER_MODE=true`; normal account mode never falls back to an admin token after logout.

## Docker Compose

```sh
cp .env.example .env
# Set the three required database passwords. API_TOKEN is optional for service clients.
# Never commit .env. Create the first password account with the command below.
docker compose up --build -d
docker compose exec api python -m backend.auth yourname --role admin
```

The stack contains Next.js, FastAPI, PostgreSQL/pgvector, Temporal, a Temporal worker, and a transactional-outbox dispatcher. It binds web/API/Temporal host ports to loopback only. Compose requires three operator-supplied database passwords and contains no usable default secrets. The application and Temporal use separate databases and non-superuser roles. The API token is not passed to the browser in account mode.

The local Dockerfile installs the lightweight PDF reader; install `.[pdf]` and provision Docling/OCR model assets offline for layout/OCR parsing. The optional bundled browser service is documented in [deploy/renderer](deploy/renderer/README.md). Browser rendering is an optional isolated service, never a browser launched inside the API process.

## What you can do

1. Create a research project and its open questions
2. Upload saved HTML, text, PDF or safe DOCX, or submit a public URL
3. Inspect immutable captures and parser representations, quality flags and exact block locations
4. Search Chinese/English text, read the underlying blocks and register verified evidence
5. Run bounded local/external research, or let a connected agent propose an update through MCP
6. Review all or selected claims; publish with compare-and-swap protection and manual section locks
7. Configure source watches, safe pipeline revisions/sample runs, and project event subscriptions
8. Export the project as a ZIP with original blobs, versioned relationships and Markdown projections

The default research provider is explicitly **deterministic extractive**, not a frontier research model. Its purpose is an operational, auditable offline path. External provider adapters need explicit opt-in, credentials, selected model and data-sharing policy. Search results remain unverified leads until archived and processed.

## Architecture

See the [detailed system design](docs/ARCHITECTURE.md) for data invariants, execution flows, trust boundaries and recovery semantics.

- `apps/web/`: one Next.js/React/TypeScript frontend, HttpOnly session proxy
- `backend/api.py`, `domain.py`, `models.py`: FastAPI/Pydantic + SQLAlchemy domain services
- `backend/processing.py`, `parsers.py`: registered safe YAML stages; offline extraction and publication gates
- `backend/storage.py`: atomic SHA-256 local store and optional S3-compatible store
- `backend/security.py`: bounded, DNS-pinned public HTTP transport with redirect revalidation
- `backend/workflows.py`, `worker.py`, `dispatcher.py`: Temporal execution, database outbox, stable dispatch generations
- `backend/scheduling.py`, `outbox.py`: durable source schedules and at-least-once webhooks
- `backend/vector_index.py`: versioned pgvector embeddings and reciprocal-rank fusion
- `backend/auth.py`: scrypt password accounts, hashed/revocable sessions, login throttling
- `backend/mcp_server.py`: official MCP SDK stdio server, same REST permissions and business logic
- `integrations/`: Codex Desktop/CLI, Claude Code and OpenClaw distribution

Source identity includes workspace/project, normalized locator, access scope and representation variant. Immutable captures link to previous captures; immutable representations and index generations coexist. Evidence has block-local Unicode code-point offsets and a SHA-256 quote hash; PDF page/bounding-box precision is recorded only when actually available. A mutable report projection points to immutable historical document/report versions.

## External providers and data policy

Configure secrets only through the deployment environment. Do not paste keys into the UI, Markdown exports, agent prompts or version control.

- `ENABLE_EXTERNAL_PROVIDERS=true` enables configured external adapters
- `EXTERNAL_EVIDENCE_POLICY=allow-reviewed` or `redact-contact-data` is additionally required
- `EXTERNAL_ALLOWED_CLASSIFICATIONS=public` is the default; imported sources are `internal` unless explicitly classified
- Sensitive evidence additionally requires `ALLOW_SENSITIVE_EXTERNAL_DATA=true`
- OpenAI-compatible model: `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`
- Search: `SEARCH_PROVIDER=brave` + `BRAVE_SEARCH_API_KEY`, or SearXNG settings described in `backend/providers.py`
- Monetary limits require model-specific configured pricing. Actual usage and price-based estimates are distinct from a provider bill

Research configuration carries model, reasoning effort, document/search/tool/time limits and a cost ceiling. A durable reservation prevents automatic retries from silently issuing a second paid call after an uncertain result. Cached successful responses can be reused; an uncertain paid attempt requires operator review and a new explicit run. Provider execution is never guaranteed byte-for-byte deterministic.

Contact regex redaction is deliberately limited and is **not a comprehensive DLP or legal compliance system**. Operators must review data-sharing policy for their deployment.

## Scholarly discovery and archive-first processing

The [scholarly discovery API](docs/SCHOLARLY_DISCOVERY.md) stores deduplicated DOI/arXiv/title-author leads with immutable provenance. Metadata and abstracts are never counted as papers read. Fulltext availability requires a saved, verified, provenance-bound source and explicit artifact review; reading completion is not inferred. Failed parsing still leaves an authorized raw capture for inspection, export and bounded reprocessing.

## Vector retrieval

PostgreSQL must have the `vector` extension. Set `ENABLE_PGVECTOR=true` when migrating. To enable it after an earlier migration, run `python -m backend.vector_index --initialize-schema` with the operator database connection. Build a generation using `POST /v1/representations/{id}/vector-index`, list `/v1/index-generations?project_id=…`, and retrieve via `/v1/hybrid-search` with explicit generation IDs.

`local_hash` is an offline feature-hashing baseline, clearly labelled as **not a learned semantic model**. Optional `openai` embeddings require an explicitly configured model, dimensions, per-million price, maximum batch reservation and reviewed data policy. Generations fix their model and dimensions; mismatched query models are rejected. Exact cosine retrieval is implemented; ANN tuning and empirical semantic-retrieval quality are not claimed.

## Agents and reproducible evaluations

```sh
EVIDENCEHARBOR_API_URL=http://127.0.0.1:8000 \
EVIDENCEHARBOR_API_TOKEN='researcher-role-token' \
python -m backend.mcp_server
```

Use researcher-role credentials for agents. The [scholarly MCP extension](docs/integrations/scholarly-mcp.md) provides 15 default tools and one explicitly opt-in, API-role-checked editor metadata tool. MCP exposes source acquisition, search/read, exact evidence registration and proposals; project context is bounded metadata, and document reads are bounded by default with version-pinned pagination and structural-block selection; it does not expose publication, schedule changes, arbitrary SQL/shell or credentials. See [agent integration instructions](docs/integrations/README.md).

`python scripts/eh.py --help` is a REST CLI. `scripts/empirical_harness.py` runs the identical FastAPI routes in-process against a persistent external SQLite directory, useful when an evaluation environment isolates network namespaces. It migrates the database before each invocation, checks imported hashes, preserves declared provenance, resumes failed imports, and returns nonzero if any import fails. This harness is explicitly local/TestClient, not a substitute for PostgreSQL/Temporal integration tests.

Raw third-party sources, repeated model outputs and evaluation databases must remain outside this public repository. Public evaluation reports should contain source URLs/hashes, bounded quotations, metrics and licensed fixtures only.

## Verification

```sh
pytest -q
python -m compileall -q backend scripts
cd apps/web && npm run build
cd ../..
python -m unittest discover -s integrations/tests -v
node --test integrations/tests/openclaw.test.mjs
```

Real PostgreSQL/pgvector and Temporal tests are opt-in. With a local PostgreSQL distribution containing pgvector and the official Temporal CLI/SDK download:

```sh
python scripts/run_integration.py --postgres-bin /path/to/postgresql/bin
```

This launches temporary services, runs real FTS/CAS/idempotency/vector/schedule/retry tests, and shuts them down. Docker Compose itself, live S3, credentialed external providers, Docling with production OCR assets and a production isolated renderer require environment-specific acceptance tests; passed unit/adaptor tests do not establish those integrations are deployed.

The three-domain empirical case study and controlled eight-run model/effort experiment are in [the empirical report](docs/empirical/REPORT.md).

See [coverage and limits](docs/coverage.md), [security boundaries](docs/security.md) and [operations](docs/operations.md). No open-source license has been selected; public availability alone does not grant reuse rights.
