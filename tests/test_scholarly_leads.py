"""Bibliographic intake cannot masquerade as archived, reviewed full-text reading."""

from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from backend import models as m
from tests.test_api_workflow import checked, evidence, ingestion, project

SOURCE_URL = "https://example.org/scholarly-fixture"


def batch(client, project_id, items, *, headers=None):
    response = client.post('/v1/discovered-works/batch', headers=headers,
                           json={"project_id": project_id, "items": items})
    assert response.status_code in {200, 201}, response.text
    return response.json()


def work(title="A scholarly evidence study", **overrides):
    return {"title": title, "authors": ["Ada Researcher"], "source_url": SOURCE_URL, **overrides}


def detail(client, work_id, *, headers=None):
    return checked(client.get(f'/v1/discovered-works/{work_id}', headers=headers))


def list_works(client, project_id, **params):
    return checked(client.get('/v1/discovered-works', params={"project_id": project_id, **params}))


def assert_unread(item, scope="metadata_only"):
    assert item["content_scope"] == scope
    assert item["fulltext_ready"] is False
    assert item["evidence_eligible"] is False
    assert item["fulltext_read"] is False


def scoped_ingestion(client, project_id, scope, *, source_url=SOURCE_URL):
    payload = {
        "project_id": project_id, "type": "text", "title": f"Saved {scope} source",
        "text": "This study reports an observed association. Its methods and limitations require careful review.",
        "original_url": source_url,
    }
    if scope is not None:
        payload["content_scope"] = scope
    submitted = checked(client.post('/v1/ingestions', json=payload), 202)
    result = checked(client.get(f'/v1/operations/{submitted["id"]}'))
    assert result["status"] == "succeeded", result
    return result


def link_payload(document_id, *, scope="fulltext", reviewed=True, note="Reviewed the saved original source."):
    return {"document_id": document_id, "content_scope": scope,
            "fulltext_reviewed": reviewed, "review_note": note}


def link(client, work_id, payload, *, headers=None):
    response = client.post(f'/v1/discovered-works/{work_id}/readings', json=payload, headers=headers)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def test_metadata_and_abstract_intake_create_no_archived_sources_or_evidence(client, runtime):
    proj = project(client)
    result = batch(client, proj["id"], [
        work("Metadata only", doi="10.1234/metadata", access_status="open_access", metadata={
            "content_scope": "fulltext", "fulltext_reviewed": True,
            "fulltext_ready": True, "evidence_eligible": True,
        }),
        work("Abstract only", abstract="Abstract text describes an observed association.",
             access_status="restricted", metadata={"papers_read": 1000}),
    ])
    assert result["created"] == 2 and result["deduplicated"] == 0
    assert_unread(result["items"][0])
    assert_unread(result["items"][1], "abstract")
    for item in result["items"]:
        saved = detail(client, item["id"])
        assert len(saved["observations"]) == 1
        assert saved["readings"] == []
    with runtime.session_factory() as session:
        for model in (m.Source, m.Capture, m.Representation, m.Document, m.Evidence, m.Operation):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_one_thousand_abstracts_remain_unread_and_paginate_without_missing_or_duplicate_records(client, runtime):
    proj = project(client, "A thousand leads are not a thousand papers read")
    identities = set()
    for start in range(0, 1000, 100):
        result = batch(client, proj["id"], [
            work(f"Distinct study {index:04d}", doi=f"10.1234/study-{index:04d}",
                 abstract="Only this abstract was supplied; the full paper has not been saved or reviewed.",
                 metadata={"fulltext_reviewed": True, "papers_read": 1000})
            for index in range(start, start + 100)
        ])
        assert result["created"] == 100 and result["deduplicated"] == 0
        for item in result["items"]:
            assert_unread(item, "abstract")
            identities.add(item["id"])
    assert len(identities) == 1000
    listed = []
    for offset in range(0, 1000, 100):
        page = list_works(client, proj["id"], offset=offset, limit=100)
        assert page["total"] == 1000 and len(page["items"]) == 100
        for item in page["items"]:
            assert_unread(item, "abstract")
            listed.append(item["id"])
    assert len(listed) == len(set(listed)) == 1000
    assert set(listed) == identities
    assert list_works(client, proj["id"], offset=1000, limit=100)["items"] == []
    with runtime.session_factory() as session:
        for model in (m.Capture, m.Document, m.Evidence):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_doi_alias_normalization_merges_title_variants_without_rewriting_observation_history(client):
    proj = project(client)
    first = batch(client, proj["id"], [work("Original supplied title", doi="https://doi.org/10.1234/ABC.1")])
    work_id = first["items"][0]["id"]
    before = detail(client, work_id)
    original_observation = before["observations"][0]
    merged = batch(client, proj["id"], [
        work("Revised bibliographic title", doi="10.1234/abc.1", abstract="New abstract metadata.")
    ])
    assert merged["created"] == 0 and merged["deduplicated"] == 1
    assert merged["items"][0]["id"] == work_id
    after = detail(client, work_id)
    assert len(after["observations"]) == 2
    assert original_observation in after["observations"]
    assert after["aliases"]
    assert list_works(client, proj["id"])["total"] == 1
    assert_unread(after, "abstract")


def test_arxiv_versions_and_title_first_author_fallback_deduplicate(client):
    proj = project(client)
    result = batch(client, proj["id"], [
        work("Preprint first title", arxiv_id="2301.01234v1"),
        work("Preprint revised title", arxiv_id="2301.01234v3"),
        work("Identifier-free study", authors=["Casey Scholar", "Different Coauthor"]),
        work("Identifier-free study", authors=["Casey Scholar", "Another Coauthor"]),
    ])
    assert result["created"] == 2 and result["deduplicated"] == 2
    assert result["items"][0]["id"] == result["items"][1]["id"]
    assert result["items"][2]["id"] == result["items"][3]["id"]
    assert result["items"][0]["id"] != result["items"][2]["id"]


def test_conflicting_dois_do_not_merge_via_matching_title_and_author(client):
    proj = project(client)
    first = batch(client, proj["id"], [work("Ambiguous shared title", doi="10.1234/one")])["items"][0]
    before = detail(client, first["id"])
    response = client.post('/v1/discovered-works/batch', json={"project_id": proj["id"], "items": [
        work("Ambiguous shared title", doi="10.1234/two"),
    ]})
    assert response.status_code == 409, response.text
    assert list_works(client, proj["id"])["total"] == 1
    assert detail(client, first["id"]) == before


def test_strong_alias_collision_rolls_back_the_entire_batch(client):
    proj = project(client)
    initial = batch(client, proj["id"], [
        work("DOI record", doi="10.1234/existing"),
        work("Separate arXiv record", arxiv_id="2302.01234v1"),
    ])
    snapshots = {item["id"]: detail(client, item["id"]) for item in initial["items"]}
    response = client.post('/v1/discovered-works/batch', json={"project_id": proj["id"], "items": [
        work("Must roll back", doi="10.1234/rolled-back"),
        work("Conflicting bridge", doi="10.1234/existing", arxiv_id="2302.01234v2"),
    ]})
    assert response.status_code == 409, response.text
    assert list_works(client, proj["id"])["total"] == 2
    for work_id, snapshot in snapshots.items():
        assert detail(client, work_id) == snapshot


def test_replaying_identical_metadata_reuses_record_and_observation(client):
    proj = project(client)
    item = work(doi="doi:10.1234/replay", metadata={"source": {"provider": "fixture", "rank": 3}})
    first = batch(client, proj["id"], [item])
    before = detail(client, first["items"][0]["id"])
    replay = batch(client, proj["id"], [item])
    assert replay["created"] == 0 and replay["deduplicated"] == 1
    assert replay["items"][0]["id"] == first["items"][0]["id"]
    assert detail(client, first["items"][0]["id"])["observations"] == before["observations"]


def test_lead_identity_is_project_scoped_and_workspace_authorized(client):
    first, second = project(client), project(client, "Other project")
    first_work = batch(client, first["id"], [work(doi="10.1234/scoped")])["items"][0]
    second_work = batch(client, second["id"], [work(doi="10.1234/scoped")])["items"][0]
    assert first_work["id"] != second_work["id"]
    other = {"Authorization": "Bearer owner-b", "X-Workspace-ID": "workspace-a"}
    assert client.get(f'/v1/discovered-works/{first_work["id"]}', headers=other).status_code == 404
    assert client.get('/v1/discovered-works', headers=other,
                      params={"project_id": first["id"]}).status_code == 404
    assert client.post('/v1/discovered-works/batch', headers=other,
                       json={"project_id": first["id"], "items": [work()]}).status_code == 404
    assert client.post(f'/v1/discovered-works/{first_work["id"]}/readings', headers=other,
                       json=link_payload(first_work["id"])).status_code == 404


def test_reader_can_inspect_leads_but_cannot_import_or_attach_readings(client, runtime):
    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    source = ingestion(client, proj["id"])
    runtime.settings.api_tokens["reader-a"] = {"workspace_id": "workspace-a", "role": "reader"}
    headers = {"Authorization": "Bearer reader-a"}
    assert client.get(f'/v1/discovered-works/{lead["id"]}', headers=headers).status_code == 200
    assert client.post('/v1/discovered-works/batch', headers=headers,
                       json={"project_id": proj["id"], "items": [work("Unauthorized")]}).status_code == 403
    assert client.post(f'/v1/discovered-works/{lead["id"]}/readings', headers=headers,
                       json=link_payload(source["result_json"]["document_id"])).status_code == 403


def test_reviewed_fulltext_link_pins_saved_resources_and_is_idempotent(client):
    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    source = scoped_ingestion(client, proj["id"], "fulltext")
    saved = source["result_json"]
    payload = link_payload(saved["document_id"])
    first = link(client, lead["id"], payload)
    again = link(client, lead["id"], payload)
    assert again["id"] == first["id"]
    for key in ("document_id", "representation_id", "capture_id"):
        assert first[key] == saved[key]
    current = detail(client, lead["id"])
    assert len(current["readings"]) == 1
    assert current["content_scope"] == "fulltext"
    assert current["fulltext_ready"] is True and current["evidence_eligible"] is True
    assert current["fulltext_read"] is False
    assert checked(client.get(f'/v1/projects/{proj["id"]}'))["evidence"] == []
    reprocess = checked(client.post(f'/v1/captures/{saved["capture_id"]}/reprocess', json={}), 202)
    assert checked(client.get(f'/v1/operations/{reprocess["id"]}'))["status"] == "succeeded"
    assert detail(client, lead["id"])["readings"] == current["readings"]


@pytest.mark.parametrize("overrides", [
    {"fulltext_reviewed": False}, {"review_note": "no"},
])
def test_fulltext_link_requires_explicit_review_and_explanation(client, overrides):
    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    source = scoped_ingestion(client, proj["id"], None)
    payload = {**link_payload(source["result_json"]["document_id"]), **overrides}
    response = client.post(f'/v1/discovered-works/{lead["id"]}/readings', json=payload)
    assert response.status_code == 422, response.text
    assert detail(client, lead["id"])["readings"] == []
    assert_unread(detail(client, lead["id"]))


def test_known_abstract_cannot_be_relabelled_as_reviewed_fulltext(client):
    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    source = scoped_ingestion(client, proj["id"], "abstract")
    saved = source["result_json"]
    capture = checked(client.get(f'/v1/captures/{saved["capture_id"]}'))
    assert capture["metadata_json"]["content_scope"] == "abstract"
    denied = client.post(f'/v1/discovered-works/{lead["id"]}/readings',
                         json=link_payload(saved["document_id"]))
    assert denied.status_code == 422, denied.text
    assert_unread(detail(client, lead["id"]))
    link(client, lead["id"], link_payload(saved["document_id"], scope="abstract", reviewed=False))
    result = detail(client, lead["id"])
    assert result["content_scope"] == "abstract"
    assert result["fulltext_ready"] is False and result["evidence_eligible"] is True
    reprocess = checked(client.post(f'/v1/captures/{saved["capture_id"]}/reprocess', json={}), 202)
    newer = checked(client.get(f'/v1/operations/{reprocess["id"]}'))
    assert newer["status"] == "succeeded"
    assert client.post(f'/v1/discovered-works/{lead["id"]}/readings',
        json=link_payload(newer["result_json"]["document_id"])).status_code == 422


def test_abstract_evidence_requires_explicit_abstract_claim_scope(client):
    proj = project(client)
    source = scoped_ingestion(client, proj["id"], "abstract")
    quote = "This study reports an observed association."
    anchor = evidence(client, proj["id"], source, quote=quote)
    assert anchor["locator_json"]["content_scope"] == "abstract"
    payload = {"project_id": proj["id"], "title": "Abstract-only claim", "content": quote,
               "claims": [{"text": quote, "evidence_ids": [anchor["id"]]}]}
    assert client.post('/v1/proposals', json=payload).status_code == 422
    payload["claims"][0]["scope"] = "fulltext"
    assert client.post('/v1/proposals', json=payload).status_code == 422
    payload["claims"][0]["scope"] = "abstract"
    proposal = checked(client.post('/v1/proposals', json=payload), 201)
    assert proposal["claims"][0]["scope"] == "abstract"
    published = checked(client.post(f'/v1/proposals/{proposal["id"]}/publish', json={}))
    assert published["kind"] == "report"


def test_metadata_only_ingestion_is_rejected_and_unspecified_source_stays_unspecified(client):
    proj = project(client)
    response = client.post('/v1/ingestions', json={"project_id": proj["id"], "type": "text",
        "text": "Bibliographic title and metadata only.", "content_scope": "metadata_only"})
    assert response.status_code == 422
    source = scoped_ingestion(client, proj["id"], None)
    capture = checked(client.get(f'/v1/captures/{source["result_json"]["capture_id"]}'))
    assert capture["metadata_json"]["content_scope"] == "unspecified"


def test_reading_links_reject_cross_project_and_report_documents(client):
    proj, other = project(client), project(client, "Separate research")
    lead = batch(client, proj["id"], [work()])["items"][0]
    unrelated = ingestion(client, other["id"])
    assert client.post(f'/v1/discovered-works/{lead["id"]}/readings',
        json=link_payload(unrelated["result_json"]["document_id"])).status_code == 404
    source = ingestion(client, proj["id"])
    anchor = evidence(client, proj["id"], source)
    proposal = checked(client.post('/v1/proposals', json={"project_id": proj["id"], "title": "Derived report",
        "content": anchor["quote"], "claims": [{"text": anchor["quote"], "evidence_ids": [anchor["id"]]}]}), 201)
    report = checked(client.post(f'/v1/proposals/{proposal["id"]}/publish', json={}))
    assert client.post(f'/v1/discovered-works/{lead["id"]}/readings',
        json=link_payload(report["id"])).status_code == 422
    assert detail(client, lead["id"])["readings"] == []


def test_corrupt_saved_blob_cannot_be_approved_as_read_fulltext(client, runtime):
    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    source = scoped_ingestion(client, proj["id"], "fulltext")
    with runtime.session_factory() as session:
        capture = session.get(m.Capture, source["result_json"]["capture_id"])
        path = Path(capture.blob_path)
        path.chmod(0o600)
        path.write_bytes(b"Corrupted original bytes")
    response = client.post(f'/v1/discovered-works/{lead["id"]}/readings',
                           json=link_payload(source["result_json"]["document_id"]))
    assert response.status_code == 422, response.text
    assert detail(client, lead["id"])["readings"] == []
    assert_unread(detail(client, lead["id"]))


def test_reading_link_requires_source_provenance_matching_the_scholarly_work(client):
    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    unrelated = scoped_ingestion(client, proj["id"], "fulltext", source_url="https://example.org/unrelated-paper")
    response = client.post(f'/v1/discovered-works/{lead["id"]}/readings',
                           json=link_payload(unrelated["result_json"]["document_id"]))
    assert response.status_code == 422, response.text
    assert detail(client, lead["id"])["readings"] == []
    assert_unread(detail(client, lead["id"]))


def test_explicit_observation_source_locator_can_match_a_saved_fulltext_artifact(client):
    proj = project(client)
    locator = "https://example.org/saved-paper-fulltext"
    lead = batch(client, proj["id"], [work(metadata={"source_locators": [locator]})])["items"][0]
    source = scoped_ingestion(client, proj["id"], "fulltext", source_url=locator)
    linked = link(client, lead["id"], link_payload(source["result_json"]["document_id"]))
    assert linked["capture_id"] == source["result_json"]["capture_id"]
    current = detail(client, lead["id"])
    assert current["fulltext_ready"] is True and current["fulltext_read"] is False


@pytest.mark.parametrize("model_name", ["WorkAlias", "WorkObservation", "WorkReading"])
@pytest.mark.parametrize("action", ["update", "delete"])
def test_database_rejects_mutation_of_scholarly_identity_history_and_reading_links(client, runtime, model_name, action):
    from backend import scholarly

    proj = project(client)
    lead = batch(client, proj["id"], [work()])["items"][0]
    source = scoped_ingestion(client, proj["id"], "fulltext")
    link(client, lead["id"], link_payload(source["result_json"]["document_id"]))
    before = detail(client, lead["id"])
    model = getattr(scholarly, model_name)
    with runtime.session_factory() as session:
        record = session.scalar(select(model).where(model.work_id == lead["id"]))
        assert record is not None
        if action == "update":
            statement = update(model).where(model.id == record.id).values(workspace_id="tampered")
        else:
            statement = delete(model).where(model.id == record.id)
        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(statement)
            session.commit()
        session.rollback()
    assert detail(client, lead["id"]) == before
