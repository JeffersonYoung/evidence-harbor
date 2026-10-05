"""Reliable database-to-Temporal bridge.

A deterministic workflow ID makes a crash after start but before DB acknowledgement
safe to replay. Never acknowledges a dispatch whose start outcome is uncertain.
"""

import asyncio
import logging
import os

from sqlalchemy import select
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from .db import SessionLocal
from .models import Operation, OperationDispatch, utcnow
from .workflows import IngestionWorkflow, ResearchCycleWorkflow


async def dispatch_once(client, session_factory=SessionLocal) -> int:
    sent = 0
    with session_factory() as session:
        records = list(
            session.scalars(
                select(OperationDispatch)
                .where(OperationDispatch.status == "pending")
                .order_by(OperationDispatch.created_at)
                .limit(50)
            )
        )
        for record in records:
            operation = session.get(Operation, record.operation_id)
            if operation is None:
                continue
            workflow_type = ResearchCycleWorkflow if operation.kind == "research" else IngestionWorkflow
            record.attempts += 1
            try:
                await client.start_workflow(
                    workflow_type.run,
                    operation.id,
                    id=f"operation-{operation.id}" + (f"-g{record.generation}" if record.generation else ""),
                    task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "research-workspace"),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass  # Existing/running/completed workflow owns this immutable operation ID.
            except Exception as exc:
                record.last_error = type(exc).__name__
                session.commit()
                logging.getLogger(__name__).exception("Dispatch start failed for %s", operation.id)
                continue
            record.status = "delivered"
            record.delivered_at = utcnow()
            record.last_error = None
            session.commit()
            sent += 1
    return sent


async def main():
    from .outbox import deliver_outbox_once
    from .scheduling import reconcile_schedules

    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            client = await Client.connect(
                os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
                namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
            )
            while True:
                await dispatch_once(client)
                await reconcile_schedules(
                    client, SessionLocal, os.getenv("TEMPORAL_TASK_QUEUE", "research-workspace")
                )
                await asyncio.to_thread(deliver_outbox_once, SessionLocal)
                await asyncio.sleep(2)
        except Exception:
            logging.getLogger(__name__).exception(
                "Dispatcher unavailable; pending records retained; retrying"
            )
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
