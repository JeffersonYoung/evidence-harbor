# EvidenceHarbor agent packages

Start with [installation, security and validation notes](../docs/integrations/README.md).

- `prepare.py`: build separate local Codex and Claude marketplaces with an absolute interpreter
- `agent-plugin/`: shared skill and compatibility manifest templates (generate before installing)
- `openclaw/`: native opt-in tool adapter to the same stdio MCP server
- `export_tool_schemas.py`: regenerate schemas from `backend.mcp_server.mcp`
- `tests/`: packaging, schema parity, registration and real transport tests

No npm/PyPI publication, agent configuration changes or credential provisioning is performed here.
