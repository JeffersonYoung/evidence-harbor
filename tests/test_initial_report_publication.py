"""The first report publication uses a project-level compare-and-swap anchor."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from backend import domain
from backend import models as m
from tests.test_api_workflow import checked, evidence, ingestion, project
from tests.test_domain_integrity import evidence_for, ingest_text, make_project
from tests.test_postgres_domain import pg_runtime  # noqa: F401 -- shared isolated PostgreSQL fixture


def test_two_initial_drafts_stay_creatable_but_only_first_publishes(client, runtime):
    proj = project(client)
    source = ingestion(client, proj["id"])
    anchor = evidence(client, proj["id"], source)
    draft = {
        "project_id": proj["id"],
        "title": "Main report",
        "content": anchor["quote"],
        "claims": [{"text": anchor["quote"], "evidence_ids": [anchor["id"]]}],
    }
    first = checked(client.post("/v1/proposals", json=draft), 201)
    second = checked(client.post("/v1/proposals", json=draft), 201)
    assert first["target_document_id"] is None and second["target_document_id"] is None
    published = checked(client.post(f"/v1/proposals/{first['id']}/publish", json={}))
    refused = client.post(f"/v1/proposals/{second['id']}/publish", json={})
    assert refused.status_code == 409
    assert "target_document_id" in refused.json()["detail"]
    assert "base_version" in refused.json()["detail"]
    assert checked(client.get(f"/v1/proposals/{second['id']}"))["status"] == "pending"
    assert checked(client.post(f"/v1/proposals/{first['id']}/publish", json={}))["id"] == published["id"]
    with runtime.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(m.Document).where(m.Document.kind == "report"))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.DocumentVersion)
                .where(m.DocumentVersion.document_id == published["id"])
            )
            == 1
        )
    # An explicit rebase remains the normal, supported way to publish the next revision.
    rebased = checked(
        client.post(
            "/v1/proposals", json={**draft, "target_document_id": published["id"], "base_version": 1}
        ),
        201,
    )
    revised = checked(client.post(f"/v1/proposals/{rebased['id']}/publish", json={"expected_version": 1}))
    assert revised["id"] == published["id"] and revised["version"] == 2


@pytest.mark.skipif(not os.getenv("EVIDENCEHARBOR_TEST_POSTGRES"), reason="Requires real PostgreSQL")
def test_postgresql_concurrent_initial_publications_have_exactly_one_winner(pg_runtime):  # noqa: F811 -- pytest fixture
    project_id = make_project(pg_runtime)
    saved = ingest_text(pg_runtime, project_id)
    anchor_id = evidence_for(pg_runtime, project_id, saved)
    claim = "Policy evidence records measurable results."
    with pg_runtime.session_factory() as session:
        proposals = [
            domain.create_proposal(
                session,
                "workspace-a",
                project_id,
                f"Candidate {i}",
                claim,
                [{"text": claim, "evidence_ids": [anchor_id]}],
            )
            for i in range(2)
        ]
        session.commit()
        identifiers = [item.id for item in proposals]
    start = Barrier(2)

    def publish(identifier):
        with pg_runtime.session_factory() as session:
            start.wait(timeout=10)
            try:
                doc = domain.publish_proposal(session, "workspace-a", identifier)
                session.commit()
                return 200, doc.id
            except domain.DomainError as exc:
                session.rollback()
                return exc.status_code, None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(publish, identifiers))
    assert sorted(code for code, _ in results) == [200, 409]
    with pg_runtime.session_factory() as session:
        report = session.scalar(
            select(m.Document).where(m.Document.project_id == project_id, m.Document.kind == "report")
        )
        assert report.id == next(identifier for code, identifier in results if code == 200)
        assert report.version == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.Document)
                .where(m.Document.project_id == project_id, m.Document.kind == "report")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.DocumentVersion)
                .where(m.DocumentVersion.document_id == report.id)
            )
            == 1
        )
        assert sorted(session.get(m.Proposal, identifier).status for identifier in identifiers) == [
            "pending",
            "published",
        ]
