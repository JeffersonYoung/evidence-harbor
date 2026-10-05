"""FastAPI transport; every resource is scoped from an authenticated principal."""

from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import db
from . import domain as d
from . import models as m
from . import schemas as s
from .config import settings as default_settings

get_session = db.get_session


def get_principal(
    request: Request, session=Depends(get_session), authorization: str | None = Header(default=None)
):
    settings = request.app.state.settings
    bearer = (
        authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    )
    if bearer:
        for token, value in settings.api_tokens.items():
            if compare_digest(bearer, token):
                return (
                    {"workspace_id": value, "role": "admin"}
                    if isinstance(value, str)
                    else {"workspace_id": value["workspace_id"], "role": value.get("role", "admin")}
                )
        try:
            from .auth import authenticate_session

            principal = authenticate_session(session, bearer)
            if principal:
                return principal
        except ImportError:
            pass
        raise d.DomainError("Invalid bearer token", 401)
    if settings.demo_mode:
        return {"workspace_id": settings.demo_workspace_id, "role": "admin", "demo": True}
    raise d.DomainError("Bearer authentication required", 401)


def require_workspace(principal=Depends(get_principal)):
    return principal["workspace_id"]


def require_role(minimum="reader"):
    def check(principal=Depends(get_principal)):
        roles = {"reader": 0, "researcher": 1, "editor": 2, "admin": 3}
        if roles.get(principal.get("role"), -1) < roles[minimum]:
            raise d.DomainError(f"{minimum.capitalize()} role required", 403)
        return principal["workspace_id"]

    return check


def commit(session):
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise d.DomainError("Resource conflict; reload and retry", 409) from exc


def schedule(request, background, operation):
    if request.app.state.settings.inline_worker and operation.status in ("pending", "running"):
        background.add_task(
            d.execute_operation,
            operation.id,
            request.app.state.session_factory,
            settings_override=request.app.state.settings,
        )


def rows(session, model, workspace_id, project_id=None):
    q = select(model).where(model.workspace_id == workspace_id)
    if project_id is not None:
        q = q.where(model.project_id == project_id)
    return list(session.scalars(q.order_by(model.created_at.desc())))


def create_app(settings_override=None, session_factory=None):
    settings = settings_override or default_settings
    factory = session_factory or db.SessionLocal

    @asynccontextmanager
    async def lifespan(app):
        # Production uses Alembic; explicit demo/test convenience only.
        if settings.demo_mode:
            with factory() as session:
                db.init_db(session.bind)
        yield

    app = FastAPI(
        title="EvidenceHarbor API",
        version="1.0.0",
        lifespan=lifespan,
        description="Scoped immutable research provenance, evidence-backed proposals, and compare-and-swap report publication. Development demo mode must be enabled explicitly.",
    )
    app.state.settings, app.state.session_factory = settings, factory
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.exception_handler(d.DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {},
        )

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/health")
    @app.get("/v1/health")
    def health():
        return {"status": "ok", "service": "EvidenceHarbor", "version": "1.0.0"}

    @app.get("/v1/me")
    def me(principal=Depends(get_principal)):
        return principal

    @app.get("/v1/diagnostics")
    def diagnostics(session=Depends(get_session), workspace=Depends(require_workspace)):
        return {
            "database": session.bind.dialect.name,
            "worker": "local" if settings.inline_worker else "temporal",
            "demo_mode": settings.demo_mode,
            "search": "postgresql-fts" if session.bind.dialect.name == "postgresql" else "sqlite-lexical",
            "vector_search": "optional, explicit setup required",
            "pending_operations": session.scalar(
                select(func.count())
                .select_from(m.Operation)
                .where(m.Operation.workspace_id == workspace, m.Operation.status == "pending")
            ),
            "providers": {
                "local": "available",
                "external": "operator configuration and explicit enablement required",
            },
        }

    @app.get("/v1/projects")
    def projects(session=Depends(get_session), workspace=Depends(require_workspace)):
        return [d.serialize(p) for p in rows(session, m.Project, workspace)]

    @app.post("/v1/projects", status_code=201)
    def create_project(
        payload: s.ProjectCreate, session=Depends(get_session), workspace=Depends(require_role("editor"))
    ):
        item = d.create_project(session, workspace, payload.name, payload.description)
        commit(session)
        return d.serialize(item)

    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        result = d.serialize(d.project(session, workspace, project_id))
        for name, model in [
            ("documents", m.Document),
            ("sources", m.Source),
            ("questions", m.Question),
            ("runs", m.ResearchRun),
            ("proposals", m.Proposal),
            ("operations", m.Operation),
            ("evidence", m.Evidence),
        ]:
            result[name] = [d.serialize(item) for item in rows(session, model, workspace, project_id)]
        return result

    @app.patch("/v1/projects/{project_id}")
    def update_project(
        project_id: str,
        payload: s.ProjectUpdate,
        session=Depends(get_session),
        workspace=Depends(require_role("editor")),
    ):
        item = d.project(session, workspace, project_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(item, key, value)
        commit(session)
        return d.serialize(item)

    @app.delete("/v1/projects/{project_id}")
    def archive_project(
        project_id: str, session=Depends(get_session), workspace=Depends(require_role("editor"))
    ):
        item = d.project(session, workspace, project_id)
        item.archived = True
        commit(session)
        return {"id": item.id, "archived": True}

    @app.post("/v1/ingestions", status_code=202)
    def ingest(
        payload: s.IngestionCreate,
        request: Request,
        background: BackgroundTasks,
        session=Depends(get_session),
        workspace=Depends(require_role("researcher")),
        idempotency_key: str | None = Header(default=None, max_length=255),
    ):
        data = payload.model_dump(exclude_none=True, exclude={"project_id"})
        op = d.create_operation(session, workspace, payload.project_id, data, idempotency_key=idempotency_key)
        commit(session)
        result = d.serialize(op)
        schedule(request, background, op)
        return result

    @app.post("/v1/ingestions/upload", status_code=202)
    async def upload(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        project_id: str = Form(...),
        title: str | None = Form(default=None),
        original_url: str | None = Form(default=None),
        classification: str = Form(default="internal"),
        content_scope: str = Form(default="unspecified"),
        session=Depends(get_session),
        workspace=Depends(require_role("researcher")),
        idempotency_key: str | None = Header(default=None, max_length=255),
    ):
        d.project(session, workspace, project_id)
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise d.DomainError("Upload exceeds configured size limit", 413)
        if not content:
            raise d.DomainError("Uploaded file is empty", 422)
        if classification not in {"public", "internal", "sensitive"}:
            raise d.DomainError("Invalid source classification", 422)
        if content_scope not in {"unspecified", "abstract", "fulltext"}:
            raise d.DomainError("Invalid content scope; metadata belongs in discovered-works", 422)
        path = d.write_blob(content, settings.blob_dir)
        data = {
            "type": "upload",
            "content_scope": content_scope,
            "classification": classification,
            "blob_path": path,
            "media_type": file.content_type or "application/octet-stream",
            "filename": Path(file.filename or "upload").name,
            "title": title or Path(file.filename or "Uploaded document").name,
        }
        if original_url:
            data["original_url"] = s.declared_original_url(original_url)
        op = d.create_operation(session, workspace, project_id, data, idempotency_key=idempotency_key)
        commit(session)
        result = d.serialize(op)
        # Do not disclose an implementation-local path in API payloads.
        result["input_json"] = {k: v for k, v in data.items() if k != "blob_path"}
        schedule(request, background, op)
        return result

    @app.get("/v1/operations")
    def operations(project_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        d.project(session, workspace, project_id)
        return [d.serialize(o) for o in rows(session, m.Operation, workspace, project_id)]

    @app.get("/v1/operations/{operation_id}")
    def get_operation(operation_id: str, request: Request, session=Depends(get_session), workspace=Depends(require_workspace)):
        result = d.serialize(d.scoped(session, m.Operation, operation_id, workspace))
        result["execution_backend"] = "inline" if request.app.state.settings.inline_worker else "temporal"
        result["status_scope"] = "local_operation" if request.app.state.settings.inline_worker else "activity_attempt"
        return result

    @app.get("/v1/operations/{operation_id}/execution")
    async def execution_status(operation_id: str, request: Request, session=Depends(get_session), workspace=Depends(require_workspace)):
        from .execution_status import describe_execution

        op = d.scoped(session, m.Operation, operation_id, workspace)
        dispatch = session.scalar(select(m.OperationDispatch).where(m.OperationDispatch.operation_id == op.id))
        return await describe_execution(op, dispatch, request.app.state.settings.inline_worker)

    @app.post("/v1/operations/{operation_id}/retry", status_code=202)
    async def retry_operation(
        operation_id: str,
        request: Request,
        background: BackgroundTasks,
        session=Depends(get_session),
        workspace=Depends(require_role("researcher")),
    ):
        op = d.scoped(session, m.Operation, operation_id, workspace)
        dispatch = session.scalar(select(m.OperationDispatch).where(m.OperationDispatch.operation_id == op.id))
        observed_generation = dispatch.generation
        if request.app.state.settings.inline_worker:
            if op.status != "failed":
                raise d.DomainError("Only failed operations may be retried", 409)
        else:
            from .execution_status import TERMINAL_FAILURES, describe_execution

            execution = await describe_execution(op, dispatch, False)
            if not execution["available"]:
                raise d.DomainError("Workflow state is unavailable; retry was not scheduled", 503)
            if execution.get("workflow_status") not in TERMINAL_FAILURES:
                raise d.DomainError("The durable workflow is not terminally failed; activity retries are still owned by Temporal", 409)
        # A concurrent explicit retry must not allocate another generation from stale state.
        op = session.scalar(select(m.Operation).where(m.Operation.id == op.id).with_for_update()
                            .execution_options(populate_existing=True))
        dispatch = session.scalar(select(m.OperationDispatch).where(m.OperationDispatch.operation_id == op.id)
                                  .with_for_update().execution_options(populate_existing=True))
        if dispatch.generation != observed_generation:
            raise d.DomainError("Another retry already changed this operation generation", 409)
        op.status, op.error, op.completed_at = "pending", None, None
        dispatch.status, dispatch.last_error = "pending", None
        dispatch.generation = (dispatch.generation or 0) + 1
        commit(session)
        result = d.serialize(op)
        schedule(request, background, op)
        return result

    @app.get("/v1/source-changes")
    def source_changes(project_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        from .continuous import SourceChange

        d.project(session, workspace, project_id)
        return {"items": [d.serialize(item) for item in rows(session, SourceChange, workspace, project_id)]}

    @app.get("/v1/sources/{source_id}")
    def get_source(source_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        return d.serialize(d.scoped(session, m.Source, source_id, workspace))

    @app.get("/v1/sources/{source_id}/observations")
    def source_observations(
        source_id: str, session=Depends(get_session), workspace=Depends(require_workspace)
    ):
        d.scoped(session, m.Source, source_id, workspace)
        return [
            d.serialize(o)
            for o in session.scalars(
                select(m.FetchObservation)
                .where(
                    m.FetchObservation.workspace_id == workspace, m.FetchObservation.source_id == source_id
                )
                .order_by(m.FetchObservation.created_at.desc())
            )
        ]

    @app.get("/v1/sources/{source_id}/captures")
    def source_captures(source_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        d.scoped(session, m.Source, source_id, workspace)
        return [
            d.serialize(c)
            for c in session.scalars(
                select(m.Capture)
                .where(m.Capture.workspace_id == workspace, m.Capture.source_id == source_id)
                .order_by(m.Capture.created_at.desc())
            )
        ]

    @app.get("/v1/captures/{capture_id}")
    def get_capture(capture_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        cap = d.scoped(session, m.Capture, capture_id, workspace)
        result = d.serialize(cap)
        result["representations"] = [
            d.serialize(r)
            for r in session.scalars(
                select(m.Representation).where(
                    m.Representation.capture_id == cap.id, m.Representation.workspace_id == workspace
                )
            )
        ]
        result["observations"] = [
            d.serialize(r)
            for r in session.scalars(
                select(m.FetchObservation).where(
                    m.FetchObservation.capture_id == cap.id, m.FetchObservation.workspace_id == workspace
                )
            )
        ]
        result["processing_operations"] = [
            {"id": operation.id, "kind": operation.kind, "status": operation.status, "error": operation.error}
            for operation in session.scalars(select(m.Operation).where(
                m.Operation.workspace_id == workspace, m.Operation.project_id == cap.project_id,
                m.Operation.kind.in_(["ingest", "reprocess"])
            ))
            if (operation.result_json or {}).get("capture_id") == cap.id or operation.input_json.get("capture_id") == cap.id
        ]
        result["processing_ready"] = bool(result["representations"])
        return result

    @app.post("/v1/captures/{capture_id}/reprocess", status_code=202)
    def reprocess(
        capture_id: str,
        request: Request,
        background: BackgroundTasks,
        payload: s.ReprocessRequest | None = None,
        session=Depends(get_session),
        workspace=Depends(require_role("researcher")),
        idempotency_key: str | None = Header(default=None),
    ):
        cap = d.scoped(session, m.Capture, capture_id, workspace)
        data = {"capture_id": cap.id, **(payload.model_dump(exclude_none=True) if payload else {})}
        if data.get("pipeline_id"):
            from .extensions import PipelineRevision

            revision = d.scoped(session, PipelineRevision, data["pipeline_id"], workspace, cap.project_id)
            data["pipeline_config"] = revision.config_json
        op = d.create_operation(
            session, workspace, cap.project_id, data, kind="reprocess", idempotency_key=idempotency_key
        )
        commit(session)
        result = d.serialize(op)
        schedule(request, background, op)
        return result

    @app.get("/v1/documents/{document_id}")
    def get_document(document_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        return d.serialize(d.scoped(session, m.Document, document_id, workspace))

    @app.patch("/v1/documents/{document_id}/locks")
    def lock_sections(
        document_id: str,
        payload: s.DocumentLocks,
        session=Depends(get_session),
        workspace=Depends(require_role("editor")),
    ):
        doc = d.scoped(session, m.Document, document_id, workspace)
        if doc.kind != "report":
            raise d.DomainError("Section locks apply to reports", 422)
        sections = d.markdown_sections(doc.content)
        if any(name not in sections for name in payload.sections):
            raise d.DomainError("Every locked section must name an existing Markdown heading", 422)
        doc.locked_sections = list(dict.fromkeys(payload.sections))
        commit(session)
        return d.serialize(doc)

    @app.get("/v1/documents/{document_id}/versions")
    def versions(document_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        d.scoped(session, m.Document, document_id, workspace)
        return [
            d.serialize(v)
            for v in session.scalars(
                select(m.DocumentVersion)
                .where(
                    m.DocumentVersion.workspace_id == workspace, m.DocumentVersion.document_id == document_id
                )
                .order_by(m.DocumentVersion.version.desc())
            )
        ]

    @app.get("/v1/documents/{document_id}/content")
    def content(
        document_id: str,
        version: int | None = Query(default=None, ge=1),
        start: int | None = Query(default=None, ge=0, le=4_000_000),
        limit: int | None = Query(default=None, ge=1, le=32_000),
        block_id: str | None = None,
        session=Depends(get_session),
        workspace=Depends(require_workspace),
    ):
        doc = d.scoped(session, m.Document, document_id, workspace)
        result = d.serialize(doc)
        rep_id = doc.representation_id
        revision = session.scalar(
            select(m.DocumentVersion).where(
                m.DocumentVersion.document_id == doc.id,
                m.DocumentVersion.workspace_id == workspace,
                m.DocumentVersion.version == (doc.version if version is None else version),
            )
        )
        if version is not None and revision is None:
            raise d.DomainError("Document version not found", 404)
        if revision is not None:
            result.update(
                content=revision.content,
                title=revision.title,
                version=revision.version,
                revision_id=revision.id,
                evidence_ids=revision.evidence_ids,
            )
            rep_id = revision.representation_id
        result["representation_id"] = rep_id
        result["blocks"] = (
            [
                d.serialize(b)
                for b in session.scalars(
                    select(m.Block)
                    .where(m.Block.representation_id == rep_id, m.Block.workspace_id == workspace)
                    .order_by(m.Block.ordinal)
                )
            ]
            if rep_id
            else []
        )
        # Historical source versions resolve their own immutable representation/capture.
        capture_id = doc.capture_id
        if rep_id:
            representation = d.scoped(session, m.Representation, rep_id, workspace, doc.project_id)
            capture_id = representation.capture_id
        result["capture_id"] = capture_id
        result["capture"] = (
            d.serialize(d.scoped(session, m.Capture, capture_id, workspace)) if capture_id else None
        )
        if rep_id and capture_id:
            from .scholarly import WorkReading
            from .scholarly import content_scope as scholarly_scope

            cap = d.scoped(session, m.Capture, capture_id, workspace)
            result["content_scope"] = scholarly_scope(session, representation, cap)
            result["fulltext_ready"] = result["content_scope"] == "fulltext" and session.scalar(
                select(WorkReading.id).where(WorkReading.representation_id == rep_id,
                                             WorkReading.workspace_id == workspace,
                                             WorkReading.content_scope == "fulltext").limit(1)
            ) is not None
            result["fulltext_read"] = False
        if start is not None or limit is not None or block_id is not None:
            result = d.document_read_range(result, start=start, limit=limit or 8000, block_id=block_id)
        return result

    @app.get("/v1/representations/{representation_id}")
    def representation(
        representation_id: str, session=Depends(get_session), workspace=Depends(require_workspace)
    ):
        rep = d.scoped(session, m.Representation, representation_id, workspace)
        result = d.serialize(rep)
        result["blocks"] = [
            d.serialize(b)
            for b in session.scalars(
                select(m.Block)
                .where(m.Block.representation_id == rep.id, m.Block.workspace_id == workspace)
                .order_by(m.Block.ordinal)
            )
        ]
        return result

    @app.post("/v1/search")
    def search(payload: s.SearchRequest, session=Depends(get_session), workspace=Depends(require_workspace)):
        return d.search(session, workspace, payload.project_id, payload.query, payload.limit)

    @app.get("/v1/search")
    def search_get(
        project_id: str,
        query: str,
        limit: int = 20,
        session=Depends(get_session),
        workspace=Depends(require_workspace),
    ):
        if not query or len(query) > 1000 or not 1 <= limit <= 100:
            raise d.DomainError("Invalid query or limit", 422)
        return d.search(session, workspace, project_id, query, limit)

    @app.post("/v1/evidence", status_code=201)
    def evidence(
        payload: s.EvidenceCreate, session=Depends(get_session), workspace=Depends(require_role("researcher"))
    ):
        item = d.create_evidence(session, workspace, **payload.model_dump())
        commit(session)
        return d.serialize(item)

    @app.get("/v1/evidence/{evidence_id}")
    def get_evidence(evidence_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        ev = d.scoped(session, m.Evidence, evidence_id, workspace)
        block = d.scoped(session, m.Block, ev.block_id, workspace, ev.project_id)
        doc = session.scalar(
            select(m.Document).where(
                m.Document.workspace_id == workspace, m.Document.representation_id == block.representation_id
            )
        )
        result = d.serialize(ev)
        result.update(
            document_id=doc.id if doc else None,
            representation_id=block.representation_id,
            block=d.serialize(block),
        )
        return result

    @app.post("/v1/questions", status_code=201)
    def question(
        payload: s.QuestionCreate, session=Depends(get_session), workspace=Depends(require_role("researcher"))
    ):
        d.project(session, workspace, payload.project_id)
        item = m.Question(workspace_id=workspace, **payload.model_dump())
        session.add(item)
        commit(session)
        return d.serialize(item)

    @app.patch("/v1/questions/{question_id}")
    def update_question(
        question_id: str,
        payload: s.QuestionUpdate,
        session=Depends(get_session),
        workspace=Depends(require_role("researcher")),
    ):
        item = d.scoped(session, m.Question, question_id, workspace)
        if payload.evidence_ids is not None:
            d.validate_evidence(session, workspace, item.project_id, payload.evidence_ids)
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(item, key, value)
        if item.status == "answered" and (not item.answer or not item.evidence_ids):
            raise d.DomainError("Answered questions require answer text and evidence", 422)
        commit(session)
        return d.serialize(item)

    @app.post("/v1/research-runs", status_code=202)
    def research(
        payload: s.ResearchRunCreate,
        request: Request,
        background: BackgroundTasks,
        session=Depends(get_session),
        workspace=Depends(require_role("researcher")),
    ):
        run, op = d.create_research_run(session, workspace, **payload.model_dump())
        commit(session)
        result = d.serialize(run)
        result["operation_id"] = op.id
        schedule(request, background, op)
        return result

    @app.get("/v1/research-runs/{run_id}")
    def get_run(run_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        run = d.scoped(session, m.ResearchRun, run_id, workspace)
        result = d.serialize(run)
        result["events"] = [
            d.serialize(e)
            for e in session.scalars(
                select(m.RunEvent)
                .where(m.RunEvent.run_id == run.id, m.RunEvent.workspace_id == workspace)
                .order_by(m.RunEvent.sequence)
            )
        ]
        return result

    @app.get("/v1/research-runs/{run_id}/events")
    def run_events(
        run_id: str, after: int = 0, session=Depends(get_session), workspace=Depends(require_workspace)
    ):
        d.scoped(session, m.ResearchRun, run_id, workspace)
        return [
            d.serialize(e)
            for e in session.scalars(
                select(m.RunEvent)
                .where(
                    m.RunEvent.run_id == run_id,
                    m.RunEvent.workspace_id == workspace,
                    m.RunEvent.sequence > after,
                )
                .order_by(m.RunEvent.sequence)
            )
        ]

    @app.post("/v1/proposals", status_code=201)
    def proposal(
        payload: s.ProposalCreate, session=Depends(get_session), workspace=Depends(require_role("researcher"))
    ):
        item = d.create_proposal(session, workspace, **payload.model_dump())
        commit(session)
        return d.serialize(item)

    @app.get("/v1/proposals/{proposal_id}")
    def get_proposal(proposal_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        item = d.scoped(session, m.Proposal, proposal_id, workspace)
        result = d.serialize(item)
        ids = list(dict.fromkeys(eid for claim in item.claims for eid in d.claim_evidence_ids(claim)))
        result["evidence"] = [
            d.serialize(e) for e in d.validate_evidence(session, workspace, item.project_id, ids)
        ]
        result["claim_records"] = [
            d.serialize(c)
            for c in session.scalars(
                select(m.ClaimRecord)
                .where(m.ClaimRecord.proposal_id == item.id, m.ClaimRecord.workspace_id == workspace)
                .order_by(m.ClaimRecord.ordinal)
            )
        ]
        return result

    @app.post("/v1/proposals/{proposal_id}/publish")
    def publish(
        proposal_id: str,
        payload: s.ProposalPublish,
        session=Depends(get_session),
        workspace=Depends(require_role("editor")),
    ):
        item = d.publish_proposal(
            session, workspace, proposal_id, payload.expected_version, payload.accepted_claim_indices
        )
        commit(session)
        return d.serialize(item)

    @app.post("/v1/proposals/{proposal_id}/reject")
    def reject(proposal_id: str, session=Depends(get_session), workspace=Depends(require_role("editor"))):
        item = d.scoped(session, m.Proposal, proposal_id, workspace)
        if item.status != "pending":
            raise d.DomainError("Only pending proposals can be rejected", 409)
        item.status = "rejected"
        for claim in session.scalars(
            select(m.ClaimRecord).where(
                m.ClaimRecord.proposal_id == item.id, m.ClaimRecord.workspace_id == workspace
            )
        ):
            claim.review_status, claim.last_reviewed_at = "rejected", m.utcnow()
        commit(session)
        return d.serialize(item)

    @app.get("/v1/projects/{project_id}/export")
    def export(project_id: str, session=Depends(get_session), workspace=Depends(require_workspace)):
        raw = d.export_project(session, workspace, project_id)
        return Response(
            content=raw,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="evidenceharbor-{project_id}.zip"'},
        )

    @app.post("/v1/subscriptions", status_code=201)
    def subscription(
        payload: s.SubscriptionCreate, session=Depends(get_session), workspace=Depends(require_role("admin"))
    ):
        d.project(session, workspace, payload.project_id)
        if payload.target_url:
            from .security import validate_url

            validate_url(payload.target_url)
        item = m.Subscription(workspace_id=workspace, **payload.model_dump())
        session.add(item)
        commit(session)
        return d.serialize(item)

    @app.get("/v1/subscriptions")
    def subscriptions(
        project_id: str, session=Depends(get_session), workspace=Depends(require_role("admin"))
    ):
        d.project(session, workspace, project_id)
        return [d.serialize(r) for r in rows(session, m.Subscription, workspace, project_id)]

    @app.delete("/v1/subscriptions/{subscription_id}")
    def deactivate_subscription(
        subscription_id: str, session=Depends(get_session), workspace=Depends(require_role("admin"))
    ):
        sub = d.scoped(session, m.Subscription, subscription_id, workspace)
        sub.active = False
        commit(session)
        return d.serialize(sub)

    @app.get("/v1/outbox")
    def outbox(project_id: str, session=Depends(get_session), workspace=Depends(require_role("admin"))):
        d.project(session, workspace, project_id)
        return [d.serialize(r) for r in rows(session, m.OutboxEvent, workspace, project_id)]

    # Optional operator modules register routes only after this module's dependencies exist.
    try:
        from .auth import router as auth_router

        app.include_router(auth_router)
    except ImportError:
        pass
    try:
        from .scheduling import create_router

        app.include_router(create_router(require_workspace, require_role))
    except ImportError:
        pass
    try:
        from .extensions import create_router as extensions_router

        app.include_router(extensions_router(require_workspace, require_role))
    except ImportError:
        pass
    from .scholarly import create_router as scholarly_router

    app.include_router(scholarly_router(require_workspace, require_role, get_principal))
    return app


app = create_app()
