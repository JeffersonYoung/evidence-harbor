"""Durable scholarly discovery is not acquisition or proof that a paper was read."""

import re
import unicodedata
from typing import Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, Query
from sqlalchemy import DDL, JSON, ForeignKey, String, Text, UniqueConstraint, event, func, select
from sqlalchemy.orm import Mapped, load_only, mapped_column

from . import domain as d
from . import models as m
from .db import Base, get_session
from .schemas import BatchInput, MetadataReviewInput, ReadingInput, WorkInput


class DiscoveredWork(m.Identified, m.Scoped, Base):
    __tablename__ = "discovered_works"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    canonical_key: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list] = mapped_column(JSON, default=list)
    year: Mapped[int | None] = mapped_column(nullable=True)
    abstract: Mapped[str] = mapped_column(Text, default="")
    access_status: Mapped[str] = mapped_column(String(30), default="unknown")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("workspace_id", "project_id", "canonical_key"),)


class WorkAlias(m.Identified, m.Scoped, Base):
    __tablename__ = "scholarly_work_aliases"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("discovered_works.id"), index=True)
    key: Mapped[str] = mapped_column(String(300))
    __table_args__ = (UniqueConstraint("workspace_id", "project_id", "key"),)


class WorkObservation(m.Identified, m.Scoped, Base):
    __tablename__ = "scholarly_work_observations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("discovered_works.id"), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("work_id", "payload_hash"),)


class WorkReading(m.Identified, m.Scoped, Base):
    __tablename__ = "scholarly_work_readings"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("discovered_works.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    representation_id: Mapped[str] = mapped_column(ForeignKey("representations.id"), index=True)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"))
    content_scope: Mapped[str] = mapped_column(String(30))
    review_note: Mapped[str] = mapped_column(Text)
    provenance_json: Mapped[dict] = mapped_column(JSON, default=dict)
    review_method: Mapped[str] = mapped_column(String(50), default="operator_attested_saved_content")
    __table_args__ = (UniqueConstraint("work_id", "representation_id"),)


class WorkMetadataReview(m.Identified, m.Scoped, Base):
    __tablename__ = "scholarly_metadata_reviews"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("discovered_works.id"), index=True)
    revision: Mapped[int]
    overrides_json: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    actor_json: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("work_id", "revision"),)


for model in (WorkAlias, WorkObservation, WorkReading, WorkMetadataReview):
    for action in ("before_update", "before_delete"):
        event.listen(model, action, m.reject_mutation)
    for action in ("UPDATE", "DELETE"):
        table = model.__tablename__
        name = f"no_{action.lower()}_{table}"
        event.listen(
            model.__table__,
            "after_create",
            DDL(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            model.__table__,
            "after_create",
            DDL(
                f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation()"
            ).execute_if(dialect="postgresql"),
        )


def normalized_words(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def identity_keys(item):
    keys = []
    if item.doi:
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", item.doi.strip(), flags=re.IGNORECASE)
        doi = unquote(doi).casefold()
        if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", doi):
            raise d.DomainError("Invalid DOI", 422)
        keys.append("doi:" + doi)
    if item.arxiv_id:
        arxiv = re.sub(
            r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", item.arxiv_id.strip(), flags=re.IGNORECASE
        )
        arxiv = re.sub(r"\.pdf$", "", arxiv, flags=re.IGNORECASE)
        arxiv = re.sub(r"v\d+$", "", arxiv, flags=re.IGNORECASE).casefold()
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})", arxiv):
            raise d.DomainError("Invalid arXiv identifier", 422)
        keys.append("arxiv:" + arxiv)
    title = normalized_words(item.title)
    if not title:
        raise d.DomainError("Scholarly title cannot be blank", 422)
    if item.authors:
        keys.append("titleauthor:" + d.digest(title + "|" + normalized_words(item.authors[0])))
    if not keys:
        raise d.DomainError("A DOI, arXiv ID or title plus author is required for scholarly identity", 422)
    if any(len(key) > 300 for key in keys):
        raise d.DomainError("Scholarly identifier is too long", 422)
    return keys


def observation_payload_hash(payload):
    import json

    return d.digest(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def intake(session, workspace, project_id, item):
    keys = identity_keys(item)
    aliases = list(
        session.scalars(
            select(WorkAlias).where(
                WorkAlias.workspace_id == workspace,
                WorkAlias.project_id == project_id,
                WorkAlias.key.in_(keys),
            )
        )
    )
    strong = {alias.work_id for alias in aliases if not alias.key.startswith("titleauthor:")}
    candidates = strong or {alias.work_id for alias in aliases}
    if len(candidates) > 1:
        raise d.DomainError("Scholarly identity collision; review aliases before merging", 409)
    existing = session.get(DiscoveredWork, next(iter(candidates))) if candidates else None
    if existing:
        prior = set(session.scalars(select(WorkAlias.key).where(WorkAlias.work_id == existing.id)))
        for prefix in ("doi:", "arxiv:"):
            old = {key for key in prior if key.startswith(prefix)}
            new = {key for key in keys if key.startswith(prefix)}
            if old and new and old != new:
                raise d.DomainError("Conflicting scholarly identifiers cannot be merged by title", 409)
        if any(alias.work_id != existing.id for alias in aliases):
            raise d.DomainError("Scholarly alias belongs to another work", 409)
    created = existing is None
    work = existing or DiscoveredWork(
        workspace_id=workspace,
        project_id=project_id,
        canonical_key=keys[0],
        title=item.title,
        authors=item.authors,
        year=item.year,
        abstract=item.abstract,
        source_url=item.source_url,
        access_status=item.access_status,
    )
    if created:
        session.add(work)
        session.flush()
    else:
        # First display identity is stable; exact title variants/provenance are retained below.
        if not work.abstract and item.abstract:
            work.abstract = item.abstract
        if work.access_status == "unknown" and item.access_status != "unknown":
            work.access_status = item.access_status
    for key in keys:
        if not any(alias.key == key for alias in aliases):
            session.add(WorkAlias(workspace_id=workspace, project_id=project_id, work_id=work.id, key=key))
    payload = item.model_dump()
    fingerprint = observation_payload_hash(payload)
    if (
        session.scalar(
            select(WorkObservation.id).where(
                WorkObservation.work_id == work.id, WorkObservation.payload_hash == fingerprint
            )
        )
        is None
    ):
        session.add(
            WorkObservation(
                workspace_id=workspace,
                project_id=project_id,
                work_id=work.id,
                payload_hash=fingerprint,
                payload=payload,
            )
        )
    session.flush()
    return work, created


def verify_reading_provenance(session, work, source, capture):
    """Bind actual saved bytes to a recorded scholarly source, not a caller boolean."""
    locators = {work.source_url} if work.source_url else set()
    for observation in session.scalars(select(WorkObservation).where(WorkObservation.work_id == work.id)):
        payload = observation.payload
        if payload.get("source_url"):
            locators.add(payload["source_url"])
        recorded = payload.get("metadata", {}).get("source_locators", [])
        if isinstance(recorded, list):
            locators.update(item for item in recorded if isinstance(item, str))
    aliases = set(session.scalars(select(WorkAlias.key).where(WorkAlias.work_id == work.id)))
    saved_locators = {source.canonical_uri}
    for key in ("declared_original_url", "requested_url", "final_url"):
        if isinstance(capture.metadata_json.get(key), str):
            saved_locators.add(capture.metadata_json[key])
    for locator in saved_locators:
        if locator in locators:
            return {
                "matched_locator": locator,
                "binding": "recorded_source_locator",
                "capture_sha256": capture.content_hash,
            }
        parsed = urlsplit(locator)
        alias = None
        if parsed.hostname and parsed.hostname.casefold() in {"doi.org", "dx.doi.org"}:
            alias = "doi:" + unquote(parsed.path.lstrip("/")).casefold()
        elif parsed.hostname and parsed.hostname.casefold() in {"arxiv.org", "www.arxiv.org"}:
            identifier = re.sub(r"^/(?:pdf|abs)/", "", parsed.path)
            identifier = re.sub(r"\.pdf$", "", identifier)
            identifier = re.sub(r"v\d+$", "", identifier)
            alias = "arxiv:" + identifier.casefold()
        if alias and alias in aliases:
            return {"matched_locator": locator, "binding": alias, "capture_sha256": capture.content_hash}
    raise d.DomainError("Saved source provenance does not match this discovered work", 422)


def content_scope(session, representation, capture):
    declared = capture.metadata_json.get("content_scope", "unspecified")
    if declared != "unspecified":
        return declared
    linked = set(
        session.scalars(
            select(WorkReading.content_scope).where(
                WorkReading.representation_id == representation.id,
                WorkReading.workspace_id == representation.workspace_id,
            )
        )
    )
    return "abstract" if "abstract" in linked else "fulltext" if "fulltext" in linked else "unspecified"


def work_record(session, work, detail=False):
    reading_query = select(WorkReading).where(WorkReading.work_id == work.id)
    if not detail:
        reading_query = reading_query.options(load_only(WorkReading.id, WorkReading.content_scope))
    readings = list(session.scalars(reading_query))
    full = any(row.content_scope == "fulltext" for row in readings)
    value = {
        **d.serialize(work),
        "content_scope": "fulltext" if full else "abstract" if work.abstract or readings else "metadata_only",
        "evidence_eligible": bool(readings),
        "fulltext_ready": full,
        "fulltext_read": False,
        "eligibility_basis": "operator-attested saved capture/representation"
        if readings
        else "discovery metadata is not evidence",
        "reading_ids": [row.id for row in readings],
    }
    review_query = select(WorkMetadataReview).where(WorkMetadataReview.work_id == work.id)
    review_query = (
        review_query.order_by(WorkMetadataReview.revision)
        if detail
        else review_query.order_by(WorkMetadataReview.revision.desc()).limit(1)
    )
    reviews = list(session.scalars(review_query))
    value["review_revision"] = reviews[-1].revision if reviews else 0
    value["review_status"] = "reviewed_metadata" if reviews else "unreviewed_provider_metadata"
    if reviews:
        value["provider_display"] = {
            "title": work.title,
            "authors": work.authors,
            "year": work.year,
            "abstract": work.abstract,
        }
        value.update(reviews[-1].overrides_json)
    # Display corrections never create a parsed representation, evidence or a fulltext reading.
    value["content_scope"] = (
        "fulltext" if full else "abstract" if value["abstract"] or readings else "metadata_only"
    )
    abstract_revision = value.get("abstract_review_revision", 0)
    value["abstract_review_revision"] = abstract_revision
    value["abstract_reviewed"] = bool(abstract_revision)
    value["abstract_display_scope"] = "abstract_metadata"
    value["abstract_evidence_eligible"] = False
    value["abstract_sha256"] = d.digest(value["abstract"])
    value["abstract_review"] = None
    if abstract_revision:
        abstract_review = session.scalar(
            select(WorkMetadataReview).where(
                WorkMetadataReview.work_id == work.id, WorkMetadataReview.revision == abstract_revision
            )
        )
        if abstract_review is not None:
            value["abstract_review"] = {
                "id": abstract_review.id,
                "revision": abstract_review.revision,
                "reason": abstract_review.reason,
                "source_url": abstract_review.source_url,
                "evidence_ids": abstract_review.evidence_ids,
            }
    if detail:
        value["metadata_reviews"] = [d.serialize(row) for row in reviews]
        value["aliases"] = [
            row.key for row in session.scalars(select(WorkAlias).where(WorkAlias.work_id == work.id))
        ]
        value["observations"] = [
            d.serialize(row)
            for row in session.scalars(select(WorkObservation).where(WorkObservation.work_id == work.id))
        ]
        value["readings"] = [d.serialize(row) for row in readings]
    return value


def agent_work_summary(session, work, record=None):
    """Bounded projection only; eligibility and reviewed metadata stay in the common domain."""
    record = work_record(session, work) if record is None else record
    fields = (
        "id",
        "project_id",
        "canonical_key",
        "year",
        "access_status",
        "content_scope",
        "evidence_eligible",
        "fulltext_ready",
        "fulltext_read",
        "review_revision",
        "review_status",
        "abstract_display_scope",
        "abstract_evidence_eligible",
        "abstract_reviewed",
        "abstract_review_revision",
        "abstract_sha256",
    )
    result = {key: record[key] for key in fields}
    result["agent_projection_version"] = 1
    result.update(
        title=record["title"][:500],
        title_length=len(record["title"]),
        authors=[name[:200] for name in record["authors"][:20]],
        author_count=len(record["authors"]),
        authors_truncated=len(record["authors"]) > 20
        or any(len(name) > 200 for name in record["authors"][:20]),
        venue=str(record.get("venue", ""))[:500],
        source_url_preview=(record.get("source_url") or "")[:1024],
        abstract_length=len(record["abstract"]),
        full_text_reading_certified=False,
    )
    review = record.get("abstract_review")
    result["abstract_review"] = (
        {
            "id": review["id"],
            "revision": review["revision"],
            "reason": review["reason"][:1000],
            "reason_length": len(review["reason"]),
            "source_url_preview": (review["source_url"] or "")[:1024],
        }
        if review
        else None
    )
    return result


def agent_work_detail(session, work, offset, limit, abstract_start, abstract_limit):
    record = work_record(session, work)
    abstract = record["abstract"]
    if abstract_start > len(abstract):
        raise d.DomainError("Abstract range is outside the saved abstract", 422)
    result = agent_work_summary(session, work, record)
    end = min(len(abstract), abstract_start + abstract_limit)
    result["abstract"] = abstract[abstract_start:end]
    result["abstract_window_sha256"] = d.digest(result["abstract"])
    result["abstract_range"] = {
        "start_offset": abstract_start,
        "end_offset": end,
        "total_length": len(abstract),
        "next_offset": end if end < len(abstract) else None,
        "offset_unit": "unicode_code_points",
    }
    result["pages"] = {}
    specs = [
        ("aliases", WorkAlias, [WorkAlias.id, WorkAlias.key]),
        (
            "observations",
            WorkObservation,
            [WorkObservation.id, WorkObservation.payload_hash, WorkObservation.created_at],
        ),
        (
            "readings",
            WorkReading,
            [
                WorkReading.id,
                WorkReading.document_id,
                WorkReading.representation_id,
                WorkReading.capture_id,
                WorkReading.content_scope,
                WorkReading.created_at,
            ],
        ),
        (
            "metadata_reviews",
            WorkMetadataReview,
            [WorkMetadataReview.id, WorkMetadataReview.revision, WorkMetadataReview.created_at],
        ),
    ]
    for name, model, columns in specs:
        conditions = (model.work_id == work.id, model.workspace_id == work.workspace_id)
        total = session.scalar(select(func.count()).select_from(model).where(*conditions))
        rows = session.execute(
            select(*columns)
            .where(*conditions)
            .order_by(model.created_at, model.id)
            .offset(offset)
            .limit(limit)
        ).mappings()
        result[name] = [dict(row) for row in rows]
        result["pages"][name] = {
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }
    result["record_reading_instruction"] = (
        "Use immutable record IDs with read_scholarly_record for provenance or review details. Abstracts and screening observations do not certify full-paper reading."
    )
    return result


def create_router(require_workspace, require_role, require_principal):
    router = APIRouter(prefix="/v1/discovered-works", tags=["scholarly discovery"])

    @router.get("/capabilities")
    def capabilities(workspace=Depends(require_workspace)):
        return {
            "protocol": "scholarly-mcp-v1",
            "agent_projection_version": 1,
            "metadata_intake_is_reading": False,
            "canonical_correction_role": "editor",
        }

    @router.post("/batch", status_code=201)
    def batch(
        payload: BatchInput,
        view: Literal["full", "agent"] = "full",
        workspace=Depends(require_role("researcher")),
        session=Depends(get_session),
    ):
        from sqlalchemy.exc import IntegrityError

        d.project(session, workspace, payload.project_id)
        rows, created = [], 0
        try:
            for item in payload.items:
                work, is_new = intake(session, workspace, payload.project_id, item)
                rows.append(work)
                created += is_new
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise d.DomainError(
                "Concurrent scholarly identity collision; retry the complete batch", 409
            ) from exc
        return {
            "items": [
                agent_work_summary(session, row) if view == "agent" else work_record(session, row)
                for row in rows
            ],
            "created": created,
            "deduplicated": len(rows) - created,
            "agent_projection_version": 1 if view == "agent" else None,
        }

    @router.get("")
    def listing(
        project_id: str,
        offset: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=100, ge=1, le=200),
        view: Literal["full", "agent"] = "full",
        workspace=Depends(require_workspace),
        session=Depends(get_session),
    ):
        d.project(session, workspace, project_id)
        query = select(DiscoveredWork).where(
            DiscoveredWork.workspace_id == workspace, DiscoveredWork.project_id == project_id
        )
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(
            query.order_by(DiscoveredWork.created_at, DiscoveredWork.id).offset(offset).limit(limit)
        )
        return {
            "items": [
                agent_work_summary(session, row) if view == "agent" else work_record(session, row)
                for row in rows
            ],
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "agent_projection_version": 1 if view == "agent" else None,
        }

    @router.get("/{work_id}")
    def detail(
        work_id: str,
        view: Literal["full", "agent"] = "full",
        offset: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=10, ge=1, le=50),
        abstract_start: int = Query(default=0, ge=0, le=100000),
        abstract_limit: int = Query(default=2000, ge=1, le=8000),
        workspace=Depends(require_workspace),
        session=Depends(get_session),
    ):
        work = d.scoped(session, DiscoveredWork, work_id, workspace)
        return (
            agent_work_detail(session, work, offset, limit, abstract_start, abstract_limit)
            if view == "agent"
            else work_record(session, work, detail=True)
        )

    @router.get("/{work_id}/records/{record_kind}/{record_id}")
    def record_window(
        work_id: str,
        record_kind: Literal["observation", "reading", "metadata_review"],
        record_id: str,
        start: int = Query(default=0, ge=0, le=2_000_000),
        limit: int = Query(default=2000, ge=1, le=16000),
        workspace=Depends(require_workspace),
        session=Depends(get_session),
    ):
        import json

        work = d.scoped(session, DiscoveredWork, work_id, workspace)
        model = {
            "observation": WorkObservation,
            "reading": WorkReading,
            "metadata_review": WorkMetadataReview,
        }[record_kind]
        record = d.scoped(session, model, record_id, workspace, work.project_id)
        if record.work_id != work.id:
            raise d.DomainError("Scholarly record not found", 404)
        text = json.dumps(d.serialize(record), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if start > len(text):
            raise d.DomainError("Record range is outside the saved record", 422)
        end = min(len(text), start + limit)
        return {
            "work_id": work.id,
            "record_kind": record_kind,
            "record_id": record.id,
            "record_sha256": d.digest(text),
            "text": text[start:end],
            "text_sha256": d.digest(text[start:end]),
            "reading_range": {
                "start_offset": start,
                "end_offset": end,
                "total_length": len(text),
                "next_offset": end if end < len(text) else None,
                "has_more": end < len(text),
                "offset_unit": "unicode_code_points",
            },
        }

    @router.post("/{work_id}/observations", status_code=201)
    def append_observation(
        work_id: str,
        payload: WorkInput,
        view: Literal["full", "agent"] = "full",
        workspace=Depends(require_role("researcher")),
        session=Depends(get_session),
    ):
        from sqlalchemy.exc import IntegrityError

        target = d.scoped(session, DiscoveredWork, work_id, workspace)
        try:
            found, created = intake(session, workspace, target.project_id, payload)
            if created or found.id != target.id:
                raise d.DomainError("Observation identity does not match the target discovered work", 409)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise d.DomainError("Concurrent scholarly identity conflict; retry the observation", 409) from exc
        result = (
            agent_work_summary(session, target)
            if view == "agent"
            else work_record(session, target, detail=True)
        )
        observation = session.scalar(
            select(WorkObservation).where(
                WorkObservation.work_id == target.id,
                WorkObservation.payload_hash == observation_payload_hash(payload.model_dump()),
            )
        )
        result["observation_id"] = observation.id
        result["observation_payload_hash"] = observation.payload_hash
        return result

    @router.post("/{work_id}/metadata-reviews", status_code=201)
    def review_metadata(
        work_id: str,
        payload: MetadataReviewInput,
        view: Literal["full", "agent"] = "full",
        workspace=Depends(require_role("editor")),
        principal=Depends(require_principal),
        session=Depends(get_session),
    ):
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError

        work = d.scoped(session, DiscoveredWork, work_id, workspace)
        if session.bind.dialect.name == "sqlite":
            session.execute(
                update(DiscoveredWork).where(DiscoveredWork.id == work.id).values(title=DiscoveredWork.title)
            )
        session.scalar(select(DiscoveredWork).where(DiscoveredWork.id == work.id).with_for_update())
        previous = session.scalar(
            select(WorkMetadataReview)
            .where(WorkMetadataReview.work_id == work.id)
            .order_by(WorkMetadataReview.revision.desc())
            .limit(1)
        )
        revision = previous.revision if previous else 0
        if revision != payload.expected_revision:
            raise d.DomainError("Metadata review revision changed; reload before correcting", 409)
        changes = payload.changes.model_dump(exclude_none=True)
        if not changes or any(isinstance(value, str) and not value.strip() for value in changes.values()):
            raise d.DomainError("A nonempty metadata correction is required", 422)
        if not payload.source_url and not payload.evidence_ids:
            raise d.DomainError("Metadata correction requires a source URL or verified evidence", 422)
        if payload.evidence_ids:
            d.validate_evidence(session, workspace, work.project_id, payload.evidence_ids)
        overrides = {**(previous.overrides_json if previous else {}), **changes}
        if "abstract" in changes:
            prior_abstract = (previous.overrides_json if previous else {}).get("abstract", work.abstract)
            if changes["abstract"] != prior_abstract or not overrides.get("abstract_review_revision"):
                overrides["abstract_review_revision"] = revision + 1
        record = WorkMetadataReview(
            workspace_id=workspace,
            project_id=work.project_id,
            work_id=work.id,
            revision=revision + 1,
            overrides_json=overrides,
            reason=payload.reason,
            source_url=payload.source_url,
            evidence_ids=payload.evidence_ids,
            actor_json={key: principal[key] for key in ("role", "user_id", "username") if key in principal},
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise d.DomainError("A concurrent metadata correction won; reload before retrying", 409) from exc
        return (
            agent_work_summary(session, work) if view == "agent" else work_record(session, work, detail=True)
        )

    @router.post("/{work_id}/readings", status_code=201)
    def link(
        work_id: str,
        payload: ReadingInput,
        workspace=Depends(require_role("researcher")),
        session=Depends(get_session),
    ):
        work = d.scoped(session, DiscoveredWork, work_id, workspace)
        doc = d.scoped(session, m.Document, payload.document_id, workspace, work.project_id)
        if doc.kind != "source" or not doc.representation_id or not doc.capture_id:
            raise d.DomainError("Only an acquired source document can establish a scholarly reading", 422)
        rep = d.scoped(session, m.Representation, doc.representation_id, workspace, work.project_id)
        cap = d.scoped(session, m.Capture, rep.capture_id, workspace, work.project_id)
        if d.digest(rep.text) != rep.content_hash or not rep.text.strip():
            raise d.DomainError("Saved representation failed integrity verification", 422)
        try:
            d.read_blob(cap, session.info.get("settings"))
        except Exception as exc:
            raise d.DomainError("Saved scholarly content failed raw integrity verification", 422) from exc
        source = d.scoped(session, m.Source, cap.source_id, workspace, work.project_id)
        provenance = verify_reading_provenance(session, work, source, cap)
        actual_scope = content_scope(session, rep, cap)
        if actual_scope == "metadata_only" or (
            actual_scope == "abstract" and payload.content_scope == "fulltext"
        ):
            raise d.DomainError("Metadata or abstract content cannot be promoted to full text", 422)
        if payload.content_scope == "fulltext" and not payload.fulltext_reviewed:
            raise d.DomainError("Full-text linking requires explicit review of the saved content", 422)
        existing = session.scalar(
            select(WorkReading).where(WorkReading.work_id == work.id, WorkReading.representation_id == rep.id)
        )
        if existing:
            if existing.content_scope != payload.content_scope:
                raise d.DomainError("An immutable scholarly reading cannot change its content scope", 409)
            return d.serialize(existing)
        row = WorkReading(
            workspace_id=workspace,
            project_id=work.project_id,
            work_id=work.id,
            document_id=doc.id,
            representation_id=rep.id,
            capture_id=cap.id,
            content_scope=payload.content_scope,
            review_note=payload.review_note,
            provenance_json=provenance,
        )
        session.add(row)
        session.commit()
        return d.serialize(row)

    return router
