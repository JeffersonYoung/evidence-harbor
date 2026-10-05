"""Relational resource model. Captured source content is append-only."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id() -> str:
    return str(uuid4())


def utcnow():
    return datetime.now(UTC)


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Scoped:
    workspace_id: Mapped[str] = mapped_column(String(120), index=True)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="Workspace")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Identified, Scoped, Base):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Source(Identified, Scoped, Base):
    __tablename__ = "sources"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    canonical_uri: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(20), default="internal")
    access_scope: Mapped[str] = mapped_column(String(120), default="workspace")
    representation_variant: Mapped[str] = mapped_column(String(120), default="default")
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "project_id",
            "canonical_uri",
            "access_scope",
            "representation_variant",
            name="uq_source_identity",
        ),
    )


class Capture(Identified, Scoped, Base):
    __tablename__ = "captures"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    previous_capture_id: Mapped[str | None] = mapped_column(ForeignKey("captures.id"), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(150))
    blob_path: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Representation(Identified, Scoped, Base):
    __tablename__ = "representations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"), index=True)
    format: Mapped[str] = mapped_column(String(40), default="text")
    parser: Mapped[str] = mapped_column(String(120), default="plain-text-v1")
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Block(Identified, Scoped, Base):
    __tablename__ = "blocks"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    representation_id: Mapped[str] = mapped_column(ForeignKey("representations.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    locator_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("representation_id", "ordinal", name="uq_block_ordinal"),)


class IndexGeneration(Identified, Scoped, Base):
    __tablename__ = "index_generations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    representation_id: Mapped[str] = mapped_column(ForeignKey("representations.id"), index=True)
    index_kind: Mapped[str] = mapped_column(String(30), default="lexical")
    version: Mapped[str] = mapped_column(String(60), default="lexical-v1")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Document(Identified, Scoped, Base):
    __tablename__ = "documents"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="source")
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    capture_id: Mapped[str | None] = mapped_column(ForeignKey("captures.id"), nullable=True)
    representation_id: Mapped[str | None] = mapped_column(ForeignKey("representations.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Operation(Identified, Scoped, Base):
    __tablename__ = "operations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="ingest")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_operation_idempotency"),)


class Evidence(Identified, Scoped, Base):
    __tablename__ = "evidence"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    block_id: Mapped[str] = mapped_column(ForeignKey("blocks.id"), index=True)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"))
    quote: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    locator_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Question(Identified, Scoped, Base):
    __tablename__ = "questions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    answer: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)


class ResearchRun(Identified, Scoped, Base):
    __tablename__ = "research_runs"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    provider: Mapped[str] = mapped_column(String(60), default="local")
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Identified, Scoped, Base):
    __tablename__ = "run_events"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),)


class Proposal(Identified, Scoped, Base):
    __tablename__ = "proposals"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    target_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    claims: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    published_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Subscription(Identified, Scoped, Base):
    __tablename__ = "subscriptions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    event_types: Mapped[list] = mapped_column(JSON, default=lambda: ["*"])
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OutboxEvent(Identified, Scoped, Base):
    __tablename__ = "outbox_events"
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImmutableResourceError(ValueError):
    pass


def reject_mutation(mapper, connection, target):
    raise ImmutableResourceError(f"{type(target).__name__} is immutable; create a new version")


for immutable in (Source, Capture, Representation, Block, IndexGeneration, Evidence):
    event.listen(immutable, "before_update", reject_mutation)
    event.listen(immutable, "before_delete", reject_mutation)


class DocumentVersion(Identified, Scoped, Base):
    __tablename__ = "document_versions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    representation_id: Mapped[str | None] = mapped_column(ForeignKey("representations.id"), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposals.id"), nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_version"),)


class OperationDispatch(Identified, Scoped, Base):
    __tablename__ = "operation_dispatches"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


for immutable in (DocumentVersion, RunEvent):
    event.listen(immutable, "before_update", reject_mutation)
    event.listen(immutable, "before_delete", reject_mutation)

# PostgreSQL's lexical index uses the exact same tokenized content as queries.
from sqlalchemy import DDL

Block.search_text = mapped_column(Text, default="")
event.listen(
    Block.__table__,
    "after_create",
    DDL("CREATE INDEX ix_blocks_fts ON blocks USING gin (to_tsvector('simple', search_text))").execute_if(
        dialect="postgresql"
    ),
)


class FetchObservation(Identified, Scoped, Base):
    __tablename__ = "fetch_observations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    capture_id: Mapped[str | None] = mapped_column(ForeignKey("captures.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


event.listen(FetchObservation, "before_update", reject_mutation)
event.listen(FetchObservation, "before_delete", reject_mutation)


class ClaimRecord(Identified, Scoped, Base):
    __tablename__ = "claims"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="")
    as_of: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="unreviewed")
    review_status: Mapped[str] = mapped_column(String(30), default="pending")
    limitations: Mapped[list] = mapped_column(JSON, default=list)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("proposal_id", "ordinal", name="uq_proposal_claim_ordinal"),)


class ClaimEvidence(Identified, Scoped, Base):
    __tablename__ = "claim_evidence"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), index=True)
    relation: Mapped[str] = mapped_column(String(30), default="supporting")
    rationale: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        UniqueConstraint("claim_id", "evidence_id", "relation", name="uq_claim_evidence_relation"),
    )


Question.priority = mapped_column(String(20), default="normal")
Question.evidence_gaps = mapped_column(JSON, default=list)
Question.recheck_condition = mapped_column(Text, default="")
ResearchRun.config_json = mapped_column(JSON, default=dict)
ResearchRun.usage_json = mapped_column(JSON, default=dict)
Document.locked_sections = mapped_column(JSON, default=list)
Proposal.accepted_claim_indices = mapped_column(JSON, nullable=True)
DocumentVersion.claim_ids = mapped_column(JSON, default=list)

# Defense in depth: database-level immutability also rejects bulk SQL writes.
# These triggers do not interfere with dropping tables during migrations.
IMMUTABLE_TABLES = (
    Source,
    Capture,
    Representation,
    Block,
    IndexGeneration,
    Evidence,
    DocumentVersion,
    RunEvent,
    FetchObservation,
    ClaimEvidence,
)
for immutable in IMMUTABLE_TABLES:
    table_name = immutable.__tablename__
    for action in ("UPDATE", "DELETE"):
        trigger_name = f"no_{action.lower()}_{table_name}"
        sqlite_ddl = f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE {action} ON {table_name} BEGIN SELECT RAISE(ABORT, '{table_name} is immutable'); END"
        event.listen(immutable.__table__, "after_create", DDL(sqlite_ddl).execute_if(dialect="sqlite"))
        postgres_ddl = f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table_name} FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation()"
        event.listen(immutable.__table__, "after_create", DDL(postgres_ddl).execute_if(dialect="postgresql"))
event.listen(
    Base.metadata,
    "before_create",
    DDL(
        """CREATE OR REPLACE FUNCTION reject_immutable_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'immutable resource: %%', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql"""
    ).execute_if(dialect="postgresql"),
)


class ExternalCallReservation(Identified, Scoped, Base):
    __tablename__ = "external_call_reservations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(60))
    reserved_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
