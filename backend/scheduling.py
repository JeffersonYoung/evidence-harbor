"""Persistent source-watch configuration, reconciled into Temporal schedules.

Only administrative API callers configure schedules. Research tools cannot mutate them.
"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Identified, Scoped


class SourceWatch(Identified, Scoped, Base):
    __tablename__ = "source_watches"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    locator: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pipeline_config: Mapped[dict] = mapped_column(JSON, default=dict)
    source_type: Mapped[str] = mapped_column(String(20), default="http")
    allowed_domains: Mapped[list] = mapped_column(JSON, default=list)
    connector_state: Mapped[dict] = mapped_column(JSON, default=dict)
    max_items: Mapped[int] = mapped_column(Integer, default=50)
    research_on_change: Mapped[bool] = mapped_column(Boolean, default=False)
    research_question: Mapped[str] = mapped_column(Text, default="")
    research_config: Mapped[dict] = mapped_column(JSON, default=dict)
    classification: Mapped[str] = mapped_column(String(20), default="internal")
    schedule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


def watch_version(watch: SourceWatch) -> str:
    data = [
        watch.locator,
        watch.interval_seconds,
        watch.enabled,
        watch.pipeline_config,
        watch.research_on_change,
        watch.research_question,
        watch.research_config,
        watch.classification,
        watch.source_type,
        watch.allowed_domains,
        watch.max_items,
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def reconcile_schedules(client, session_factory, task_queue: str) -> int:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleState,
        ScheduleUpdate,
    )
    from temporalio.service import RPCError, RPCStatusCode

    from .workflows import SourceSyncWorkflow

    reconciled = 0
    with session_factory() as session:
        watches = list(session.scalars(select(SourceWatch)))
        for watch in watches:
            version = watch_version(watch)
            if watch.schedule_version == version:
                continue
            schedule = Schedule(
                action=ScheduleActionStartWorkflow(
                    SourceSyncWorkflow.run, watch.id, id=f"watch-{watch.id}", task_queue=task_queue
                ),
                spec=ScheduleSpec(
                    intervals=[ScheduleIntervalSpec(every=timedelta(seconds=watch.interval_seconds))]
                ),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(hours=1)),
                state=ScheduleState(
                    paused=not watch.enabled, note="Managed by EvidenceHarbor source-watch configuration"
                ),
            )
            schedule_id = f"source-watch-{watch.id}"
            try:
                await client.create_schedule(schedule_id, schedule)
            except ScheduleAlreadyRunningError:
                await client.get_schedule_handle(schedule_id).update(
                    lambda _, value=schedule: ScheduleUpdate(value)
                )
            except RPCError as exc:
                if exc.status != RPCStatusCode.ALREADY_EXISTS:
                    raise
                await client.get_schedule_handle(schedule_id).update(
                    lambda _, value=schedule: ScheduleUpdate(value)
                )
            watch.schedule_version = version
            session.commit()
            reconciled += 1
    return reconciled


def enqueue_watch(session, watch_id: str, invocation_id: str) -> dict:
    """The invocation ID deduplicates activity redelivery, not future watch checks."""
    from . import domain

    watch = session.get(SourceWatch, watch_id)
    if watch is None or not watch.enabled:
        return {"status": "disabled", "watch_id": watch_id}
    if watch.source_type != "http":
        operation = domain.create_operation(
            session,
            watch.workspace_id,
            watch.project_id,
            {"watch_id": watch.id, "invocation_id": invocation_id},
            kind="connector_sync",
            idempotency_key=f"connector-sync:{watch.id}:{invocation_id}",
        )
        return {"operation_id": operation.id, "status": operation.status}
    # Runtime import keeps workflow definitions free of database imports.
    payload = {
        "type": "url",
        "project_id": watch.project_id,
        "url": watch.locator,
        "title": watch.title,
        "pipeline_config": watch.pipeline_config,
        "watch_id": watch.id,
        "classification": watch.classification,
    }
    operation = domain.create_operation(
        session,
        watch.workspace_id,
        watch.project_id,
        payload,
        idempotency_key=f"watch:{watch.id}:{invocation_id}",
    )
    return {"operation_id": operation.id, "status": operation.status}


def create_router(require_workspace, require_role):
    from fastapi import APIRouter, BackgroundTasks, Depends, Request
    from pydantic import BaseModel, ConfigDict, Field

    from . import domain
    from .db import get_session

    router = APIRouter(prefix="/v1/source-watches", tags=["source watches"])

    class WatchInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        project_id: str
        locator: str = Field(min_length=8, max_length=8192)
        title: str = Field(default="", max_length=1000)
        interval_seconds: int = Field(default=86400, ge=300, le=31536000)
        enabled: bool = True
        pipeline_config: dict = Field(default_factory=dict)
        source_type: str = Field(default="http", pattern="^(http|rss|sitemap)$")
        allowed_domains: list[str] = Field(default_factory=list, max_length=20)
        max_items: int = Field(default=50, ge=1, le=100)
        research_on_change: bool = False
        research_question: str = Field(default="", max_length=50000)
        research_config: dict = Field(default_factory=dict)
        classification: str = Field(default="internal", pattern="^(public|internal|sensitive)$")

    class WatchUpdate(BaseModel):
        model_config = ConfigDict(extra="forbid")
        interval_seconds: int | None = Field(default=None, ge=300, le=31536000)
        enabled: bool | None = None
        research_on_change: bool | None = None
        research_question: str | None = Field(default=None, max_length=50000)

    @router.get("")
    def list_watches(project_id: str, workspace=Depends(require_workspace), session=Depends(get_session)):
        domain.project(session, workspace, project_id)
        return {
            "items": [
                domain.serialize(w)
                for w in session.scalars(
                    select(SourceWatch).where(
                        SourceWatch.workspace_id == workspace, SourceWatch.project_id == project_id
                    )
                )
            ]
        }

    @router.post("", status_code=201)
    def create_watch(
        payload: WatchInput, workspace=Depends(require_role("admin")), session=Depends(get_session)
    ):
        from .security import validate_source_config_url

        domain.project(session, workspace, payload.project_id)
        validate_source_config_url(payload.locator)
        if payload.pipeline_config:
            # Pipeline schema is validated by the same registry used by ingestion.
            from .processing import load_pipeline

            load_pipeline(payload.pipeline_config)
        if payload.research_config:
            from .schemas import ResearchRunCreate

            ResearchRunCreate(
                project_id=payload.project_id,
                question=payload.research_question or "Review new evidence",
                **payload.research_config,
            )
        watch = SourceWatch(workspace_id=workspace, **payload.model_dump())
        session.add(watch)
        session.commit()
        return domain.serialize(watch)

    @router.patch("/{watch_id}")
    def update_watch(
        watch_id: str,
        payload: WatchUpdate,
        workspace=Depends(require_role("admin")),
        session=Depends(get_session),
    ):
        watch = domain.scoped(session, SourceWatch, watch_id, workspace)
        for key, value in payload.model_dump(exclude_none=True).items():
            setattr(watch, key, value)
        session.commit()
        return domain.serialize(watch)

    @router.post("/{watch_id}/check", status_code=202)
    def check_watch(
        watch_id: str,
        request: Request,
        background: BackgroundTasks,
        workspace=Depends(require_role("admin")),
        session=Depends(get_session),
    ):
        from .models import new_id

        domain.scoped(session, SourceWatch, watch_id, workspace)
        result = enqueue_watch(session, watch_id, new_id())
        session.commit()
        if result.get("operation_id") and request.app.state.settings.inline_worker:
            background.add_task(
                domain.execute_operation,
                result["operation_id"],
                request.app.state.session_factory,
                settings_override=request.app.state.settings,
            )
        return result

    return router
