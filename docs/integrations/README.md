# Agent distribution

EvidenceHarbor ships **source-installable adapters**, not a published npm/PyPI package or public marketplace listing. A local Python MCP process forwards every operation to the same authenticated REST backend used by the web application. There is no second datastore or research engine in the adapters.

| Client | Delivery | Status |
|---|---|---|
| Codex Desktop / CLI | Local marketplace, compatibility plugin manifest, skill and stdio MCP | Codex 0.159.2 accepted and listed generated marketplace in isolated temporary home; Desktop installation not tested |
| Claude Code | Local marketplace, plugin manifest, skill and stdio MCP | Generated package checked; real Claude validator/install not run (client unavailable) |
| OpenClaw | Local JavaScript tool plugin, nine opt-in tools, Python stdio MCP bridge | Registration/transport tests and package contents checked; real OpenClaw validator/install not run (host unavailable) |

No real client settings, persistent access, API tokens, public registries or publishing configuration are changed by the build helpers. Marketplace discovery was checked using a temporary isolated Codex home without credentials; no plugin was enabled there. The remaining install/enable steps below are operator actions.

## Common prerequisite

Deploy/start the backend following the main repository setup. Install this checkout into a **dedicated Python 3.12+ virtual environment**, using a reviewed commit for a repeatable source checkout:

```sh
git clone https://github.com/JeffersonYoung/evidence-harbor.git
cd evidence-harbor
# For a repeatable release, git checkout <reviewed-commit-sha> first.
python3.12 -m venv .venv
.venv/bin/python -m pip install .
```

The source commit fixes EvidenceHarbor code; current Python dependency ranges are **not a transitive dependency lock**. Use the deployment's approved dependency lock or constraints for fully reproducible environments. There are no auto-updates or install-time network scripts in these adapters.

An operator supplies an existing **researcher-role**, project/workspace-scoped credential through the host's environment or secret facility. Set EVIDENCEHARBOR_API_URL to your trusted API origin and EVIDENCEHARBOR_API_TOKEN to that credential. Do not put real tokens in config examples, chat, source control or process arguments. Do not use the backend's administrator convenience API_TOKEN. Creating a token or granting new access is a separate security action.

Use HTTPS for a remote API; HTTP is only appropriate for a trusted local loopback deployment. These variables must exist in the actual client/Gateway process, which can differ from a terminal shell's environment. The adapters do not load `.env` files automatically or provide an OAuth/login flow.

## Capabilities and authority

The core MCP server exposes get_project_context, search_library, read_document, get_evidence, create_evidence, search_web, ingest_source, get_ingestion_status and propose_research_update. OpenClaw prefixes their names with `evidenceharbor_`; arguments are identical. A checked-in schema snapshot is generated from the live server definition and tested for drift. All authorization, quoting validation and persistence remain in the backend.

Proposals are reviewable drafts. No adapter offers report publication, schedule changes, credential management or administrative tools. Ingestion and evidence/proposal creation are writes; review the client's tool approval settings. A client allowlist is additional protection, not a replacement for backend authorization. Retrieved source text is untrusted.

## Build and verify

From the repository root:

```sh
.venv/bin/python -m integrations.prepare --output /absolute/new/path/evidenceharbor-agents
.venv/bin/python -m unittest discover -s integrations/tests -v
node --test integrations/tests/openclaw.test.mjs
npm pack --dry-run --ignore-scripts ./integrations/openclaw
```

The generator refuses to overwrite an existing destination, keeps virtualenv interpreter paths intact and emits separate Codex/Claude packages plus a direct Codex MCP configuration. It does not capture tokens. Keep the environment at that absolute path; rebuild the package if it moves. Do not install the raw `agent-plugin` template directly: the generator configures each client's environment forwarding.

Maintainers changing the MCP interface must review the allowed capability list, run `.venv/bin/python -m integrations.export_tool_schemas`, run the tests, and bump plugin versions for releases. The test with an HTTP fixture verifies Node → Python MCP client → core stdio MCP → authenticated HTTP forwarding. It is not a live service or real client installation test.

See [Codex and Claude](codex-claude.md) and [OpenClaw](openclaw.md) for installation and validation steps. No remote HTTP MCP endpoint is provided in this release; do not configure the REST API URL as if it were an MCP endpoint.

## Proposal contract detail

Every `claims[i].text` must occur verbatim as an exact substring of the submitted `content`. A claim list that only paraphrases the report is rejected with HTTP 422. Build report content from the actual claim text, then add narrative context and valid saved evidence references. A stale `base_version` yields HTTP 409 and must be re-read/reviewed rather than silently overwritten. These guards establish traceable submitted claims, not semantic proof that the cited evidence supports them.
