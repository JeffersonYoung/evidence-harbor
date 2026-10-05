# Codex Desktop and Claude Code

First follow [the common prerequisites and generator](README.md). Below, `/absolute/new/path/evidenceharbor-agents` means its output directory.

## Codex

The package uses the documented `.codex-plugin/plugin.json` compatibility layout, a skill, and `.mcp.json`. Its MCP command is the absolute dedicated Python interpreter; only the API URL and token environment variables are forwarded explicitly.

```sh
codex plugin marketplace add /absolute/new/path/evidenceharbor-agents/codex
codex plugin list --available --json
```

Then open the Desktop Plugins Directory, select EvidenceHarbor's local marketplace and install the plugin. Restart the app when required by your client version. Ensure the Desktop process actually receives the two environment variables before calling a tool. Ask for one known project's context as a read-only smoke test; inspect tool names and approval settings before allowing write tools.

If local plugins are unavailable in your installed Codex surface, merge the generated `codex-mcp.toml` section into that client's config instead. Do not overwrite unrelated settings, and do not enable both routes simultaneously. The fallback deliberately requests tool approval. A ChatGPT web/global-directory installation is a different distribution path and is not implemented or claimed here.

## Claude Code

```sh
claude plugin validate /absolute/new/path/evidenceharbor-agents/claude
claude plugin marketplace add /absolute/new/path/evidenceharbor-agents/claude
claude plugin install evidenceharbor@evidenceharbor-local
claude plugin list
```

Start Claude Code with the API URL and existing token available in its process environment. Inspect `/mcp`, then invoke `/evidenceharbor:evidenceharbor` with a known project ID. For a temporary development load rather than an installed marketplace, use:

```sh
claude --plugin-dir /absolute/new/path/evidenceharbor-agents/claude/agent-plugin
```

This is a stdio package. It requires the installed backend Python package on the same computer as the client, but the authenticated REST API can run elsewhere. Nothing is downloaded or installed by the plugin itself.

## Official references

Documentation reviewed 2026-10-04. Installed client versions and organization policies can change availability; the clients' own validation remains authoritative.

- [OpenAI plugin packaging and local marketplaces](https://developers.openai.com/plugins/build/plugins)
- [Codex MCP configuration and environment forwarding](https://developers.openai.com/codex/mcp)
- [Claude plugin manifests](https://code.claude.com/docs/en/plugins-reference)
- [Claude local marketplace installation](https://code.claude.com/docs/en/plugin-marketplaces)
