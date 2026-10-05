"""Temporal worker: python -m backend.worker. No second scheduler or task queue."""

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from .workflows import IngestionWorkflow, ResearchCycleWorkflow, SourceSyncWorkflow

HEARTBEAT_INTERVAL_SECONDS = 10


@contextmanager
def _activity_heartbeats(resource_id: str):
    """Keep synchronous work live without moving its database/session across threads."""
    stopped = threading.Event()

    def pulse():
        while not stopped.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                activity.heartbeat({"resource_id": resource_id})
            except Exception:
                logging.getLogger(__name__).exception("Activity heartbeat failed")
                return

    context = copy_context()
    heartbeat_thread = threading.Thread(
        target=context.run,
        args=(pulse,),
        name=f"activity-heartbeat-{activity.info().activity_id}",
        daemon=True,
    )
    try:
        # Both startup and cleanup are protected from injected sync cancellation.
        with activity.shield_thread_cancel_exception():
            activity.heartbeat({"resource_id": resource_id})
            heartbeat_thread.start()
        yield
    finally:
        with activity.shield_thread_cancel_exception():
            stopped.set()
            if heartbeat_thread.ident is not None:
                # SDK synchronous heartbeat calls have their own bounded wait.
                heartbeat_thread.join()


@activity.defn(name="execute_operation")
def execute_operation_activity(operation_id: str) -> dict:
    from .domain import execute_operation

    with _activity_heartbeats(operation_id):
        result = execute_operation(operation_id, force_resume=True)
        if isinstance(result, dict) and result.get("status") == "failed":
            from temporalio.exceptions import ApplicationError

            raise ApplicationError(result.get("error") or "Operation failed", type="OperationFailed")
        return result if isinstance(result, dict) else {"operation_id": operation_id}


@activity.defn(name="execute_operation_v2")
def execute_operation_activity_v2(operation_id: str) -> dict:
    from temporalio.exceptions import ApplicationError

    from .domain import execute_operation

    with _activity_heartbeats(operation_id):
        result = execute_operation(operation_id, force_resume=True, defer_failure=True)
        if isinstance(result, dict) and result.get("status") in {"retrying", "failed"}:
            retryable = (result.get("result_json") or {}).get("error_retryable") is True
            raise ApplicationError(
                result.get("error") or "Operation attempt failed",
                type="OperationAttemptFailed",
                non_retryable=not retryable,
            )
        return result if isinstance(result, dict) else {"operation_id": operation_id}


@activity.defn(name="finalize_operation_failure")
def finalize_operation_failure_activity(operation_id: str) -> dict:
    from sqlalchemy import select
    from temporalio.exceptions import ApplicationError

    from . import domain, models
    from .db import SessionLocal

    with _activity_heartbeats(operation_id), SessionLocal() as session:
        operation = session.scalar(
            select(models.Operation).where(models.Operation.id == operation_id).with_for_update()
        )
        if operation is None:
            raise ApplicationError("Operation not found", type="OperationNotFound", non_retryable=True)
        if operation.status == "succeeded" or (operation.status == "failed" and operation.completed_at):
            return domain.serialize(operation)
        message = operation.error or "Operation activity exhausted retries or stopped before completion"
        domain.fail_operation(session, operation, message)
        if operation.kind == "research":
            run = domain.scoped(session, models.ResearchRun, operation.input_json["run_id"],
                                operation.workspace_id, operation.project_id)
            if run.status != "succeeded":
                run.status, run.error, run.completed_at = "failed", message, models.utcnow()
                domain.run_event(session, run, "run.failed", {"error": message})
        session.commit()
        return domain.serialize(operation)


@activity.defn(name="sync_source")
def sync_source_activity(watch_id: str) -> dict:
    from .db import SessionLocal
    from .scheduling import enqueue_watch

    info = activity.info()
    # Stable across activity retries, new for every schedule tick/workflow execution.
    invocation = f"{info.workflow_id}:{info.workflow_run_id}"
    with _activity_heartbeats(watch_id), SessionLocal() as session:
        result = enqueue_watch(session, watch_id, invocation)
        session.commit()
        return result


async def main():
    from .config import Settings

    Settings()  # Validate declared durability before connecting or polling any work.
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
            activities=[execute_operation_activity, execute_operation_activity_v2,
                        finalize_operation_failure_activity, sync_source_activity],
            activity_executor=executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
