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
    "list_discovered_works", "read_discovered_work", "read_scholarly_record",
    "intake_discovered_works", "record_scholarly_observation", "link_scholarly_reading",
}
EDITOR_ALLOWED = {"review_discovered_work_metadata"}


async def export():
    module = importlib.import_module("backend.mcp_server")
    server = module.mcp
    tools = await server.list_tools()
    actual = {tool.name for tool in tools}
    assert actual == ALLOWED | (EDITOR_ALLOWED if module.editor_tools_enabled() else set()), "Review tool exposure before updating the adapter"
    schemas = [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema}
        for tool in tools if tool.name in ALLOWED
    ]
    schemas.sort(key=lambda tool: tool["name"])
    (HERE / "openclaw/tool-schemas.json").write_text(json.dumps(schemas, indent=2) + "\n")
    from mcp.server.fastmcp import FastMCP

    editor_server = FastMCP("EvidenceHarbor editor schema")
    editor_server.add_tool(module.review_discovered_work_metadata)
    editor_tools = await editor_server.list_tools()
    editor_schemas = [{"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema} for tool in editor_tools]
    (HERE / "openclaw/editor-tool-schemas.json").write_text(json.dumps(editor_schemas, indent=2) + "\n")
    names = [f"evidenceharbor_{tool['name']}" for tool in schemas + editor_schemas]
    manifest = {
        "id": "evidenceharbor",
        "name": "EvidenceHarbor",
        "version": "0.2.0",
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
