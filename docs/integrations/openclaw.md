# OpenClaw adapter

This is a native tool plugin wrapping the **same** Python MCP server, not a separate research implementation. Its built JavaScript entrypoint imports `definePluginEntry` from the installed host. Each tool opens a short-lived official Python MCP client session. This favors simple isolation over process reuse and adds startup latency per call.

## Operator installation

Follow [common prerequisites](README.md), then configure the Gateway process with:

- EVIDENCEHARBOR_PYTHON: absolute path to the dedicated environment's Python executable
- EVIDENCEHARBOR_API_URL: trusted backend origin (HTTPS when remote)
- EVIDENCEHARBOR_API_TOKEN: existing restricted researcher credential

No secrets belong in `openclaw.plugin.json`. The plugin intentionally has no token-generation or credential-saving flow. It forwards only a small environment allowlist to its subprocess and never runs a shell command assembled from tool arguments.

Review the package, then install from the checkout:

```sh
openclaw plugins install /absolute/path/evidence-harbor/integrations/openclaw --link
openclaw plugins inspect evidenceharbor --runtime --json
openclaw plugins doctor --json
```

Merge this opt-in into your existing OpenClaw configuration, preserving its other entries:

```json
{
  "tools": {"allow": ["evidenceharbor"]}
}
```

For read-only use, allow individual names instead: `evidenceharbor_get_project_context`, `evidenceharbor_search_library`, `evidenceharbor_read_document`, `evidenceharbor_get_evidence`, `evidenceharbor_search_web`, `evidenceharbor_get_ingestion_status`. Do not allow `evidenceharbor_create_evidence`, `evidenceharbor_ingest_source` or `evidenceharbor_propose_research_update` unless writes are intended. Scope the backend token independently.

Node 24.16+ and Python 3.12+ are required. The implementation follows the documented focused SDK entrypoint and optional-tool contract. OpenClaw's plugin API is experimental: pin and test your actual host release before production use. The peer range expresses the documented interface baseline, not proof that every future host works.

## Validation and limits

Repository tests check nine registrations, unchanged MCP arguments, opt-in flags, backend error handling, schema parity and actual stdio/HTTP forwarding against a local fixture. `npm pack --dry-run` checks required JavaScript, Python, schemas, manifest and skill files are included. No third-party implementation code is vendored.

No OpenClaw host is installed in the implementation environment, so host loading, actual Gateway secret/environment forwarding and user-facing tool execution remain **unverified**. Run the commands above and a read-only project lookup after installation. The adapter has bounded input/output and timeouts; a failed call does not imply a write was rolled back. Inspect backend state before retrying a timed-out ingestion or proposal.

## Official references

- [Plugin building and optional tools](https://docs.openclaw.ai/plugins/building-plugins)
- [Manifest and capability declarations](https://docs.openclaw.ai/plugins/manifest)
- [Plugin installation CLI](https://docs.openclaw.ai/cli/plugins)
- [Official MCP client sessions](https://modelcontextprotocol.io/docs/develop/build-client)
