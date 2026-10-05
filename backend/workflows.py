"""Replay-safe Temporal orchestration. All I/O is in Activities."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


def activity_heartbeat_timeout():
    # Old histories replay their original command, including no heartbeat timeout.
    return timedelta(seconds=30) if workflow.patched("activity-heartbeats-v1") else None


async def execute_with_terminal_failure(operation_id: str, retry_policy: RetryPolicy) -> dict:
    try:
        return await workflow.execute_activity(
            "execute_operation_v2", operation_id,
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
    except ActivityError:
        await workflow.execute_activity(
            "finalize_operation_failure", operation_id,
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=2)),
        )
        raise


@workflow.defn
class IngestionWorkflow:
    @workflow.run
    async def run(self, operation_id: str) -> dict:
        retry_policy = RetryPolicy(maximum_attempts=4, initial_interval=timedelta(seconds=2))
        if workflow.patched("operation-lifecycle-v2"):
            return await execute_with_terminal_failure(operation_id, retry_policy)
        return await workflow.execute_activity(
            "execute_operation",
            operation_id,
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=activity_heartbeat_timeout(),
            retry_policy=retry_policy,
        )


@workflow.defn
class ResearchCycleWorkflow:
    @workflow.run
    async def run(self, operation_id: str) -> dict:
        retry_policy = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))
        if workflow.patched("operation-lifecycle-v2"):
            return await execute_with_terminal_failure(operation_id, retry_policy)
        return await workflow.execute_activity(
            "execute_operation",
            operation_id,
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=activity_heartbeat_timeout(),
            retry_policy=retry_policy,
        )


@workflow.defn
class SourceSyncWorkflow:
    @workflow.run
    async def run(self, subscription_id: str) -> dict:
        return await workflow.execute_activity(
            "sync_source",
            subscription_id,
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=activity_heartbeat_timeout(),
            retry_policy=RetryPolicy(maximum_attempts=4),
        )
