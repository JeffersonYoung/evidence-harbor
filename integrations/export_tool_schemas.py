"""Refresh OpenClaw's MCP schema snapshot from the authoritative backend server."""

import asyncio
import importlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWED = {
    "get_project_context",
    "search_library",
    "read_document",
    "get_evidence",
    "search_web",
    "ingest_source",
    "get_ingestion_status",
    "propose_research_update",
    "create_evidence",
}


async def export():
    module = importlib.import_module("backend.mcp_server")
    server = module.mcp
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == ALLOWED, "Review tool exposure before updating the adapter"
    schemas = [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema}
        for tool in tools
    ]
    schemas.sort(key=lambda tool: tool["name"])
    (HERE / "openclaw/tool-schemas.json").write_text(json.dumps(schemas, indent=2) + "\n")
    names = [f"evidenceharbor_{tool['name']}" for tool in schemas]
    manifest = {
        "id": "evidenceharbor",
        "name": "EvidenceHarbor",
        "version": "0.1.0",
        "description": "Research tools using the EvidenceHarbor MCP server and existing API authorization",
        "categories": ["other"],
        "contracts": {"tools": names},
        "toolMetadata": {name: {"optional": True} for name in names},
        "configSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "skills": ["./skills"],
    }
    (HERE / "openclaw/openclaw.plugin.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(export())
