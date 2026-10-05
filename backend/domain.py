"""Transport-independent, tenant-scoped application services.

Transactions are owned by callers. Immutable evidence always points to a specific
capture/representation/block; a later fetch or parser run cannot move its anchor.
"""

import io
import json
import logging
import math
import os
import re
import time
import zipfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from . import models as m
from .config import settings


class DomainError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def digest(data: bytes | str) -> str:
    return sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def serialize(obj):
    if obj is None:
        return None
    result = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    # Storage keys are an implementation detail, never bearer access to a file.
    result.pop("blob_path", None)
    result.pop("request_hash", None)
    if isinstance(result.get("input_json"), dict):
        result["input_json"] = {
            k: v for k, v in result["input_json"].items() if k not in {"blob_path", "api_key", "token"}
        }
    return result


def scoped(session: Session, model, resource_id: str, workspace_id: str, project_id: str | None = None):
    q = select(model).where(model.id == resource_id, model.workspace_id == workspace_id)
    if project_id is not None and hasattr(model, "project_id"):
        q = q.where(model.project_id == project_id)
    value = session.scalar(q)
    if value is None:
        raise DomainError("Resource not found", 404)
    return value


def project(session, workspace_id, project_id):
    return scoped(session, m.Project, project_id, workspace_id)


def create_project(session, workspace_id: str, name: str, description: str = ""):
    if not name.strip():
        raise DomainError("Project name cannot be blank", 422)
    if session.get(m.Workspace, workspace_id) is None:
        session.add(m.Workspace(id=workspace_id, name=workspace_id))
        session.flush()
    item = m.Project(workspace_id=workspace_id, name=name.strip(), description=description)
    session.add(item)
    session.flush()
    emit_event(session, workspace_id, item.id, "project.created", {"project_id": item.id})
    return item


def emit_event(session, workspace_id, project_id, event_type, payload):
    for sub in session.scalars(
        select(m.Subscription).where(
            m.Subscription.workspace_id == workspace_id,
            m.Subscription.project_id == project_id,
            m.Subscription.active.is_(True),
        )
    ):
        if "*" in sub.event_types or event_type in sub.event_types:
            session.add(
                m.OutboxEvent(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    subscription_id=sub.id,
                    event_type=event_type,
                    payload=payload,
                )
            )


def create_operation(session, workspace_id, project_id, input_json, *, kind="ingest", idempotency_key=None):
    project(session, workspace_id, project_id)
    request_hash = digest(
        json.dumps(
            {"project_id": project_id, "kind": kind, "input": input_json}, sort_keys=True, ensure_ascii=False
        )
    )
    if idempotency_key:
        existing = session.scalar(
            select(m.Operation).where(
                m.Operation.workspace_id == workspace_id, m.Operation.idempotency_key == idempotency_key
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise DomainError("Idempotency key already used with a different request", 409)
            return existing
    operation = m.Operation(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=kind,
        input_json=input_json,
        request_hash=request_hash,
        idempotency_key=idempotency_key,
    )
    session.add(operation)
    session.flush()
    session.add(
        m.OperationDispatch(workspace_id=workspace_id, project_id=project_id, operation_id=operation.id)
    )
    emit_event(
        session, workspace_id, project_id, "operation.pending", {"operation_id": operation.id, "kind": kind}
    )
    session.flush()
    return operation


def tokenize(value: str) -> list[str]:
    """One tokenizer for lexical indexing and queries, including CJK words."""
    try:
        import jieba

        tokens = jieba.cut_for_search(value.lower())
    except ImportError:
        tokens = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", value.lower())
    return [t.strip() for t in tokens if t.strip() and any(c.isalnum() for c in t)]


def write_blob(raw: bytes, blob_dir=None):
    from .storage import LocalContentStore, get_store

    if os.getenv("STORAGE_BACKEND", "local") == "s3":
        store = get_store()
        obj = store.put(raw)
        return f"s3://{store.bucket}/{obj.key}"
    root = Path(blob_dir or settings.blob_dir).resolve()
    obj = LocalContentStore(root).put(raw)
    return str(root / obj.key)


def read_blob(capture, settings_override=None):
    """Resolve by digest in the current store, so database restores are relocatable."""
    from .storage import LocalContentStore, StorageError, get_store, object_key

    active_settings = settings_override or settings
    if os.getenv("STORAGE_BACKEND", "local") == "s3":
        return get_store().get(capture.content_hash)
    try:
        return LocalContentStore(active_settings.blob_dir).get(capture.content_hash)
    except StorageError:
        # Opt-in migration compatibility only; ordinary reads never follow an
        # arbitrary historical absolute filesystem path from a restored database.
        if os.getenv("ALLOW_LEGACY_BLOB_PATHS", "false").lower() not in {"1", "true", "yes"}:
            raise
        path = Path(capture.blob_path)
        expected = object_key(capture.content_hash)
        if not str(path).endswith("/" + expected):
            raise DomainError("Raw object path is not content-addressed", 500)
        return LocalContentStore(path.parents[2]).get(capture.content_hash)


def split_blocks(content: str, max_size=1800):
    blocks = []
    # Preserve exact offsets and whitespace; splitting never rewrites source text.
    for start in range(0, len(content), max_size):
        end = min(start + max_size, len(content))
        blocks.append(
            {
                "text": content[start:end],
                "start_offset": start,
                "end_offset": end,
                "locator_json": {"start_offset": start, "end_offset": end},
            }
        )
    return blocks


def archive_capture(
    session, workspace_id, project_id, source_uri, kind, title, raw_bytes, media_type,
    metadata=None, access_scope="workspace", representation_variant="default", blob_dir=None,
    classification="internal",
):
    """Store original bytes and identity only; parsing never determines archive visibility."""
    project(session, workspace_id, project_id)
    if classification not in {"public", "internal", "sensitive"}:
        raise DomainError("Invalid source classification", 422)
    source = session.scalar(select(m.Source).where(
        m.Source.workspace_id == workspace_id, m.Source.project_id == project_id,
        m.Source.canonical_uri == source_uri, m.Source.access_scope == access_scope,
        m.Source.representation_variant == representation_variant,
    ).with_for_update())
    if source is None:
        source = m.Source(workspace_id=workspace_id, project_id=project_id, canonical_uri=source_uri,
                          kind=kind, title=title or source_uri, classification=classification,
                          access_scope=access_scope, representation_variant=representation_variant)
        session.add(source)
        session.flush()
    if source.classification != classification:
        raise DomainError("Source classification is immutable; select a distinct access scope or representation variant", 409)
    previous = session.scalar(select(m.Capture).where(m.Capture.source_id == source.id)
                              .order_by(m.Capture.created_at.desc()))
    checksum = digest(raw_bytes)
    unchanged = previous is not None and previous.content_hash == checksum
    if unchanged:
        capture = previous
        # Reusing an old identity must not silently change content-scope declarations.
        if capture.metadata_json.get("content_scope", "unspecified") != (metadata or {}).get("content_scope", "unspecified"):
            raise DomainError("Saved content scope is immutable", 409)
        read_blob(capture, session.info.get("settings"))
    else:
        capture = m.Capture(workspace_id=workspace_id, project_id=project_id, source_id=source.id,
                            previous_capture_id=previous.id if previous else None, content_hash=checksum,
                            blob_path=write_blob(raw_bytes, blob_dir), media_type=media_type,
                            byte_size=len(raw_bytes), metadata_json={**dict(metadata or {}), "raw_hash": checksum,
                            "storage_backend": "s3" if os.getenv("STORAGE_BACKEND") == "s3" else "local",
                            "archived_before_processing": True})
        session.add(capture)
        session.flush()
    session.add(m.FetchObservation(workspace_id=workspace_id, project_id=project_id,
                                  source_id=source.id, capture_id=capture.id,
                                  status="unchanged" if unchanged else "changed", content_hash=checksum,
                                  metadata_json={"archival_complete": True}))
    saved = {"source_id": source.id, "capture_id": capture.id, "unchanged": unchanged,
             "archival_complete": True}
    if not unchanged:
        emit_event(session, workspace_id, project_id, "capture.created", saved)
    session.flush()
    return saved


def store_capture(
    session,
    workspace_id,
    project_id,
    source_uri,
    kind,
    title,
    raw_bytes,
    media_type,
    extracted_text,
    blocks=None,
    parser="plain-text-v1",
    metadata=None,
    access_scope="workspace",
    representation_variant="default",
    blob_dir=None,
    classification="internal",
):
    project(session, workspace_id, project_id)
    if classification not in {"public", "internal", "sensitive"}:
        raise DomainError("Invalid source classification", 422)
    if not extracted_text.strip():
        raise DomainError("No extractable text was found", 422)
    metadata = dict(metadata or {})
    raw_hash = digest(raw_bytes)
    expected_hash = metadata.get("raw_hash") or metadata.get("raw_sha256")
    if expected_hash and expected_hash != raw_hash:
        raise DomainError("Raw content digest does not match processor result", 422)
    source = session.scalar(
        select(m.Source).where(
            m.Source.workspace_id == workspace_id,
            m.Source.project_id == project_id,
            m.Source.canonical_uri == source_uri,
            m.Source.access_scope == access_scope,
            m.Source.representation_variant == representation_variant,
        )
    )
    if source is None:
        source = m.Source(
            workspace_id=workspace_id,
            project_id=project_id,
            canonical_uri=source_uri,
            kind=kind,
            title=title or source_uri,
            classification=classification,
            access_scope=access_scope,
            representation_variant=representation_variant,
        )
        session.add(source)
        session.flush()
    if source.classification != classification:
        raise DomainError(
            "Source classification is immutable; select a distinct access scope or representation variant",
            409,
        )
    previous = session.scalar(
        select(m.Capture).where(m.Capture.source_id == source.id).order_by(m.Capture.created_at.desc())
    )
    if previous is not None and previous.content_hash == raw_hash:
        # Every successful check is observable, but unchanged bytes do not invent a new capture.
        session.add(
            m.FetchObservation(
                workspace_id=workspace_id,
                project_id=project_id,
                source_id=source.id,
                capture_id=previous.id,
                status="unchanged",
                content_hash=raw_hash,
                metadata_json=metadata,
            )
        )
        rep = session.scalar(
            select(m.Representation)
            .where(
                m.Representation.capture_id == previous.id,
                m.Representation.parser == parser,
                m.Representation.content_hash == digest(extracted_text),
            )
            .order_by(m.Representation.created_at.desc())
        )
        if rep is not None and rep.metadata_json.get("config_hash") == metadata.get("config_hash"):
            doc = session.scalar(select(m.Document).where(m.Document.representation_id == rep.id))
            generation = session.scalar(
                select(m.IndexGeneration).where(
                    m.IndexGeneration.representation_id == rep.id, m.IndexGeneration.index_kind == "lexical"
                )
            )
            block_ids = list(
                session.scalars(
                    select(m.Block.id).where(m.Block.representation_id == rep.id).order_by(m.Block.ordinal)
                )
            )
            return {
                "source_id": source.id,
                "capture_id": previous.id,
                "representation_id": rep.id,
                "document_id": doc.id,
                "index_generation_id": generation.id,
                "block_ids": block_ids,
                "unchanged": True,
                "representation_changed": False,
            }
        result = store_representation(
            session,
            workspace_id,
            project_id,
            previous,
            extracted_text,
            blocks,
            parser,
            metadata,
            title or source.title,
        )
        return {
            **result,
            "source_id": source.id,
            "capture_id": previous.id,
            "unchanged": True,
            "representation_changed": True,
        }
    path = write_blob(raw_bytes, blob_dir or getattr(session.info.get("settings"), "blob_dir", None))
    capture = m.Capture(
        workspace_id=workspace_id,
        project_id=project_id,
        source_id=source.id,
        previous_capture_id=previous.id if previous else None,
        content_hash=raw_hash,
        blob_path=path,
        media_type=media_type,
        byte_size=len(raw_bytes),
        metadata_json=metadata,
    )
    session.add(capture)
    session.flush()
    session.add(
        m.FetchObservation(
            workspace_id=workspace_id,
            project_id=project_id,
            source_id=source.id,
            capture_id=capture.id,
            status="unchanged" if previous and previous.content_hash == raw_hash else "changed",
            content_hash=raw_hash,
        )
    )
    result = store_representation(
        session,
        workspace_id,
        project_id,
        capture,
        extracted_text,
        blocks,
        parser,
        metadata,
        title or source.title,
    )
    result.update(source_id=source.id, capture_id=capture.id)
    emit_event(session, workspace_id, project_id, "capture.created", result)
    return result


def store_representation(
    session,
    workspace_id,
    project_id,
    capture,
    extracted_text,
    blocks=None,
    parser="plain-text-v1",
    metadata=None,
    title=None,
):
    metadata = dict(metadata or {})
    if metadata.get("publication_gates") and not all(metadata["publication_gates"].values()):
        raise DomainError("Publication gates have not all passed", 422)
    if not tokenize(extracted_text):
        raise DomainError("Representation contains no searchable tokens", 422)
    if capture.workspace_id != workspace_id or capture.project_id != project_id:
        raise DomainError("Resource not found", 404)
    blocks = blocks if blocks is not None else split_blocks(extracted_text)
    if not blocks:
        raise DomainError("Representation requires at least one block", 422)
    previous_end = 0
    for b in blocks:
        start, end = b["start_offset"], b["end_offset"]
        if (
            type(start) is not int
            or type(end) is not int
            or start < previous_end
            or end <= start
            or end > len(extracted_text)
            or extracted_text[start:end] != b["text"]
        ):
            raise DomainError("Block source map failed exact-text validation", 422)
        previous_end = end
        if b.get("quote_hash") and b["quote_hash"] != digest(b["text"]):
            raise DomainError("Block quote digest mismatch", 422)
    rep = m.Representation(
        workspace_id=workspace_id,
        project_id=project_id,
        capture_id=capture.id,
        text=extracted_text,
        content_hash=digest(extracted_text),
        parser=parser,
        metadata_json=metadata,
    )
    session.add(rep)
    session.flush()
    saved_blocks = []
    for ordinal, b in enumerate(blocks):
        locator = dict(b.get("locator_json") or {})
        if b.get("page") is not None:
            locator["page"] = b["page"]
        locator.update(start_offset=b["start_offset"], end_offset=b["end_offset"])
        block = m.Block(
            workspace_id=workspace_id,
            project_id=project_id,
            representation_id=rep.id,
            ordinal=ordinal,
            text=b["text"],
            start_offset=b["start_offset"],
            end_offset=b["end_offset"],
            locator_json=locator,
            content_hash=digest(b["text"]),
            search_text=" ".join(tokenize(b["text"])),
        )
        session.add(block)
        saved_blocks.append(block)
    generation = m.IndexGeneration(
        workspace_id=workspace_id,
        project_id=project_id,
        representation_id=rep.id,
        metadata_json={"ready": True, "tokenizer": "jieba-cut-for-search-v1", "block_count": len(blocks)},
    )
    session.add(generation)
    doc = m.Document(
        workspace_id=workspace_id,
        project_id=project_id,
        title=title or "Source document",
        kind="source",
        source_id=capture.source_id,
        capture_id=capture.id,
        representation_id=rep.id,
        content=extracted_text,
    )
    session.add(doc)
    session.flush()
    session.add(
        m.DocumentVersion(
            workspace_id=workspace_id,
            project_id=project_id,
            document_id=doc.id,
            version=1,
            title=doc.title,
            content=extracted_text,
            representation_id=rep.id,
        )
    )
    session.flush()
    return {
        "document_id": doc.id,
        "representation_id": rep.id,
        "index_generation_id": generation.id,
        "block_ids": [b.id for b in saved_blocks],
    }


def search(session, workspace_id, project_id, query, limit=20):
    from .scholarly import content_scope

    project(session, workspace_id, project_id)
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return {"results": [], "query": query, "mode": "lexical", "vector_available": False}
    q = (
        select(m.Block, m.Representation, m.Capture, m.Source, m.Document)
        .join(m.Representation, m.Block.representation_id == m.Representation.id)
        .join(m.Capture, m.Representation.capture_id == m.Capture.id)
        .join(m.Source, m.Capture.source_id == m.Source.id)
        .join(m.Document, m.Document.representation_id == m.Representation.id)
        .where(m.Block.workspace_id == workspace_id, m.Block.project_id == project_id)
    )
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import or_

        conditions = [
            sql_text(
                f"to_tsvector('simple', blocks.search_text) @@ plainto_tsquery('simple', :term_{i})"
            ).bindparams(**{f"term_{i}": term})
            for i, term in enumerate(terms)
        ]
        q = q.where(or_(*conditions))
    else:
        from sqlalchemy import or_

        q = q.where(or_(*[m.Block.search_text.contains(t, autoescape=True) for t in terms]))
    hits = []
    for block, rep, cap, source, doc in session.execute(q.limit(2000)):
        block_terms = tokenize(block.text)
        score = sum(block_terms.count(t) for t in terms) / max(len(block_terms), 1) ** 0.5
        hits.append(
            {
                "document_id": doc.id,
                "block_id": block.id,
                "representation_id": rep.id,
                "capture_id": cap.id,
                "source_id": source.id,
                "title": doc.title,
                "source_uri": source.canonical_uri,
                "classification": source.classification,
                "content_scope": content_scope(session, rep, cap),
                "text": block.text,
                "score": round(score, 6),
                "locator_json": block.locator_json,
            }
        )
    hits.sort(key=lambda h: (-h["score"], h["block_id"]))
    return {"results": hits[: min(limit, 100)], "query": query, "mode": "lexical", "vector_available": False}


def create_evidence(session, workspace_id, project_id, block_id, quote, start_offset=None, evidence_id=None):
    project(session, workspace_id, project_id)
    block = scoped(session, m.Block, block_id, workspace_id, project_id)
    if not quote:
        raise DomainError("Evidence quote cannot be empty", 422)
    offset = block.text.find(quote) if start_offset is None else start_offset
    if offset < 0 or block.text[offset : offset + len(quote)] != quote:
        raise DomainError("Quote does not exactly match the referenced block", 422)
    rep = scoped(session, m.Representation, block.representation_id, workspace_id, project_id)
    cap = scoped(session, m.Capture, rep.capture_id, workspace_id, project_id)
    from .scholarly import content_scope

    scope = content_scope(session, rep, cap)
    if scope == "metadata_only":
        raise DomainError("Discovery metadata cannot establish research evidence", 422)
    if evidence_id:
        existing = session.get(m.Evidence, evidence_id)
        if existing:
            if (
                existing.workspace_id != workspace_id
                or existing.project_id != project_id
                or existing.block_id != block.id
                or existing.quote != quote
                or existing.start_offset != offset
            ):
                raise DomainError("Evidence identity conflict", 409)
            return existing
    item = m.Evidence(
        id=evidence_id or m.new_id(),
        workspace_id=workspace_id,
        project_id=project_id,
        block_id=block.id,
        capture_id=cap.id,
        quote=quote,
        start_offset=offset,
        end_offset=offset + len(quote),
        content_hash=digest(quote),
        locator_json={
            **block.locator_json,
            "content_scope": scope,
            "representation_start_offset": block.start_offset + offset,
            "representation_end_offset": block.start_offset + offset + len(quote),
        },
    )
    session.add(item)
    session.flush()
    return item


def validate_evidence(session, workspace_id, project_id, evidence_ids):
    valid = []
    for evidence_id in evidence_ids:
        ev = scoped(session, m.Evidence, evidence_id, workspace_id, project_id)
        block = scoped(session, m.Block, ev.block_id, workspace_id, project_id)
        rep = scoped(session, m.Representation, block.representation_id, workspace_id, project_id)
        cap = scoped(session, m.Capture, ev.capture_id, workspace_id, project_id)
        try:
            read_blob(cap, session.info.get("settings"))
        except Exception as exc:
            raise DomainError("Evidence raw object failed integrity verification", 422) from exc
        if (
            digest(rep.text) != rep.content_hash
            or digest(block.text) != block.content_hash
            or rep.text[block.start_offset : block.end_offset] != block.text
        ):
            raise DomainError("Evidence representation integrity check failed", 422)
        if (
            rep.capture_id != ev.capture_id
            or block.text[ev.start_offset : ev.end_offset] != ev.quote
            or digest(ev.quote) != ev.content_hash
        ):
            raise DomainError("Evidence anchor failed integrity verification", 422)
        valid.append(ev)
    return valid


def claim_evidence_ids(claim):
    return list(
        dict.fromkeys(
            list(claim.get("evidence_ids", []))
            + [link["evidence_id"] for link in claim.get("evidence_links", [])]
        )
    )


def validate_claims(session, workspace_id, project_id, content, claims):
    if not claims:
        raise DomainError("At least one evidence-backed claim is required", 422)
    for claim in claims:
        if not claim.get("text") or claim["text"] not in content:
            raise DomainError("Every claim must appear verbatim in proposal content", 422)
        if not claim.get("evidence_ids"):
            raise DomainError("Every claim requires evidence", 422)
        verified = validate_evidence(session, workspace_id, project_id, claim_evidence_ids(claim))
        from .scholarly import content_scope

        for evidence in verified:
            block = scoped(session, m.Block, evidence.block_id, workspace_id, project_id)
            rep = scoped(session, m.Representation, block.representation_id, workspace_id, project_id)
            cap = scoped(session, m.Capture, evidence.capture_id, workspace_id, project_id)
            scope = content_scope(session, rep, cap)
            if scope == "metadata_only":
                raise DomainError("Metadata-only evidence cannot support a claim", 422)
            if scope == "abstract" and claim.get("scope") != "abstract":
                raise DomainError("Claims using abstract evidence must explicitly declare scope='abstract'", 422)
        for link in claim.get("evidence_links", []):
            if link.get("relation") not in {"supporting", "contradicting", "limiting"}:
                raise DomainError("Invalid claim evidence relation", 422)
            validate_evidence(session, workspace_id, project_id, [link["evidence_id"]])


def create_proposal(
    session, workspace_id, project_id, title, content, claims, target_document_id=None, base_version=None
):
    project(session, workspace_id, project_id)
    validate_claims(session, workspace_id, project_id, content, claims)
    if bool(target_document_id) != bool(base_version):
        raise DomainError("Target and base version must be supplied together", 422)
    if target_document_id:
        doc = scoped(session, m.Document, target_document_id, workspace_id, project_id)
        if doc.kind != "report":
            raise DomainError("Source documents are immutable; proposals may update reports only", 422)
        if doc.version != base_version:
            raise DomainError("Report version has changed", 409)
    item = m.Proposal(
        workspace_id=workspace_id,
        project_id=project_id,
        title=title,
        content=content,
        claims=claims,
        target_document_id=target_document_id,
        base_version=base_version,
    )
    session.add(item)
    session.flush()
    for ordinal, claim in enumerate(claims):
        record = m.ClaimRecord(
            workspace_id=workspace_id,
            project_id=project_id,
            proposal_id=item.id,
            ordinal=ordinal,
            text=claim["text"],
            scope=claim.get("scope", ""),
            as_of=claim.get("as_of"),
            status=claim.get("status", "unreviewed"),
            limitations=claim.get("limitations", []),
        )
        session.add(record)
        session.flush()
        links = list(claim.get("evidence_links") or [])
        linked_ids = {link["evidence_id"] for link in links}
        links.extend(
            {"evidence_id": eid, "relation": "supporting"}
            for eid in claim["evidence_ids"]
            if eid not in linked_ids
        )
        seen = set()
        for link in links:
            key = (link["evidence_id"], link.get("relation", "supporting"))
            if key not in seen:
                session.add(
                    m.ClaimEvidence(
                        workspace_id=workspace_id,
                        project_id=project_id,
                        claim_id=record.id,
                        evidence_id=key[0],
                        relation=key[1],
                        rationale=link.get("rationale", ""),
                    )
                )
                seen.add(key)
    session.flush()
    emit_event(session, workspace_id, project_id, "proposal.created", {"proposal_id": item.id})
    return item


def markdown_sections(content):
    sections, heading, lines = {}, "__preamble__", []
    for line in content.splitlines(keepends=True):
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line.strip())
        if match:
            if heading in sections:
                raise DomainError("Duplicate section headings cannot be manually locked", 422)
            sections[heading] = "".join(lines)
            heading, lines = match.group(1), [line]
        else:
            lines.append(line)
    if heading in sections:
        raise DomainError("Duplicate section headings cannot be manually locked", 422)
    sections[heading] = "".join(lines)
    return sections


def publish_proposal(session, workspace_id, proposal_id, expected_version=None, accepted_claim_indices=None):
    # Serialize repeated/concurrent publishes of the same proposal.
    if session.bind.dialect.name == "sqlite":
        session.execute(
            update(m.Proposal)
            .where(m.Proposal.id == proposal_id, m.Proposal.workspace_id == workspace_id)
            .values(status=m.Proposal.status)
        )
    item = session.scalar(
        select(m.Proposal)
        .where(m.Proposal.id == proposal_id, m.Proposal.workspace_id == workspace_id)
        .with_for_update()
    )
    if item is None:
        raise DomainError("Resource not found", 404)
    if item.status == "published":
        return scoped(session, m.Document, item.published_document_id, workspace_id, item.project_id)
    if item.status != "pending":
        raise DomainError("Only pending proposals can be published", 409)
    # The project row is the creation-time CAS anchor. Different initial proposals
    # otherwise lock different rows and could each create a competing main report.
    locked_project = session.scalar(
        select(m.Project)
        .where(m.Project.id == item.project_id, m.Project.workspace_id == workspace_id)
        .with_for_update()
    )
    if locked_project is None:
        raise DomainError("Resource not found", 404)
    if item.target_document_id is None:
        existing_report = session.scalar(
            select(m.Document.id)
            .where(
                m.Document.workspace_id == workspace_id,
                m.Document.project_id == item.project_id,
                m.Document.kind == "report",
            )
            .limit(1)
        )
        if existing_report is not None:
            raise DomainError(
                "A project report already exists; rebase the proposal with its target_document_id and base_version",
                409,
            )
    validate_claims(session, workspace_id, item.project_id, item.content, item.claims)
    selected = (
        list(range(len(item.claims)))
        if accepted_claim_indices is None
        else sorted(set(accepted_claim_indices))
    )
    if not selected or any(type(i) is not int or i < 0 or i >= len(item.claims) for i in selected):
        raise DomainError("Accepted claim indexes must reference this proposal", 422)
    published_content = (
        item.content
        if accepted_claim_indices is None
        else "# "
        + item.title
        + "\n\n"
        + "\n\n".join(
            item.claims[i]["text"]
            + " "
            + " ".join("[evidence:" + e + "]" for e in item.claims[i]["evidence_ids"])
            for i in selected
        )
    )
    if item.target_document_id:
        current = session.scalar(
            select(m.Document)
            .where(
                m.Document.id == item.target_document_id,
                m.Document.workspace_id == workspace_id,
                m.Document.project_id == item.project_id,
            )
            .with_for_update()
        )
        if current is None:
            raise DomainError("Resource not found", 404)
        if current.locked_sections:
            before, after = markdown_sections(current.content), markdown_sections(published_content)
            if any(before.get(name) != after.get(name) for name in current.locked_sections):
                raise DomainError("Proposal changes a manually locked report section", 409)
        version = item.base_version if expected_version is None else expected_version
        if version != item.base_version:
            raise DomainError("Expected version does not match proposal base", 409)
        result = session.execute(
            update(m.Document)
            .where(
                m.Document.id == item.target_document_id,
                m.Document.workspace_id == workspace_id,
                m.Document.project_id == item.project_id,
                m.Document.kind == "report",
                m.Document.version == version,
            )
            .values(title=item.title, content=published_content, version=version + 1, updated_at=m.utcnow())
        )
        if result.rowcount != 1:
            raise DomainError("Report version changed; rebase the proposal before publishing", 409)
        session.flush()
        doc = scoped(session, m.Document, item.target_document_id, workspace_id, item.project_id)
        session.refresh(doc)
    else:
        if expected_version is not None:
            raise DomainError("New report has no expected version", 422)
        doc = m.Document(
            workspace_id=workspace_id,
            project_id=item.project_id,
            title=item.title,
            kind="report",
            content=published_content,
        )
        session.add(doc)
        session.flush()
    records = list(session.scalars(select(m.ClaimRecord).where(m.ClaimRecord.proposal_id == item.id)))
    for record in records:
        record.review_status = "accepted" if record.ordinal in selected else "rejected"
        record.last_reviewed_at = m.utcnow()
    item.accepted_claim_indices = selected
    session.add(
        m.DocumentVersion(
            workspace_id=workspace_id,
            project_id=item.project_id,
            document_id=doc.id,
            version=doc.version,
            title=doc.title,
            content=doc.content,
            proposal_id=item.id,
            evidence_ids=list(
                dict.fromkeys(eid for i in selected for eid in claim_evidence_ids(item.claims[i]))
            ),
            claim_ids=[r.id for r in records if r.ordinal in selected],
        )
    )
    item.status, item.published_document_id, item.published_at = "published", doc.id, m.utcnow()
    session.flush()
    emit_event(
        session,
        workspace_id,
        item.project_id,
        "proposal.published",
        {"proposal_id": item.id, "document_id": doc.id, "version": doc.version},
    )
    return doc


def run_event(session, run, event_type, data):
    seq = (session.scalar(select(func.max(m.RunEvent.sequence)).where(m.RunEvent.run_id == run.id)) or 0) + 1
    event = m.RunEvent(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.id,
        sequence=seq,
        type=event_type,
        data=data,
    )
    session.add(event)
    session.flush()
    return event


def create_research_run(session, workspace_id, project_id, question, provider="local", **config):
    project(session, workspace_id, project_id)
    run = m.ResearchRun(
        workspace_id=workspace_id,
        project_id=project_id,
        question=question,
        provider=provider,
        config_json=config,
    )
    session.add(run)
    session.flush()
    run_event(session, run, "run.created", {"question": question, "provider": provider})
    op = create_operation(session, workspace_id, project_id, {"run_id": run.id}, kind="research")
    return run, op


def discover_and_ingest(session, run, config, started, audit):
    """Bounded discovery; only ordinary saved captures can become evidence."""
    from .continuous import discover_sources
    from .processing import archive_ingestion, ingest_operation

    if config.get("web_discovery") is not True:
        return {"searches": 0, "ingestions": 0, "document_ids": [], "search_cost_reserved_usd": 0.0}
    raw_price = os.getenv("SEARCH_PRICE_PER_REQUEST_USD")
    if raw_price is None:
        raise DomainError("Configure SEARCH_PRICE_PER_REQUEST_USD explicitly before web discovery", 422)
    try:
        price = float(raw_price)
    except ValueError as exc:
        raise DomainError("Search request pricing must be a nonnegative finite value", 422) from exc
    if not math.isfinite(price) or price < 0:
        raise DomainError("Search request pricing must be a nonnegative finite value", 422)
    max_searches, max_tools = config.get("max_searches", 3), config.get("max_tool_calls", 16)
    if max_searches < 2 or max_tools < 5:
        raise DomainError("Web discovery requires at least 2 searches and 5 tool calls", 422)
    queries = config.get("discovery_queries") or [run.question]
    planned_searches = min(3, max_searches - 1, len(queries), max_tools - 4)
    reserved = price * planned_searches
    if reserved > config.get("max_cost_usd", 0.0):
        raise DomainError("Search request reservation exceeds the research cost budget", 422)
    max_ingestions = min(config.get("max_documents", 8), max_tools - planned_searches - 3)
    discovery_config = {
        **config,
        "max_searches": planned_searches + 1,
        "max_documents": max_ingestions,
        "max_duration_seconds": max(
            5, int(config.get("max_duration_seconds", 180) - (time.monotonic() - started))
        ),
    }
    discovery = discover_sources(run.question, discovery_config, used_searches=1)
    audit.update(
        {
            "enabled": True,
            "provider": discovery.get("provider"),
            "searches_used": discovery.get("searches_used", 0),
            "leads": [],
        }
    )
    searches_used = discovery.get("searches_used", 0)
    run_event(
        session,
        run,
        "discovery.completed",
        {
            "candidate_count": len(discovery.get("candidates", [])),
            "searches_used": searches_used,
            "snippets_are_evidence": False,
        },
    )
    documents, attempts = [], 0
    for index, candidate in enumerate(discovery.get("candidates", [])[:max_ingestions]):
        lead = {
            "url": candidate.get("url"),
            "title": candidate.get("title", ""),
            "provider": candidate.get("provider"),
            "status": "unverified",
            "evidence_eligible": False,
        }
        audit["leads"].append(lead)
        # Default network deadline20 + isolated parser deadline33 + a margin.
        if config.get("max_duration_seconds", 180) - (time.monotonic() - started) < 55:
            lead["reason"] = "insufficient_remaining_time_for_safe_fetch_and_parse"
            continue
        operation = create_operation(
            session,
            run.workspace_id,
            run.project_id,
            {
                "type": "url",
                "url": candidate.get("url"),
                "title": candidate.get("title") or candidate.get("url"),
                "classification": config.get("discovery_classification", "internal"),
                "discovered_by_run_id": run.id,
            },
            idempotency_key=f"discovery:{run.id}:{index}",
        )
        lead["operation_id"] = operation.id
        if operation.status == "succeeded":
            saved = operation.result_json
        else:
            attempts += 1
            # Acquisition is an independent durable activity: a later research/model
            # failure must not erase originals that were already saved successfully.
            session.commit()
            try:
                with session.begin_nested():
                    operation.status, operation.started_at = "running", m.utcnow()
                    if not (operation.result_json or {}).get("archival_complete"):
                        archive_ingestion(session, operation)
                session.commit()
                with session.begin_nested():
                    saved = ingest_operation(session, operation)
                    complete_operation(session, operation, saved)
                session.commit()
            except Exception as exc:
                logging.getLogger(__name__).exception("Discovery candidate ingestion %s failed", operation.id)
                dispatch = session.scalar(
                    select(m.OperationDispatch).where(m.OperationDispatch.operation_id == operation.id)
                )
                dispatch.status, dispatch.delivered_at = "delivered", m.utcnow()
                fail_operation(
                    session, operation, f"{type(exc).__name__}: candidate could not be saved and validated"
                )
                lead["reason"] = operation.error
                if (operation.result_json or {}).get("archival_complete"):
                    lead["capture_id"] = operation.result_json["capture_id"]
                    lead["archival_complete"] = True
                session.commit()
                continue
        dispatch = session.scalar(
            select(m.OperationDispatch).where(m.OperationDispatch.operation_id == operation.id)
        )
        dispatch.status, dispatch.delivered_at = "delivered", m.utcnow()
        documents.append(saved["document_id"])
        lead.update(
            status="saved",
            evidence_eligible=True,
            capture_id=saved["capture_id"],
            document_id=saved["document_id"],
        )
    session.flush()
    run_event(
        session,
        run,
        "discovery.ingestions_completed",
        {"saved_count": len(documents), "attempts": attempts, "leads": audit["leads"]},
    )
    return {
        "searches": searches_used,
        "ingestions": attempts,
        "document_ids": documents,
        "search_cost_reserved_usd": price * searches_used,
    }


def research_run(session, run, external_call=None, research_context=None):
    from .providers import get_provider

    started = time.monotonic()
    config = run.config_json or {}
    research_context = research_context if research_context is not None else {}
    if run.provider != "local" and config.get("max_cost_usd", 0) <= 0:
        raise DomainError("External research requires an explicit positive cost budget", 422)
    run.status = "running"
    run_event(session, run, "run.started", {})
    discovery = discover_and_ingest(session, run, config, started, research_context)
    remaining_reads = config.get("max_tool_calls", 16) - discovery["searches"] - discovery["ingestions"] - 2
    if remaining_reads < 1:
        raise DomainError("Research tool-call budget exhausted before evidence reading", 422)
    results = search(
        session,
        run.workspace_id,
        run.project_id,
        run.question,
        limit=min(config.get("max_documents", 8), remaining_reads),
    )["results"]
    documents_read = set(discovery["document_ids"])
    failed_document_attempts = max(0, discovery["ingestions"] - len(discovery["document_ids"]))
    usable_document_cap = max(0, config.get("max_documents", 8) - failed_document_attempts)
    allowed_results = []
    for hit in results:
        if hit["document_id"] not in documents_read and len(documents_read) >= usable_document_cap:
            continue
        documents_read.add(hit["document_id"])
        allowed_results.append(hit)
    results = allowed_results
    run_event(
        session,
        run,
        "search.completed",
        {"hit_count": len(results), "block_ids": [r["block_id"] for r in results]},
    )
    if not results:
        raise DomainError(
            "No local evidence matched this question. Add sources or use a more specific question.", 422
        )
    evidence = []
    for hit in results:
        ev = create_evidence(
            session,
            run.workspace_id,
            run.project_id,
            hit["block_id"],
            hit["text"],
            evidence_id=str(uuid5(UUID(run.id), hit["block_id"])),
        )
        evidence.append(
            {
                "id": ev.id,
                "block_id": ev.block_id,
                "quote": ev.quote,
                "title": hit["title"],
                "source_uri": hit["source_uri"],
                "classification": hit["classification"],
                "content_scope": hit.get("content_scope", "unspecified"),
            }
        )
    run_event(session, run, "evidence.collected", {"evidence_ids": [e["id"] for e in evidence]})
    if time.monotonic() - started >= config.get("max_duration_seconds", 180):
        raise DomainError("Research time budget exhausted before model invocation", 422)
    kwargs = dict(  # noqa: C408 -- shared provider call arguments
        question=run.question,
        evidence=evidence,
        model=config.get("model"),
        reasoning_effort=config.get("reasoning_effort"),
        max_output_tokens=config.get("max_output_tokens", 2000),
        timeout=min(120, max(1, config.get("max_duration_seconds", 180) - (time.monotonic() - started))),
        max_cost_usd=max(0.0, config.get("max_cost_usd", 0.0) - discovery["search_cost_reserved_usd"]),
    )
    result = external_call(**kwargs) if external_call else get_provider(run.provider).research(**kwargs)
    elapsed = time.monotonic() - started
    if elapsed > config.get("max_duration_seconds", 180):
        raise DomainError("Research time budget was exceeded; proposal was not published", 422)
    run.usage_json = {
        **result.get("usage", {}),
        "model": result.get("model", config.get("model") or run.provider),
        "reasoning_effort": result.get("reasoning_effort", config.get("reasoning_effort")),
        "searches": discovery["searches"] + 1,
        "external_searches": discovery["searches"],
        "documents": len(documents_read) + failed_document_attempts,
        "ingestion_attempts": discovery["ingestions"],
        "tool_calls": len(evidence) + discovery["searches"] + discovery["ingestions"] + 2,
        "search_cost_reserved_usd": discovery["search_cost_reserved_usd"],
        "duration_seconds": round(elapsed, 3),
    }
    current_report = session.scalar(
        select(m.Document)
        .where(
            m.Document.workspace_id == run.workspace_id,
            m.Document.project_id == run.project_id,
            m.Document.kind == "report",
        )
        .order_by(m.Document.updated_at.desc())
    )
    abstract_ids = {item["id"] for item in evidence if item.get("content_scope") == "abstract"}
    for claim in result["claims"]:
        if abstract_ids.intersection(claim_evidence_ids(claim)):
            claim["scope"] = "abstract"
            claim["limitations"] = [*claim.get("limitations", []), "Supported only within an abstract; full text was not established"]
    proposal = create_proposal(
        session,
        run.workspace_id,
        run.project_id,
        result["title"],
        result["content"],
        result["claims"],
        current_report.id if current_report else None,
        current_report.version if current_report else None,
    )
    run.status, run.completed_at = "succeeded", m.utcnow()
    run.result_json = {
        "proposal_id": proposal.id,
        "evidence_ids": [e["id"] for e in evidence],
        "summary": result.get("summary", ""),
        "provider": run.provider,
        "usage": run.usage_json,
        "discovery": research_context if config.get("web_discovery") else None,
    }
    run_event(session, run, "proposal.created", {"proposal_id": proposal.id})
    run_event(session, run, "run.succeeded", run.result_json)
    return run.result_json


def complete_operation(session, operation, result):
    operation.status, operation.result_json, operation.completed_at, operation.error = (
        "succeeded",
        result,
        m.utcnow(),
        None,
    )
    emit_event(
        session,
        operation.workspace_id,
        operation.project_id,
        "operation.succeeded",
        {"operation_id": operation.id, "result": result},
    )


def fail_operation(session, operation, error):
    operation.status, operation.error, operation.completed_at = "failed", str(error)[:2000], m.utcnow()
    emit_event(
        session,
        operation.workspace_id,
        operation.project_id,
        "operation.failed",
        {"operation_id": operation.id, "error": operation.error},
    )


def execute_operation(operation_id: str, session_factory=None, force_resume=False, settings_override=None, defer_failure=False):
    from .db import SessionLocal

    factory = session_factory or SessionLocal
    with factory() as session:
        if session.bind.dialect.name == "sqlite":
            session.execute(
                update(m.Operation).where(m.Operation.id == operation_id).values(status=m.Operation.status)
            )
        op = session.scalar(select(m.Operation).where(m.Operation.id == operation_id).with_for_update())
        if op is None:
            raise DomainError("Operation not found", 404)
        if op.status == "succeeded" or (op.status == "failed" and not force_resume):
            return serialize(op)
        external_provider, cost_budget = None, 0.0
        if op.kind == "research":
            run = scoped(session, m.ResearchRun, op.input_json["run_id"], op.workspace_id, op.project_id)
            if run.provider != "local" or (run.config_json or {}).get("web_discovery") is True:
                external_provider = run.provider
                cost_budget = (run.config_json or {}).get("max_cost_usd", 0.0)
        elif op.kind == "vector_index" and op.input_json.get("provider") == "openai":
            external_provider = "openai-embeddings"
            cost_budget = op.input_json.get("max_cost_usd", float(os.getenv("EMBEDDING_MAX_BATCH_USD", "0")))
        reservation_id, cached_external_result = None, None
        if external_provider:
            reservation = session.scalar(
                select(m.ExternalCallReservation).where(m.ExternalCallReservation.operation_id == op.id)
            )
            if reservation is not None:
                if (
                    op.kind == "research"
                    and (run.config_json or {}).get("web_discovery") is not True
                    and reservation.status == "cached"
                    and reservation.output_json is not None
                ):
                    cached_external_result = reservation.output_json
                else:
                    message = "An earlier external call may have incurred cost. Automatic retry is blocked; inspect provider usage before explicitly creating a new run."
                    op.result_json = {**(op.result_json or {}), "error_type": "UncertainExternalCall", "error_retryable": False}
                    if defer_failure:
                        op.status, op.error, op.completed_at = "retrying", message, None
                    else:
                        fail_operation(session, op, message)
                    if op.kind == "research":
                        run.status, run.error, run.completed_at = ("retrying" if defer_failure else "failed"), message, (None if defer_failure else m.utcnow())
                        run_event(session, run, "run.attempt_failed" if defer_failure else "run.failed", {"error": message})
                    session.commit()
                    return serialize(op)
            else:
                reservation = m.ExternalCallReservation(
                    workspace_id=op.workspace_id,
                    project_id=op.project_id,
                    operation_id=op.id,
                    provider=(external_provider + "+web-discovery")
                    if op.kind == "research" and (run.config_json or {}).get("web_discovery")
                    else external_provider,
                    reserved_cost_usd=cost_budget,
                )
                session.add(reservation)
                session.flush()
            reservation_id = reservation.id
        op.status, op.started_at, op.error, op.completed_at = "running", m.utcnow(), None, None
        if op.kind == "research":
            run.status, run.error, run.completed_at = "running", None, None
        session.commit()

    def external_research_call(**kwargs):
        if cached_external_result is not None:
            for anchor in cached_external_result.get("evidence", []):
                create_evidence(
                    session,
                    op.workspace_id,
                    op.project_id,
                    anchor["block_id"],
                    anchor["quote"],
                    evidence_id=anchor["id"],
                )
            return cached_external_result.get("result", cached_external_result)
        from .providers import get_provider

        output = get_provider(external_provider).research(**kwargs)
        cache_payload = {"result": output, "evidence": kwargs.get("evidence", [])}
        if session.bind.dialect.name == "sqlite":
            # SQLite holds a database writer lock. Cache in the current transaction;
            # a rollback deliberately leaves the durable reservation uncertain and
            # blocks a second paid call rather than weakening the cost bound.
            reserved = session.get(m.ExternalCallReservation, reservation_id)
            reserved.status, reserved.output_json, reserved.completed_at = "cached", cache_payload, m.utcnow()
            session.flush()
            return output
        # Persist the expensive response before proposal validation/transaction commit.
        # A crash before this commit stays uncertain and can never issue another call.
        with factory() as cache_session:
            reserved = cache_session.get(m.ExternalCallReservation, reservation_id)
            reserved.status, reserved.output_json, reserved.completed_at = "cached", cache_payload, m.utcnow()
            cache_session.commit()
        return output

    research_context = {}
    try:
        with factory() as session:
            if settings_override is not None:
                session.info["settings"] = settings_override
            # A write lock provides the same serial execution guarantee in SQLite
            # tests as SELECT FOR UPDATE does in PostgreSQL production.
            if session.bind.dialect.name == "sqlite":
                session.execute(
                    update(m.Operation)
                    .where(m.Operation.id == operation_id)
                    .values(status=m.Operation.status)
                )
            op = session.scalar(select(m.Operation).where(m.Operation.id == operation_id).with_for_update())
            if op.status == "succeeded":
                return serialize(op)
            if op.kind == "ingest":
                from .processing import archive_ingestion, ingest_operation

                if not (op.result_json or {}).get("archival_complete"):
                    archive_ingestion(session, op)
                    # Original identity/bytes survive all later parser/quality failures.
                    session.commit()
                    if session.bind.dialect.name == "sqlite":
                        session.execute(update(m.Operation).where(m.Operation.id == operation_id)
                                        .values(status=m.Operation.status))
                    op = session.scalar(select(m.Operation).where(m.Operation.id == operation_id).with_for_update())
                    if op.status == "succeeded":
                        return serialize(op)
                result = ingest_operation(session, op)
                if op.input_json.get("watch_id"):
                    from .continuous import after_ingestion

                    change = after_ingestion(session, op, result)
                    if change is not None:
                        result["change"] = change
            elif op.kind == "research":
                run = scoped(session, m.ResearchRun, op.input_json["run_id"], op.workspace_id, op.project_id)
                result = research_run(
                    session, run, external_research_call if external_provider else None, research_context
                )
            elif op.kind == "connector_sync":
                from .connectors import enqueue_connector_watch
                from .scheduling import SourceWatch

                watch = scoped(
                    session, SourceWatch, op.input_json["watch_id"], op.workspace_id, op.project_id
                )
                result = enqueue_connector_watch(session, watch, op.input_json["invocation_id"])
            elif op.kind == "vector_index":
                from .vector_index import index_operation

                result = index_operation(session, op)
            elif op.kind == "pipeline_test":
                from .extensions import pipeline_test_operation

                result = pipeline_test_operation(session, op)
            elif op.kind == "reprocess":
                from .processing import reprocess_operation

                result = reprocess_operation(session, op)
            else:
                raise DomainError("Unknown operation kind", 422)
            complete_operation(session, op, result)
            session.commit()
            follow_on = (
                result.get("change", {}).get("research_operation_id") if isinstance(result, dict) else None
            )
            if settings_override is not None and settings_override.inline_worker:
                if follow_on:
                    execute_operation(follow_on, factory, settings_override=settings_override)
                if op.kind == "connector_sync":
                    for queued_id in result.get("operation_ids", []):
                        execute_operation(queued_id, factory, settings_override=settings_override)
            return serialize(op)
    except Exception as exc:
        logging.getLogger(__name__).exception("Operation %s failed", operation_id)
        with factory() as session:
            op = session.scalar(select(m.Operation).where(m.Operation.id == operation_id).with_for_update())
            if op.status == "succeeded":
                return serialize(op)
            # Parser/provider errors are observable without leaking credentials.
            message = (
                str(exc)
                if isinstance(exc, (DomainError, ValueError))
                else f"{type(exc).__name__}: processing failed; consult server logs"
            )
            from .parsers import ParseError
            from .processing import PipelineError
            from .security import UnsafeURLError

            deterministic = isinstance(exc, (ParseError, PipelineError, UnsafeURLError)) or (
                isinstance(exc, DomainError) and exc.status_code < 500
            )
            if defer_failure or op.result_json:
                op.result_json = {**(op.result_json or {}), "error_type": type(exc).__name__,
                                  "error_retryable": not deterministic}
            if defer_failure:
                op.status, op.error, op.completed_at = "retrying", str(message)[:2000], None
                emit_event(session, op.workspace_id, op.project_id, "operation.attempt_failed",
                           {"operation_id": op.id, "error": op.error, "retryable": not deterministic})
            else:
                fail_operation(session, op, message)
            archived_id = (op.result_json or {}).get("capture_id")
            if op.kind in {"ingest", "reprocess"} and (archived_id or op.input_json.get("capture_id")):
                cap = scoped(session, m.Capture, archived_id or op.input_json["capture_id"], op.workspace_id, op.project_id)
                session.add(m.FetchObservation(workspace_id=op.workspace_id, project_id=op.project_id,
                                              source_id=cap.source_id, capture_id=cap.id, status="processing_failed",
                                              error=message, metadata_json={"operation_id": op.id, "archival_complete": True}))
            if op.kind == "ingest" and not archived_id and op.input_json.get("type") == "url":
                try:
                    from .schemas import declared_original_url

                    source_uri = declared_original_url(op.input_json.get("url"))
                except ValueError:
                    source_uri = None
                if source_uri:
                    source = session.scalar(
                        select(m.Source).where(
                            m.Source.workspace_id == op.workspace_id,
                            m.Source.project_id == op.project_id,
                            m.Source.canonical_uri == source_uri,
                            m.Source.access_scope == op.input_json.get("access_scope", "workspace"),
                            m.Source.representation_variant
                            == op.input_json.get("representation_variant", "default"),
                        )
                    )
                    if source is not None:
                        session.add(
                            m.FetchObservation(
                                workspace_id=op.workspace_id,
                                project_id=op.project_id,
                                source_id=source.id,
                                status="failed",
                                error=message,
                                metadata_json={"operation_id": op.id, "requested_url": source_uri},
                            )
                        )
            if op.kind == "research":
                run = session.get(m.ResearchRun, op.input_json["run_id"])
                run.status, run.error, run.completed_at = ("retrying" if defer_failure else "failed"), message, (None if defer_failure else m.utcnow())
                if research_context:
                    for lead in research_context.get("leads", []):
                        # Acquisition commits independently; only research/proposal work rolled back.
                        lead["research_transaction_rolled_back"] = True
                        if lead.get("status") != "saved":
                            lead.update(status="unverified", evidence_eligible=False)
                    run.result_json = {"discovery": research_context, "proposal_created": False}
                run_event(session, run, "run.attempt_failed" if defer_failure else "run.failed", {"error": message})
            session.commit()
            return serialize(op)


def execute_research_run(run_id, session_factory=None, force_resume=False):
    from .db import SessionLocal

    factory = session_factory or SessionLocal
    with factory() as session:
        run = session.get(m.ResearchRun, run_id)
        if run is None:
            raise DomainError("Research run not found", 404)
        op = session.scalar(
            select(m.Operation)
            .where(
                m.Operation.project_id == run.project_id,
                m.Operation.workspace_id == run.workspace_id,
                m.Operation.kind == "research",
            )
            .order_by(m.Operation.created_at.desc())
        )
        if op is None or op.input_json.get("run_id") != run_id:
            op = next(
                (
                    o
                    for o in session.scalars(
                        select(m.Operation).where(
                            m.Operation.workspace_id == run.workspace_id, m.Operation.kind == "research"
                        )
                    )
                    if o.input_json.get("run_id") == run_id
                ),
                None,
            )
        if op is None:
            raise DomainError("Run operation not found", 404)
        operation_id = op.id
    return execute_operation(operation_id, factory, force_resume)


def export_project(session, workspace_id, project_id):
    from .extensions import PipelineRevision
    from .scheduling import SourceWatch
    from .scholarly import (
        DiscoveredWork,
        WorkAlias,
        WorkMetadataReview,
        WorkObservation,
        WorkReading,
        work_record,
    )

    proj = project(session, workspace_id, project_id)
    tables = (
        m.Source,
        m.Capture,
        m.Representation,
        m.Block,
        m.IndexGeneration,
        m.Document,
        m.DocumentVersion,
        m.Evidence,
        m.Question,
        m.ResearchRun,
        m.RunEvent,
        m.Proposal,
        m.FetchObservation,
        m.ClaimRecord,
        m.ClaimEvidence,
        PipelineRevision,
        SourceWatch,
        DiscoveredWork,
        WorkAlias,
        WorkObservation,
        WorkReading,
        WorkMetadataReview,
    )
    manifest = {
        "schema_version": 1,
        "exported_at": m.utcnow().isoformat(),
        "project": serialize(proj),
        "resources": {},
        "objects": [],
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for model in tables:
            objects = list(
                session.scalars(
                    select(model).where(model.workspace_id == workspace_id, model.project_id == project_id)
                )
            )
            records = [work_record(session, o) if model is DiscoveredWork else serialize(o) for o in objects]
            if model is SourceWatch:
                # Portable configuration, not live connector cursors or scheduler state.
                for record in records:
                    record.pop("connector_state", None)
                    record.pop("schedule_version", None)
            manifest["resources"][model.__tablename__] = records
            if model is m.Capture:
                for capture in objects:
                    try:
                        raw = read_blob(capture, session.info.get("settings"))
                    except Exception as exc:
                        raise DomainError(
                            "Cannot export: raw object failed integrity verification", 500
                        ) from exc
                    if digest(raw) != capture.content_hash:
                        raise DomainError("Cannot export: raw object failed integrity verification", 500)
                    member = f"raw/{capture.content_hash}"
                    if member not in z.namelist():
                        z.writestr(member, raw)
                        manifest["objects"].append(
                            {"path": member, "sha256": capture.content_hash, "size": len(raw)}
                        )
            if model is m.Document:
                for doc in objects:
                    z.writestr(f"documents/{doc.id}.md", doc.content)
        resources = manifest["resources"]
        projections = {
            "INDEX.md": f"# {proj.name}\n\n"
            "Portable, read-only projections. The manifest preserves IDs and version relationships.\n\n"
            "- [Brief](BRIEF.md)\n- [Reports](REPORT.md)\n- [Questions](QUESTIONS.md)\n"
            "- [Sources](SOURCES.md)\n- [Changelog](CHANGELOG.md)\n\n"
            "Pipeline revisions and source-watch configuration are included in manifest.json. "
            "Restoring this archive does not activate schedules or notifications.\n",
            "BRIEF.md": f"# {proj.name}\n\n{proj.description}\n",
            "REPORT.md": "# Published reports\n\n" + "\n\n".join(
                f"## {doc['title']} (v{doc['version']})\n\n"
                f"Document: {doc['id']}\n\n{doc['content']}"
                for doc in resources["documents"] if doc["kind"] == "report"
            ),
            "QUESTIONS.md": "# Questions\n\n" + "\n\n".join(
                f"## {q['text']}\n\nStatus: {q['status']} · ID: {q['id']}\n\n{q['answer']}"
                for q in resources["questions"]
            ),
            "SOURCES.md": "# Sources\n\n" + "\n\n".join(
                f"## {source['title'] or source['id']}\n\n"
                f"ID: {source['id']}\n\nLocator: {source['canonical_uri']}\n\n"
                f"Classification: {source['classification']}"
                for source in resources["sources"]
            ),
            "CHANGELOG.md": "# Saved document revisions\n\n" + "\n".join(
                f"- {v['created_at']}: {v['title']} v{v['version']} "
                f"(document {v['document_id']}; revision {v['id']})"
                for v in sorted(resources["document_versions"], key=lambda row: row["created_at"])
            ) + "\n",
        }
        for name, content in projections.items():
            z.writestr(name, content)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr(
            "README.txt",
            "Research Workspace portable export. manifest.json contains resource relationships and immutable citation anchors. raw/ objects are SHA-256 addressed. documents/ contains current text. Account/provider credentials and notification endpoints are excluded. Research content may itself be confidential; treat this archive as private. This is a portable project export, not a complete deployment backup or automatic import.\n",
        )
    return archive.getvalue()


def document_read_range(result, *, start=None, limit=8000, block_id=None):
    """Project a bounded reading window without changing canonical evidence coordinates."""
    text = result["content"]
    total = len(text)
    lower, upper = 0, total
    blocks = result["blocks"]
    if block_id is not None:
        selected = next((block for block in blocks if block["id"] == block_id), None)
        if selected is None:
            raise DomainError("Block not found in selected document version", 404)
        lower, upper = selected["start_offset"], selected["end_offset"]
        blocks = [selected]
    start = lower if start is None else start
    if start < lower or start > upper or limit < 1 or limit > 32000:
        raise DomainError("Reading range is outside the selected document or block", 422)
    end = min(start + limit, upper)
    visible = []
    for block in blocks:
        left, right = max(start, block["start_offset"]), min(end, block["end_offset"])
        if left >= right:
            continue
        local_start, local_end = left - block["start_offset"], right - block["start_offset"]
        clipped = block["text"][local_start:local_end]
        visible.append({
            **{key: value for key, value in block.items() if key != "search_text"},
            "text": clipped,
            "canonical_content_hash": block["content_hash"],
            "content_hash": digest(clipped),
            "text_range": {
                "block_start_offset": local_start,
                "block_end_offset": local_end,
                "document_start_offset": left,
                "document_end_offset": right,
                "truncated": local_start != 0 or local_end != len(block["text"]),
            },
        })
    return {
        **result,
        "content": text[start:end],
        "blocks": visible,
        "reading_range": {
            "start_offset": start,
            "end_offset": end,
            "total_length": total,
            "scope_start_offset": lower,
            "scope_end_offset": upper,
            "next_offset": end if end < upper else None,
            "has_more": end < upper,
            "offset_unit": "unicode_code_points",
        },
    }
