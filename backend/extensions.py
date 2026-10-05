"""Safe pipeline revisions, sample runs, provider discovery and external leads."""

import difflib
import os
from dataclasses import asdict

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, ForeignKey, String, event, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, get_session
from .models import Identified, Scoped, reject_mutation


class PipelineRevision(Identified, Scoped, Base):
    __tablename__ = "pipeline_revisions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    config_json: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))


for operation in ("before_update", "before_delete"):
    event.listen(PipelineRevision, operation, reject_mutation)


def pipeline_test_operation(session, operation):
    from . import domain, models
    from .processing import process_document

    revision = domain.scoped(
        session,
        PipelineRevision,
        operation.input_json["pipeline_id"],
        operation.workspace_id,
        operation.project_id,
    )
    capture = domain.scoped(
        session,
        models.Capture,
        operation.input_json["capture_id"],
        operation.workspace_id,
        operation.project_id,
    )
    raw = domain.read_blob(capture, session.info.get("settings"))
    result = process_document(raw, capture.media_type, pipeline_config=revision.config_json)
    previous = session.scalar(
        select(models.Representation)
        .where(models.Representation.capture_id == capture.id)
        .order_by(models.Representation.created_at.desc())
    )
    diff = list(
        difflib.unified_diff(
            (previous.text if previous else "").splitlines(),
            result.extracted_text.splitlines(),
            fromfile="published",
            tofile="sample",
            lineterm="",
        )
    )
    return {
        "pipeline_id": revision.id,
        "capture_id": capture.id,
        "published": False,
        "quality": result.quality,
        "gates": result.gates,
        "warnings": result.warnings,
        "parser_version": result.parser_version,
        "config_hash": result.config_hash,
        "content": result.extracted_text,
        "blocks": result.blocks,
        "diff": diff[:2000],
        "diff_truncated": len(diff) > 2000,
    }


def create_router(require_workspace, require_role):
    from . import domain, models
    from .processing import STAGE_OPTIONS, STAGE_ORDER, load_pipeline
    from .providers import ProviderError, ProviderNotConfigured, get_search_provider, provider_status

    router = APIRouter(prefix="/v1", tags=["processing configuration"])

    class PipelineInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        project_id: str
        name: str = Field(min_length=1, max_length=120)
        config: dict | str

    class TrialInput(BaseModel):
        capture_id: str

    class SearchInput(BaseModel):
        query: str = Field(min_length=1, max_length=1000)
        limit: int = Field(default=5, ge=1, le=20)
        provider: str | None = None

    class VectorInput(BaseModel):
        provider: str = "local_hash"

    class HybridInput(BaseModel):
        project_id: str
        query: str = Field(min_length=1, max_length=1000)
        generation_ids: list[str] = Field(default_factory=list, max_length=20)
        provider: str = "local_hash"
        limit: int = Field(default=20, ge=1, le=100)

    @router.post("/representations/{representation_id}/vector-index", status_code=202)
    def vector_index(
        representation_id: str,
        payload: VectorInput,
        request: Request,
        background: BackgroundTasks,
        workspace=Depends(require_role("admin")),
        session=Depends(get_session),
    ):
        representation = domain.scoped(session, models.Representation, representation_id, workspace)
        if payload.provider not in {"local_hash", "openai"}:
            raise domain.DomainError("Unknown embedding provider", 422)
        if session.bind.dialect.name != "postgresql":
            raise domain.DomainError(
                "Vector indexing requires PostgreSQL and ENABLE_PGVECTOR=true migration", 503
            )
        operation = domain.create_operation(
            session,
            workspace,
            representation.project_id,
            {"representation_id": representation_id, "provider": payload.provider},
            kind="vector_index",
        )
        session.commit()
        if request.app.state.settings.inline_worker:
            background.add_task(
                domain.execute_operation,
                operation.id,
                request.app.state.session_factory,
                settings_override=request.app.state.settings,
            )
        return domain.serialize(operation)

    @router.post("/hybrid-search")
    def hybrid(
        payload: HybridInput, workspace=Depends(require_role("researcher")), session=Depends(get_session)
    ):
        from .vector_index import hybrid_search

        if not payload.generation_ids:
            return domain.search(session, workspace, payload.project_id, payload.query, payload.limit)
        if session.bind.dialect.name != "postgresql":
            raise domain.DomainError("Vector retrieval requires PostgreSQL", 503)
        return hybrid_search(
            session,
            workspace,
            payload.project_id,
            payload.query,
            payload.generation_ids,
            payload.provider,
            payload.limit,
        )

    @router.get("/index-generations")
    def generations(project_id: str, workspace=Depends(require_workspace), session=Depends(get_session)):
        domain.project(session, workspace, project_id)
        return {
            "items": [
                domain.serialize(g)
                for g in session.scalars(
                    select(models.IndexGeneration).where(
                        models.IndexGeneration.workspace_id == workspace,
                        models.IndexGeneration.project_id == project_id,
                    )
                )
            ]
        }

    @router.get("/captures/{capture_id}/raw")
    def raw_capture(capture_id: str, workspace=Depends(require_workspace), session=Depends(get_session)):
        from fastapi import Response

        capture = domain.scoped(session, models.Capture, capture_id, workspace)
        raw = domain.read_blob(capture, session.info.get("settings"))
        # No archived HTML is ever executed on the application origin.
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="capture-{capture.id}.bin"',
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @router.get("/providers")
    def providers(workspace=Depends(require_workspace)):
        return provider_status()

    @router.post("/web-search")
    def web_search(payload: SearchInput, workspace=Depends(require_role("researcher"))):
        try:
            provider = get_search_provider(payload.provider or os.getenv("SEARCH_PROVIDER", ""))
            results = provider.search(payload.query, payload.limit)
        except ProviderNotConfigured as exc:
            raise domain.DomainError(str(exc), 503)
        except ProviderError as exc:
            raise domain.DomainError(str(exc), 502)
        return {
            "results": [asdict(item) for item in results],
            "status": "unverified_leads",
            "evidence_eligible": False,
            "provider": provider.name,
        }

    @router.get("/pipeline-registry")
    def registry(workspace=Depends(require_workspace)):
        return {
            "version": 1,
            "stage_order": list(STAGE_ORDER),
            "options": {
                stage: {
                    name: {
                        "type": typ.__name__,
                        "default": default,
                        "allowed": sorted(bounds) if isinstance(bounds, set) else list(bounds),
                    }
                    for name, (typ, default, bounds) in options.items()
                }
                for stage, options in STAGE_OPTIONS.items()
            },
            "arbitrary_code_allowed": False,
        }

    @router.get("/pipelines")
    def pipelines(project_id: str, workspace=Depends(require_workspace), session=Depends(get_session)):
        domain.project(session, workspace, project_id)
        return {
            "items": [
                domain.serialize(r)
                for r in session.scalars(
                    select(PipelineRevision)
                    .where(
                        PipelineRevision.workspace_id == workspace, PipelineRevision.project_id == project_id
                    )
                    .order_by(PipelineRevision.created_at.desc())
                )
            ]
        }

    @router.post("/pipelines", status_code=201)
    def create_pipeline(
        payload: PipelineInput, workspace=Depends(require_role("admin")), session=Depends(get_session)
    ):
        domain.project(session, workspace, payload.project_id)
        config = load_pipeline(payload.config)
        item = PipelineRevision(
            workspace_id=workspace,
            project_id=payload.project_id,
            name=payload.name,
            config_hash=config.config_hash,
            config_json={
                "version": config.version,
                "stages": [{"name": stage.name, "options": stage.options} for stage in config.stages],
            },
        )
        session.add(item)
        session.commit()
        return domain.serialize(item)

    @router.post("/pipelines/{pipeline_id}/test-runs", status_code=202)
    def test_run(
        pipeline_id: str,
        payload: TrialInput,
        request: Request,
        background: BackgroundTasks,
        workspace=Depends(require_role("admin")),
        session=Depends(get_session),
    ):
        revision = domain.scoped(session, PipelineRevision, pipeline_id, workspace)
        domain.scoped(session, models.Capture, payload.capture_id, workspace, revision.project_id)
        operation = domain.create_operation(
            session,
            workspace,
            revision.project_id,
            {"pipeline_id": pipeline_id, "capture_id": payload.capture_id},
            kind="pipeline_test",
        )
        session.commit()
        if request.app.state.settings.inline_worker:
            background.add_task(
                domain.execute_operation,
                operation.id,
                request.app.state.session_factory,
                settings_override=request.app.state.settings,
            )
        return domain.serialize(operation)

    return router
