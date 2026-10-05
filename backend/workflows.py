"""Replay-safe Temporal orchestration. All I/O is in Activities."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class IngestionWorkflow:
    @workflow.run
    async def run(self, operation_id: str) -> dict:
        return await workflow.execute_activity(
            "execute_operation",
            operation_id,
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=RetryPolicy(maximum_attempts=4, initial_interval=timedelta(seconds=2)),
        )


@workflow.defn
class ResearchCycleWorkflow:
    @workflow.run
    async def run(self, operation_id: str) -> dict:
        return await workflow.execute_activity(
            "execute_operation",
            operation_id,
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
        )


@workflow.defn
class SourceSyncWorkflow:
    @workflow.run
    async def run(self, subscription_id: str) -> dict:
        return await workflow.execute_activity(
            "sync_source",
            subscription_id,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=4),
        )
