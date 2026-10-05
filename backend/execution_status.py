"""Read-only workflow truth; an activity attempt's failure is not workflow termination."""

import asyncio
import os

from temporalio.client import Client

TERMINAL_FAILURES = {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
TERMINAL = TERMINAL_FAILURES | {"COMPLETED", "CONTINUED_AS_NEW"}


async def describe_execution(operation, dispatch, inline_worker):
    if inline_worker:
        return {
            "backend": "inline",
            "available": True,
            "operation_status": operation.status,
            "terminal": operation.status in {"succeeded", "failed"},
        }
    generation = dispatch.generation if dispatch else 0
    identifier = f"operation-{operation.id}" + (f"-g{generation}" if generation else "")
    result = {
        "backend": "temporal",
        "operation_status": operation.status,
        "workflow_id": identifier,
        "generation": generation,
        "dispatch_status": dispatch.status if dispatch else None,
    }

    async def retrieve():
        client = await Client.connect(
            os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
            namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        )
        return await client.get_workflow_handle(identifier).describe()

    try:
        description = await asyncio.wait_for(retrieve(), timeout=5)
        status = description.status.name
        return {
            **result,
            "available": True,
            "workflow_status": status,
            "terminal": status in TERMINAL,
            "note": "Database status describes the latest activity attempt; Temporal owns durable completion",
        }
    except Exception as exc:  # noqa: BLE001 -- diagnostic endpoint must report unavailable, never infer success
        return {
            **result,
            "available": False,
            "workflow_status": "UNKNOWN",
            "terminal": None,
            "error": type(exc).__name__,
            "note": "Could not verify workflow state; do not infer completion",
        }
