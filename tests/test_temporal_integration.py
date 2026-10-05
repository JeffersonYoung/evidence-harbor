"""Actual Temporal server tests, not mocked workflow replay."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from backend import domain, models
from backend.db import Base
from backend.dispatcher import dispatch_once
from backend.scheduling import SourceWatch, reconcile_schedules
from backend.worker import execute_operation_activity, sync_source_activity
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
            activities=[execute_operation_activity, sync_source_activity],
            activity_executor=pool,
        ):
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
            activities=[execute_operation_activity],
            activity_executor=pool,
        ):
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(client.get_workflow_handle(f"operation-{operation_id}").result(), 45)
            assert len(attempts) == 4
            with temporal_db() as session:
                assert session.get(models.Operation, operation_id).status == "failed"
                assert not list(session.scalars(select(models.Capture)))
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
