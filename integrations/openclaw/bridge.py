"""One-shot JSON-to-MCP transport adapter. All domain work stays in backend.mcp_server."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def invoke(request):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "backend.mcp_server"],
        env={
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "LANG",
                "EVIDENCEHARBOR_API_URL",
                "EVIDENCEHARBOR_API_TOKEN",
            }
        },
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(request["name"], request.get("arguments", {}))
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.read(1_048_577))
        print(json.dumps(asyncio.run(asyncio.wait_for(invoke(request), timeout=60))))
    except Exception:  # noqa: BLE001 - Boundary intentionally redacts third-party error details.
        # Do not reflect exceptions that might contain server URLs, headers or tokens.
        print(
            json.dumps(
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "EvidenceHarbor MCP call failed. Check the Python installation, API availability and credential permissions.",
                        }
                    ],
                }
            )
        )
        sys.exit(1)
