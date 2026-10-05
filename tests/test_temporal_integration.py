"""Actual Temporal server tests, not mocked workflow replay."""

import asyncio
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from backend import domain, models
from backend.db import Base
from backend.dispatcher import dispatch_once
from backend.scheduling import SourceWatch, reconcile_schedules
from backend.worker import (
    execute_operation_activity,
    execute_operation_activity_v2,
    finalize_operation_failure_activity,
    sync_source_activity,
)
from backend.workflows import IngestionWorkflow, ResearchCycleWorkflow, SourceSyncWorkflow


@pytest.fixture
def temporal_db(tmp_path, monkeypatch):
    if not os.getenv("EVIDENCEHARBOR_TEST_TEMPORAL"):
        pytest.skip("Real Temporal not configured")
    engine = create_engine(f"sqlite:///{tmp_path}/state.sqlite", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    import backend.db

    monkeypatch.setattr(backend.db, "SessionLocal", factory)
    monkeypatch.setattr(domain.settings, "blob_dir", tmp_path / "objects")
    monkeypatch.setenv("OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("TEMPORAL_ADDRESS", os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"])
    yield factory
    engine.dispose()


@pytest.mark.asyncio
async def test_temporal_durable_dispatch_retry_and_idempotency(temporal_db, monkeypatch):
    client = await Client.connect(os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"])
    queue = "test-" + uuid4().hex
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", queue)
    with temporal_db() as session:
        project = domain.create_project(session, "w", "Recovery")
        operation = domain.create_operation(
            session,
            "w",
            project.id,
            {"type": "text", "text": "Temporal durable evidence remains readable after a retry."},
        )
        session.commit()
        operation_id = operation.id
    # Dispatch before a worker exists: Temporal durably retains the work.
    assert await dispatch_once(client, temporal_db) == 1
    assert await dispatch_once(client, temporal_db) == 0
    handle = client.get_workflow_handle(f"operation-{operation_id}")
    import backend.processing

    original = backend.processing.ingest_operation
    calls = []

    def interrupted_once(session, operation):
        calls.append(operation.id)
        if len(calls) == 1:
            raise OSError("simulated worker activity failure before publication")
        return original(session, operation)

    monkeypatch.setattr(backend.processing, "ingest_operation", interrupted_once)
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[IngestionWorkflow, ResearchCycleWorkflow, SourceSyncWorkflow],
            activities=[execute_operation_activity, execute_operation_activity_v2,
                        finalize_operation_failure_activity, sync_source_activity],
            activity_executor=pool,
        ):
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                with temporal_db() as session:
                    snapshot = session.get(models.Operation, operation_id)
                    if snapshot.status == "retrying":
                        assert snapshot.completed_at is None
                        assert snapshot.result_json["error_retryable"] is True
                        break
                assert asyncio.get_running_loop().time() < deadline, "Transient attempt was not exposed as retrying"
                await asyncio.sleep(0.01)
            result = await asyncio.wait_for(handle.result(), timeout=45)
    assert result["status"] == "succeeded"
    assert len(calls) == 2
    with temporal_db() as session:
        assert len(list(session.scalars(select(models.Capture)))) == 1
        record = session.scalar(select(models.OperationDispatch))
        record.status = "pending"
        session.commit()  # Simulated lost dispatch acknowledgement.
    assert await dispatch_once(client, temporal_db) == 1
    with temporal_db() as session:
        assert len(list(session.scalars(select(models.Capture)))) == 1
    history = await handle.fetch_history()
    assert len(history.events) > 5
    scheduled = [event.activity_task_scheduled_event_attributes for event in history.events
                 if event.HasField("activity_task_scheduled_event_attributes")]
    assert len(scheduled) == 1
    assert scheduled[0].heartbeat_timeout.ToTimedelta() == timedelta(seconds=30)
    assert scheduled[0].start_to_close_timeout.ToTimedelta() == timedelta(minutes=20)


@pytest.mark.asyncio
async def test_real_temporal_schedule_reconcile_and_pause(temporal_db):
    client = await Client.connect(os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"])
    with temporal_db() as session:
        project = domain.create_project(session, "w", "Sources")
        watch = SourceWatch(
            workspace_id="w", project_id=project.id, locator="https://example.com/", interval_seconds=86400
        )
        session.add(watch)
        session.commit()
        watch_id = watch.id
    schedule_id = f"source-watch-{watch_id}"
    try:
        assert await reconcile_schedules(client, temporal_db, "test-" + uuid4().hex) == 1
        assert await reconcile_schedules(client, temporal_db, "unused") == 0
        info = await client.get_schedule_handle(schedule_id).describe()
        assert not info.schedule.state.paused
        with temporal_db() as session:
            watch = session.get(SourceWatch, watch_id)
            watch.enabled = False
            session.commit()
        assert await reconcile_schedules(client, temporal_db, "unused") == 1
        info = await client.get_schedule_handle(schedule_id).describe()
        assert info.schedule.state.paused
    finally:
        await client.get_schedule_handle(schedule_id).delete()


@pytest.mark.asyncio
async def test_terminal_workflow_failure_then_explicit_api_retry_new_generation(temporal_db, monkeypatch):
    from fastapi.testclient import TestClient
    from temporalio.client import WorkflowFailureError

    import backend.processing
    from backend.api import create_app
    from backend.config import Settings

    client = await Client.connect(os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"])
    queue = "retry-" + uuid4().hex
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", queue)
    with temporal_db() as session:
        project = domain.create_project(session, "w", "Terminal retry")
        operation = domain.create_operation(
            session,
            "w",
            project.id,
            {"type": "text", "text": "Durable explicit retry must create one capture after recovery."},
        )
        session.commit()
        operation_id = operation.id
    original = backend.processing.ingest_operation
    failing = [True]
    attempts = []

    def temporarily_broken(session, operation):
        attempts.append(operation.id)
        if failing[0]:
            raise ValueError("Temporary fixture parser failure")
        return original(session, operation)

    monkeypatch.setattr(backend.processing, "ingest_operation", temporarily_broken)
    await dispatch_once(client, temporal_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[IngestionWorkflow],
            activities=[execute_operation_activity, execute_operation_activity_v2,
                        finalize_operation_failure_activity],
            activity_executor=pool,
        ):
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(client.get_workflow_handle(f"operation-{operation_id}").result(), 45)
            assert len(attempts) == 4
            with temporal_db() as session:
                assert session.get(models.Operation, operation_id).status == "failed"
                assert session.get(models.Operation, operation_id).completed_at is not None
                assert len(list(session.scalars(select(models.Capture)))) == 1
                assert not list(session.scalars(select(models.Representation)))
            failing[0] = False
            settings = Settings(
                api_tokens={"fixture-admin": "w"}, inline_worker=False, blob_dir=domain.settings.blob_dir
            )
            with TestClient(create_app(settings, temporal_db)) as api:
                response = api.post(
                    f"/v1/operations/{operation_id}/retry", headers={"Authorization": "Bearer fixture-admin"}
                )
                assert response.status_code == 202, response.text
            with temporal_db() as session:
                dispatch = session.scalar(select(models.OperationDispatch))
                assert dispatch.generation == 1
            assert await dispatch_once(client, temporal_db) == 1
            result = await asyncio.wait_for(
                client.get_workflow_handle(f"operation-{operation_id}-g1").result(), 30
            )
            assert result["status"] == "succeeded"
    with temporal_db() as session:
        assert len(list(session.scalars(select(models.Capture)))) == 1


@pytest.mark.parametrize("outcome", ["succeeded", "failed_result", "raised_exception"])
def test_sync_activity_heartbeats_copy_context_and_stop_on_every_exit(monkeypatch, outcome):
    import backend.worker as worker_module

    assert worker_module.HEARTBEAT_INTERVAL_SECONDS == 10
    # Exercise real thread waits at a short test cadence; no Temporal clocks are altered.
    monkeypatch.setattr(worker_module, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    inherited = ContextVar("heartbeat-test-context", default=None)
    token = inherited.set("copied-context")
    background_seen = threading.Event()
    beats, heartbeat_threads = [], []
    environment = ActivityEnvironment()

    def on_heartbeat(details):
        beats.append((details, activity.info().activity_id, inherited.get()))
        current = threading.current_thread()
        if current.name.startswith("activity-heartbeat-"):
            heartbeat_threads.append(current)
            background_seen.set()

    environment.on_heartbeat = on_heartbeat

    def operation(identifier, *, force_resume):
        assert identifier == "fixture-operation" and force_resume is True
        assert background_seen.wait(2), "Copied activity context must allow a background heartbeat"
        if outcome == "raised_exception":
            raise ValueError("Synthetic operation error")
        return {"status": "failed" if outcome == "failed_result" else "succeeded", "error": "fixture"}

    monkeypatch.setattr(domain, "execute_operation", operation)
    try:
        if outcome == "succeeded":
            assert environment.run(execute_operation_activity, "fixture-operation")["status"] == "succeeded"
        else:
            expected = ApplicationError if outcome == "failed_result" else ValueError
            with pytest.raises(expected):
                environment.run(execute_operation_activity, "fixture-operation")
        assert len(beats) >= 2
        assert all(details == {"resource_id": "fixture-operation"} and context == "copied-context"
                   for details, _identifier, context in beats)
        assert heartbeat_threads and all(not thread.is_alive() for thread in heartbeat_threads)
        completed_count = len(beats)
        threading.Event().wait(0.03)
        assert len(beats) == completed_count
    finally:
        inherited.reset(token)


@pytest.mark.parametrize("patched", [False, True])
@pytest.mark.parametrize("workflow_type,minutes", [
    (IngestionWorkflow, 20), (ResearchCycleWorkflow, 20), (SourceSyncWorkflow, 2),
])
@pytest.mark.asyncio
async def test_activity_timeout_patch_preserves_legacy_commands(monkeypatch, workflow_type, minutes, patched):
    calls, patches = [], []

    def patch(identifier):
        patches.append(identifier)
        return patched if identifier == "activity-heartbeats-v1" else False

    async def execute(name, identifier, **options):
        calls.append((name, identifier, options))
        return {"operation_id": identifier}

    monkeypatch.setattr(workflow, "patched", patch)
    monkeypatch.setattr(workflow, "execute_activity", execute)
    assert await workflow_type().run("fixture") == {"operation_id": "fixture"}
    expected_patches = (["operation-lifecycle-v2"] if workflow_type is not SourceSyncWorkflow else [])
    assert patches == [*expected_patches, "activity-heartbeats-v1"]
    assert calls[0][2]["start_to_close_timeout"] == timedelta(minutes=minutes)
    assert calls[0][2]["heartbeat_timeout"] == (timedelta(seconds=30) if patched else None)


@workflow.defn(name="IngestionWorkflow", sandboxed=False)
class LegacyIngestionWorkflow:
    """The deployed pre-heartbeat command shape, used only to create replay history."""

    @workflow.run
    async def run(self, operation_id: str) -> dict:
        return await workflow.execute_activity(
            "execute_operation", operation_id,
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=RetryPolicy(maximum_attempts=4, initial_interval=timedelta(seconds=2)),
        )


@pytest.mark.asyncio
async def test_new_workflow_code_replays_real_history_created_without_heartbeat_timeout(temporal_db):
    client = await Client.connect(os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"])
    queue = "legacy-heartbeat-" + uuid4().hex
    with temporal_db() as session:
        project = domain.create_project(session, "w", "Legacy replay")
        operation = domain.create_operation(session, "w", project.id, {
            "type": "text", "text": "An actual saved source verifies compatibility with old workflow history.",
        })
        session.commit()
        operation_id = operation.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        async with Worker(client, task_queue=queue, workflows=[LegacyIngestionWorkflow],
                          activities=[execute_operation_activity], activity_executor=pool):
            handle = await client.start_workflow(LegacyIngestionWorkflow.run, operation_id,
                id="legacy-" + uuid4().hex, task_queue=queue)
            result = await asyncio.wait_for(handle.result(), timeout=30)
            assert result["status"] == "succeeded"
    history = await handle.fetch_history()
    scheduled = [event.activity_task_scheduled_event_attributes for event in history.events
                 if event.HasField("activity_task_scheduled_event_attributes")]
    assert len(scheduled) == 1
    assert scheduled[0].heartbeat_timeout.ToTimedelta() == timedelta(0)
    assert scheduled[0].start_to_close_timeout.ToTimedelta() == timedelta(minutes=20)
    await Replayer(workflows=[IngestionWorkflow], workflow_runner=SandboxedWorkflowRunner()).replay_workflow(history)


@pytest.mark.parametrize("retryable", [False, True])
def test_v2_activity_uses_persisted_retry_classification_without_terminal_finalization(monkeypatch, retryable):
    def operation(identifier, *, force_resume, defer_failure):
        assert identifier == "fixture" and force_resume and defer_failure
        return {"status": "retrying", "error": "Persisted attempt error",
                "completed_at": None, "result_json": {"error_retryable": retryable}}

    monkeypatch.setattr(domain, "execute_operation", operation)
    with pytest.raises(ApplicationError) as raised:
        ActivityEnvironment().run(execute_operation_activity_v2, "fixture")
    assert raised.value.non_retryable is not retryable
    assert raised.value.type == "OperationAttemptFailed"


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_type", [IngestionWorkflow, ResearchCycleWorkflow])
async def test_v2_workflow_finalizes_only_after_activity_retry_exhaustion(monkeypatch, workflow_type):
    calls = []
    terminal = ActivityError("Terminal attempt", scheduled_event_id=1, started_event_id=2,
        identity="fixture", activity_type="execute_operation_v2", activity_id="1", retry_state=None)

    async def execute(name, identifier, **options):
        calls.append((name, identifier, options))
        if name == "execute_operation_v2":
            raise terminal
        return {"status": "failed"}

    monkeypatch.setattr(workflow, "patched", lambda _name: True)
    monkeypatch.setattr(workflow, "execute_activity", execute)
    with pytest.raises(ActivityError) as raised:
        await workflow_type().run("fixture")
    assert raised.value is terminal
    assert [entry[0] for entry in calls] == ["execute_operation_v2", "finalize_operation_failure"]
    assert calls[0][2]["heartbeat_timeout"] == timedelta(seconds=30)
    assert calls[0][2]["start_to_close_timeout"] == timedelta(minutes=20)
    assert calls[1][2]["retry_policy"].maximum_attempts == 5
    assert calls[1][2]["start_to_close_timeout"] == timedelta(minutes=2)


def test_terminal_finalizer_is_idempotent_and_closes_research_run(runtime):
    with runtime.session_factory() as session:
        project = domain.create_project(session, "workspace-a", "Finalization")
        run, operation = domain.create_research_run(session, "workspace-a", project.id, "What is supported?")
        operation.status = run.status = "retrying"
        operation.error = run.error = "Persisted terminal attempt failure"
        session.commit()
        operation_id, run_id = operation.id, run.id
    environment = ActivityEnvironment()
    first = environment.run(finalize_operation_failure_activity, operation_id)
    with runtime.session_factory() as session:
        persisted = domain.serialize(session.get(models.Operation, operation_id))
    again = environment.run(finalize_operation_failure_activity, operation_id)
    assert persisted == again
    assert first["status"] == "failed" and first["completed_at"] is not None
    assert first["error"] == "Persisted terminal attempt failure"
    with runtime.session_factory() as session:
        run = session.get(models.ResearchRun, run_id)
        assert run.status == "failed" and run.completed_at is not None
        events = list(session.scalars(select(models.RunEvent).where(
            models.RunEvent.run_id == run_id, models.RunEvent.type == "run.failed")))
        assert len(events) == 1


def test_terminal_finalizer_never_overwrites_a_succeeded_operation(runtime):
    with runtime.session_factory() as session:
        project = domain.create_project(session, "workspace-a", "Success is final")
        operation = domain.create_operation(session, "workspace-a", project.id,
            {"type": "text", "text": "A completed source operation must never be replaced by late failure."})
        session.commit()
        operation_id = operation.id
    succeeded = domain.execute_operation(operation_id, runtime.session_factory, settings_override=runtime.settings)
    assert succeeded["status"] == "succeeded"
    with runtime.session_factory() as session:
        persisted = domain.serialize(session.get(models.Operation, operation_id))
    assert ActivityEnvironment().run(finalize_operation_failure_activity, operation_id) == persisted


@pytest.mark.asyncio
async def test_worker_process_loss_recovers_on_real_heartbeat_timeout_without_clock_changes(temporal_db, tmp_path):
    """Kill only a disposable test worker; wait for the genuine 30-second server timeout."""
    client = await Client.connect(os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"])
    queue = "crash-heartbeat-" + uuid4().hex
    marker = tmp_path / "processing-started"
    with temporal_db() as session:
        project = domain.create_project(session, "w", "Worker crash recovery")
        operation = domain.create_operation(session, "w", project.id, {
            "type": "text", "text": "A crashed worker leaves one durable archive for the next actual attempt.",
        })
        session.commit()
        operation_id = operation.id
        database_url = str(session.bind.url)
    handle = await client.start_workflow(IngestionWorkflow.run, operation_id,
        id="crash-" + uuid4().hex, task_queue=queue)
    child_code = """
import asyncio, os, threading
from pathlib import Path
import backend.processing
from backend.worker import main
original = backend.processing.ingest_operation
def stall_after_archival(session, operation):
    Path(os.environ['TEST_PROCESSING_MARKER']).write_text(operation.id)
    threading.Event().wait(180)
    return original(session, operation)
backend.processing.ingest_operation = stall_after_archival
asyncio.run(main())
"""
    env = {**os.environ, "DATABASE_URL": database_url, "BLOB_DIR": str(domain.settings.blob_dir),
           "OBJECTS_DIR": str(domain.settings.blob_dir), "RAW_STORAGE_DIR": str(domain.settings.blob_dir),
           "STORAGE_BACKEND": "local", "LOCAL_WORKER": "false", "ENABLE_EXTERNAL_PROVIDERS": "false",
           "TEMPORAL_ADDRESS": os.environ["EVIDENCEHARBOR_TEST_TEMPORAL"], "TEMPORAL_TASK_QUEUE": queue,
           "TEST_PROCESSING_MARKER": str(marker)}
    process = None
    try:
        with (tmp_path / "crashed-worker.log").open("wb") as output:
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", child_code,
                cwd=str(Path(__file__).resolve().parents[1]), env=env, stdout=output, stderr=output)
            deadline = asyncio.get_running_loop().time() + 20
            while not marker.exists():
                assert process.returncode is None, (tmp_path / "crashed-worker.log").read_text()
                assert asyncio.get_running_loop().time() < deadline, "Disposable worker did not begin processing"
                await asyncio.sleep(0.05)
            description = await handle.describe()
            pending = description.raw_description.pending_activities
            assert len(pending) == 1 and pending[0].attempt == 1
            assert pending[0].HasField("last_heartbeat_time")
            with temporal_db() as session:
                operation = session.get(models.Operation, operation_id)
                assert operation.status == "running" and operation.completed_at is None
                capture_id = operation.result_json["capture_id"]
            process.kill()
            await process.wait()
            killed_at = time.monotonic()
            attempts = []

            @activity.defn(name="execute_operation_v2")
            def record_recovery_attempt(identifier):
                attempts.append(activity.info().attempt)
                return execute_operation_activity_v2(identifier)

            with ThreadPoolExecutor(max_workers=2) as pool:
                async with Worker(client, task_queue=queue, workflows=[IngestionWorkflow],
                                  activities=[record_recovery_attempt, finalize_operation_failure_activity],
                                  activity_executor=pool):
                    result = await asyncio.wait_for(handle.result(), timeout=75)
            assert 20 <= time.monotonic() - killed_at < 75
            assert attempts == [2]
            assert result["status"] == "succeeded" and result["result_json"]["capture_id"] == capture_id
            with temporal_db() as session:
                assert len(list(session.scalars(select(models.Capture)))) == 1
                assert len(list(session.scalars(select(models.Representation)))) == 1
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
