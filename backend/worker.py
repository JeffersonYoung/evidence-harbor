"""Temporal worker: python -m backend.worker. No second scheduler or task queue."""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from .workflows import IngestionWorkflow, ResearchCycleWorkflow, SourceSyncWorkflow


@activity.defn(name="execute_operation")
def execute_operation_activity(operation_id: str) -> dict:
    from .domain import execute_operation

    result = execute_operation(operation_id, force_resume=True)
    if isinstance(result, dict) and result.get("status") == "failed":
        from temporalio.exceptions import ApplicationError

        raise ApplicationError(result.get("error") or "Operation failed", type="OperationFailed")
    return result if isinstance(result, dict) else {"operation_id": operation_id}


@activity.defn(name="sync_source")
def sync_source_activity(watch_id: str) -> dict:
    from .db import SessionLocal
    from .scheduling import enqueue_watch

    info = activity.info()
    # Stable across activity retries, new for every schedule tick/workflow execution.
    invocation = f"{info.workflow_id}:{info.workflow_run_id}"
    with SessionLocal() as session:
        result = enqueue_watch(session, watch_id, invocation)
        session.commit()
        return result


async def main():
    logging.basicConfig(level=logging.INFO)
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    while True:
        try:
            client = await Client.connect(address, namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
            break
        except Exception:
            logging.getLogger(__name__).exception("Temporal connection failed; retrying in 5 seconds")
            await asyncio.sleep(5)
    with ThreadPoolExecutor(max_workers=int(os.getenv("WORKER_CONCURRENCY", "4"))) as executor:
        worker = Worker(
            client,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "research-workspace"),
            workflows=[IngestionWorkflow, ResearchCycleWorkflow, SourceSyncWorkflow],
            activities=[execute_operation_activity, sync_source_activity],
            activity_executor=executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
