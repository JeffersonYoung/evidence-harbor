"""Transactional source-change impact and bounded, review-only research follow-up.

This module never publishes a proposal or rewrites historical evidence. A change
is semantic only in the narrow, deterministic sense of normalized body equality;
missing quotes and affected claims are review signals, not truth judgments.
"""
from __future__ import annotations

import time
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import JSON, Boolean, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Identified, Scoped


class SourceChange(Identified, Scoped, Base):
    __tablename__ = "source_changes"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"), index=True)
    previous_capture_id: Mapped[str | None] = mapped_column(ForeignKey("captures.id"), nullable=True)
    representation_id: Mapped[str] = mapped_column(ForeignKey("representations.id"))
    previous_representation_id: Mapped[str | None] = mapped_column(ForeignKey("representations.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    bytes_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    body_changed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    processing_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    impact_json: Mapped[dict] = mapped_column(JSON, default=dict)
    question_id: Mapped[str | None] = mapped_column(ForeignKey("questions.id"), nullable=True)
    research_run_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.id"), nullable=True)
    research_operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"), nullable=True)
    research_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


def _signature(representation) -> tuple[str, str | None]:
    return representation.parser, (representation.metadata_json or {}).get("config_hash")


def _result(change: SourceChange) -> dict:
    return {"id": change.id, "source_id": change.source_id, "capture_id": change.capture_id,
            "previous_capture_id": change.previous_capture_id, "kind": change.kind,
            "bytes_changed": change.bytes_changed, "body_changed": change.body_changed,
            "processing_changed": change.processing_changed, "impact": change.impact_json,
            "question_id": change.question_id, "research_run_id": change.research_run_id,
            "research_operation_id": change.research_operation_id,
            "research_blocked_reason": change.research_blocked_reason}


def _affected(session, workspace_id: str, project_id: str, source_id: str, current_representation_id: str,
              current_text: str, *, incomparable: bool) -> dict:
    from . import models as m
    # Every old anchor is retained; look beyond the immediately previous capture so
    # a published claim from an older capture is not missed.
    evidence = list(session.scalars(select(m.Evidence).join(m.Capture, m.Capture.id == m.Evidence.capture_id).join(m.Block, m.Block.id == m.Evidence.block_id).where(
        m.Evidence.workspace_id == workspace_id, m.Evidence.project_id == project_id,
        m.Capture.source_id == source_id, m.Block.representation_id != current_representation_id).order_by(m.Evidence.id).limit(10001)))
    truncated = len(evidence) > 10000
    evidence = evidence[:10000]
    affected = []
    retained = 0
    for item in evidence:
        if incomparable or item.quote not in current_text:
            affected.append({"evidence_id": item.id, "capture_id": item.capture_id,
                             "reason": "processing_changed_not_comparable" if incomparable else "exact_quote_missing_in_new_body"})
        else:
            retained += 1
    affected_ids = [item["evidence_id"] for item in affected]
    claim_ids = []
    if affected_ids:
        # Chunk IN clauses for SQLite's portable parameter limit and deterministic order.
        for offset in range(0, len(affected_ids), 500):
            claim_ids.extend(session.scalars(select(m.ClaimEvidence.claim_id).where(
                m.ClaimEvidence.workspace_id == workspace_id, m.ClaimEvidence.project_id == project_id,
                m.ClaimEvidence.evidence_id.in_(affected_ids[offset:offset + 500]))))
    return {"affected_evidence": affected, "affected_claim_ids": sorted(set(claim_ids)),
            "retained_exact_quote_count": retained, "review_scope_truncated": truncated,
            "comparison": "exact_quote_presence", "historical_anchors_unchanged": True,
            "semantic_entailment_assessed": False}


def after_ingestion(session, operation, result: dict) -> dict | None:
    """Call under the ingestion operation's row lock before its transaction commits.

    Records one change per operation. A configured watch can enqueue one bounded
    research operation when comparable source body text changes. No network I/O.
    """
    from pydantic import ValidationError

    from . import domain
    from . import models as m
    from .scheduling import SourceWatch
    from .schemas import ResearchRunCreate

    if not result.get("capture_id") or not result.get("representation_id"):
        return None
    existing = session.scalar(select(SourceChange).where(SourceChange.operation_id == operation.id,
                                                         SourceChange.workspace_id == operation.workspace_id))
    if existing:
        return _result(existing)
    capture = domain.scoped(session, m.Capture, result["capture_id"], operation.workspace_id, operation.project_id)
    representation = domain.scoped(session, m.Representation, result["representation_id"], operation.workspace_id, operation.project_id)
    source = domain.scoped(session, m.Source, capture.source_id, operation.workspace_id, operation.project_id)
    if representation.capture_id != capture.id:
        raise domain.DomainError("Change analysis capture/representation mismatch", 422)
    reused_capture = result.get("unchanged") is True
    previous = (capture if reused_capture else
                domain.scoped(session, m.Capture, capture.previous_capture_id, operation.workspace_id, operation.project_id)
                if capture.previous_capture_id else None)
    previous_representation = None
    processing_changed = False
    if reused_capture and result.get("representation_changed") is not True:
        # A successful no-change poll reuses the existing capture. Do not compare
        # it to its historical predecessor and repeatedly re-trigger research.
        previous_representation = representation
    elif previous:
        query = select(m.Representation).where(
            m.Representation.workspace_id == operation.workspace_id, m.Representation.project_id == operation.project_id,
            m.Representation.capture_id == previous.id)
        if reused_capture:
            query = query.where(m.Representation.id != representation.id)
        previous_reps = list(session.scalars(query.order_by(m.Representation.created_at.desc(), m.Representation.id)))
        previous_representation = next((item for item in previous_reps if _signature(item) == _signature(representation)), None)
        if previous_representation is None or reused_capture:
            previous_representation = previous_representation or (previous_reps[0] if previous_reps else None)
            processing_changed = True
    byte_changed = bool(previous and previous.content_hash != capture.content_hash)
    body_changed = (previous_representation.content_hash != representation.content_hash) if previous_representation and not processing_changed else None
    if previous is None:
        kind = "initial"
    elif processing_changed:
        kind = "processing_and_bytes_changed" if byte_changed else "processing_changed"
    elif body_changed:
        kind = "body_changed"
    elif byte_changed:
        kind = "bytes_only"
    else:
        kind = "unchanged"
    impact = {"affected_evidence": [], "affected_claim_ids": [], "historical_anchors_unchanged": True}
    if body_changed or processing_changed:
        impact = _affected(session, operation.workspace_id, operation.project_id, source.id,
                           representation.id, representation.text, incomparable=processing_changed)
    impact.update({"current_parser": representation.parser,
                   "current_config_hash": (representation.metadata_json or {}).get("config_hash"),
                   "previous_parser": previous_representation.parser if previous_representation else None,
                   "previous_config_hash": (previous_representation.metadata_json or {}).get("config_hash") if previous_representation else None,
                   "comparable_processing": bool(previous_representation and not processing_changed)})
    change = SourceChange(id=str(uuid5(NAMESPACE_URL, "source-change:" + operation.id)),
                          workspace_id=operation.workspace_id, project_id=operation.project_id,
                          operation_id=operation.id, source_id=source.id, capture_id=capture.id,
                          previous_capture_id=previous.id if previous else None, representation_id=representation.id,
                          previous_representation_id=previous_representation.id if previous_representation else None,
                          kind=kind, bytes_changed=byte_changed, body_changed=body_changed,
                          processing_changed=processing_changed, impact_json=impact)
    session.add(change)
    session.flush()
    if body_changed or processing_changed:
        reason = "source body changed" if body_changed else "processing changed; source body is not comparable"
        question = m.Question(id=str(uuid5(NAMESPACE_URL, "source-change-question:" + operation.id)),
                              workspace_id=operation.workspace_id, project_id=operation.project_id,
                              text=f"Review {source.title or source.canonical_uri}: {reason}. Do the affected claims still hold?",
                              status="open", priority="high" if impact["affected_claim_ids"] else "normal",
                              evidence_ids=[item["evidence_id"] for item in impact["affected_evidence"]],
                              evidence_gaps=[f"Recheck claim {identifier} against the new capture" for identifier in impact["affected_claim_ids"]],
                              recheck_condition=f"Compare capture {capture.id} with {previous.id}; retain historical evidence anchors")
        session.add(question)
        session.flush()
        change.question_id = question.id
    watch_id = (operation.input_json or {}).get("watch_id")
    watch = domain.scoped(session, SourceWatch, watch_id, operation.workspace_id, operation.project_id) if watch_id else None
    if watch and watch.enabled and getattr(watch, "research_on_change", False) and body_changed:
        configured = dict(getattr(watch, "research_config", {}) or {})
        question_text = getattr(watch, "research_question", "") or f"Review changed evidence for {source.title}: {representation.text[:240]}"
        try:
            # Schema validation applies defaults and rejects unknown/bypassing budget fields.
            request = ResearchRunCreate(project_id=operation.project_id, question=question_text, **configured)
            data = request.model_dump(exclude={"project_id", "question", "provider"})
            # Automatic follow-up has tighter maxima than an explicitly initiated run.
            data.update(max_searches=min(data["max_searches"], 3), max_documents=min(data["max_documents"], 10),
                        max_tool_calls=min(data["max_tool_calls"], 20), max_duration_seconds=min(data["max_duration_seconds"], 180),
                        max_cost_usd=min(data["max_cost_usd"], 5.0))
            if request.provider != "local" and data["max_cost_usd"] <= 0:
                change.research_blocked_reason = "External automatic research requires an explicit positive per-run cost budget"
            else:
                run, followup = domain.create_research_run(session, operation.workspace_id, operation.project_id,
                                                            question_text, request.provider, **data)
                change.research_run_id, change.research_operation_id = run.id, followup.id
                domain.run_event(session, run, "source.change.triggered", {"source_change_id": change.id, "capture_id": capture.id,
                                                                         "review_only": True, "question_id": change.question_id})
        except (ValidationError, TypeError) as exc:
            change.research_blocked_reason = "Watch research configuration failed validation: " + type(exc).__name__
    session.flush()
    summary = _result(change)
    domain.emit_event(session, operation.workspace_id, operation.project_id, "source.change.assessed", summary)
    return summary


def discover_sources(question: str, config: dict, *, used_searches: int = 0, used_documents: int = 0) -> dict:
    """Explicit, bounded external discovery returning candidates, never evidence.

    The caller must separately save/fetch/parse candidates before using them as
    evidence. No calls happen unless web_discovery is exactly True and credentials
    are configured. No ambient source text or titles are appended to queries.
    """
    from .providers import ProviderError, get_search_provider
    if config.get("web_discovery") is not True:
        return {"candidates": [], "searches_used": 0, "enabled": False}
    maximum_searches = config.get("max_searches", 1)
    maximum_documents = config.get("max_documents", 8)
    duration = config.get("max_duration_seconds", 60)
    if type(maximum_searches) is not int or not 1 <= maximum_searches <= 10 or type(maximum_documents) is not int or not 1 <= maximum_documents <= 30:
        raise ProviderError("Discovery requires bounded search/document budgets")
    if type(duration) is not int or not 5 <= duration <= 900 or used_searches < 0 or used_documents < 0:
        raise ProviderError("Invalid discovery usage or duration budget")
    remaining_searches = max(0, min(3, maximum_searches - used_searches))
    remaining_documents = max(0, maximum_documents - used_documents)
    if not remaining_searches or not remaining_documents:
        return {"candidates": [], "searches_used": 0, "enabled": True, "budget_exhausted": True}
    queries = config.get("discovery_queries") or [question]
    if not isinstance(queries, list) or not queries or any(not isinstance(query, str) or not query.strip() or len(query) > 2000 for query in queries):
        raise ProviderError("Discovery queries must be explicit, bounded text strings")
    provider = get_search_provider(config.get("search_provider", ""))
    deadline = time.monotonic() + min(duration, 180)
    candidates, seen, calls = [], set(), 0
    for query in queries[:remaining_searches]:
        # The adapter's request limit is 20 seconds; do not start a request that
        # cannot fit in the caller's remaining wall-time budget.
        if deadline - time.monotonic() < 20:
            break
        found = provider.search(query, limit=min(20, remaining_documents - len(candidates)))
        calls += 1
        for item in found:
            if item.url not in seen:
                seen.add(item.url)
                candidates.append(item.to_dict())
                if len(candidates) >= remaining_documents:
                    break
        if len(candidates) >= remaining_documents:
            break
    return {"candidates": candidates, "searches_used": calls, "enabled": True,
            "provider": provider.name, "evidence_ready": False,
            "budget_exhausted": len(candidates) >= remaining_documents or calls >= remaining_searches}
