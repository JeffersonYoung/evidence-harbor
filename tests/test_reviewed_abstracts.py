"""Synthetic reviewed abstracts stay metadata, with immutable provider provenance."""

import io
import json
import zipfile
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from backend import models as m
from backend.scholarly import DiscoveredWork, WorkMetadataReview, WorkObservation
from tests.helpers import digest
from tests.test_api_workflow import checked, evidence, ingestion, project
from tests.test_scholarly_leads import assert_unread, batch, detail, work
from tests.test_scholarly_mcp_transport import (
    EDITOR,
    RESEARCHER,
    call_error,
    call_json,
    direct_agent_get,
    mcp_session,
    scholarly_http_bridge,  # noqa: F401 - shared real HTTP/stdio fixture
)


def abstract_review(abstract, *, revision=0, **changes):
    return {
        "expected_revision": revision,
        "changes": {"abstract": abstract, **changes},
        "reason": (
            "Compared this synthetic abstract with the fixture preprint. "
            "Equivalence to a published journal version has not been verified."
        ),
        "source_url": "https://example.org/reviewed-abstract-fixture",
    }


def assert_metadata_abstract(record, *, abstract_revision):
    assert_unread(record, "abstract")
    assert record["abstract_display_scope"] == "abstract_metadata"
    assert record["abstract_evidence_eligible"] is False
    assert record["abstract_reviewed"] is (abstract_revision > 0)
    assert record["abstract_review_revision"] == abstract_revision


def archived_counts(runtime):
    with runtime.session_factory() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (m.Source, m.Capture, m.Representation, m.Document, m.Evidence, m.Operation)
        }


def test_reviewed_abstract_overlay_survives_intakes_and_exports_without_rewriting_provenance(client, runtime):
    proj = project(client)
    original_text = "Provider fixture contains a transcription mistake."
    supplied = work(doi="10.1234/reviewed-abstract", abstract=original_text)
    lead = batch(client, proj["id"], [supplied])["items"][0]
    before = detail(client, lead["id"])
    assert_metadata_abstract(before, abstract_revision=0)
    path = f'/v1/discovered-works/{lead["id"]}/metadata-reviews'
    corrected_text = "Reviewed synthetic abstract describes an association, with explicit limitations."
    current = checked(client.post(path, json=abstract_review(corrected_text)), 201)
    assert current["abstract"] == corrected_text
    assert current["provider_display"]["abstract"] == original_text
    assert current["observations"] == before["observations"]
    assert current["metadata_reviews"][0]["overrides_json"]["abstract"] == corrected_text
    assert current["abstract_review"] == {
        key: current["metadata_reviews"][0][key]
        for key in ("id", "revision", "reason", "source_url", "evidence_ids")
    }
    assert "has not been verified" in current["abstract_review"]["reason"]
    assert_metadata_abstract(current, abstract_revision=1)

    # Both provider intake paths can append history but cannot replace reviewed display text.
    replay = batch(client, proj["id"], [{**supplied, "abstract": "Later provider abstract fixture."}])
    assert replay["deduplicated"] == 1
    checked(client.post(f'/v1/discovered-works/{lead["id"]}/observations', json={
        **supplied, "abstract": "Another provider observation fixture.",
    }), 201)
    current = detail(client, lead["id"])
    assert current["abstract"] == corrected_text
    assert current["provider_display"]["abstract"] == original_text
    assert before["observations"][0] in current["observations"]
    assert {row["payload"]["abstract"] for row in current["observations"]} == {
        original_text, "Later provider abstract fixture.", "Another provider observation fixture.",
    }

    # Later non-abstract reviews and repeated identical content do not move the abstract revision.
    title_review = abstract_review(corrected_text, revision=1)
    title_review["changes"] = {"title": "Reviewed synthetic title"}
    current = checked(client.post(path, json=title_review), 201)
    assert current["review_revision"] == 2
    assert_metadata_abstract(current, abstract_revision=1)
    assert current["abstract_review"]["revision"] == 1
    current = checked(client.post(path, json=abstract_review(corrected_text, revision=2)), 201)
    assert current["review_revision"] == 3
    assert_metadata_abstract(current, abstract_revision=1)
    assert current["abstract_review"]["revision"] == 1
    final_text = corrected_text + " Updated interpretation remains metadata."
    current = checked(client.post(path, json=abstract_review(final_text, revision=3)), 201)
    assert_metadata_abstract(current, abstract_revision=4)
    listing = checked(client.get('/v1/discovered-works', params={"project_id": proj["id"]}))
    assert listing["items"][0]["abstract"] == final_text
    assert listing["items"][0]["provider_display"]["abstract"] == original_text
    assert_metadata_abstract(listing["items"][0], abstract_revision=4)
    exported = client.get(f'/v1/projects/{proj["id"]}/export')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    exported_work = manifest["resources"]["discovered_works"][0]
    assert exported_work["abstract"] == final_text
    assert exported_work["provider_display"]["abstract"] == original_text
    assert_metadata_abstract(exported_work, abstract_revision=4)
    assert len(manifest["resources"]["scholarly_metadata_reviews"]) == 4
    assert all(value == 0 for value in archived_counts(runtime).values())
    with runtime.session_factory() as session:
        assert session.get(DiscoveredWork, lead["id"]).abstract == original_text
        for model in (WorkObservation, WorkMetadataReview):
            column, value = ("payload", {}) if model is WorkObservation else ("reason", "Replace review")
            with pytest.raises(DBAPIError):
                session.execute(update(model).where(model.work_id == lead["id"]).values({column: value}))
                session.commit()
            session.rollback()


def test_abstract_review_requires_editor_cas_nonempty_bounded_text_and_support(client, runtime):
    proj = project(client)
    lead = batch(client, proj["id"], [work(abstract="Original fixture abstract.")])["items"][0]
    path = f'/v1/discovered-works/{lead["id"]}/metadata-reviews'
    payload = abstract_review("Replacement fixture abstract.")
    for role in ("reader", "researcher", "editor"):
        runtime.settings.api_tokens[f"abstract-{role}"] = {"workspace_id": "workspace-a", "role": role}
    for role in ("reader", "researcher"):
        assert client.post(path, json=payload, headers={
            "Authorization": f"Bearer abstract-{role}",
        }).status_code == 403
    assert client.post(path, json=payload, headers={"Authorization": "Bearer owner-b"}).status_code == 404
    invalid = [abstract_review(value) for value in ("", " \n\t ", None, "字" * 100001)]
    unsupported = deepcopy(payload)
    unsupported.pop("source_url")
    invalid.append(unsupported)
    for forbidden in ("fulltext_ready", "fulltext_read", "evidence_eligible"):
        fabricated = deepcopy(payload)
        fabricated["changes"][forbidden] = True
        invalid.append(fabricated)
    for candidate in invalid:
        response = client.post(path, json=candidate)
        assert response.status_code == 422, response.text
    assert detail(client, lead["id"])["review_revision"] == 0
    maximum = abstract_review("字" * 100000)
    accepted = checked(client.post(path, json=maximum, headers={
        "Authorization": "Bearer abstract-editor",
    }), 201)
    assert accepted["abstract"] == maximum["changes"]["abstract"]
    assert accepted["metadata_reviews"][0]["actor_json"]["role"] == "editor"
    assert client.post(path, json=payload).status_code == 409
    assert detail(client, lead["id"])["review_revision"] == 1
    assert all(value == 0 for value in archived_counts(runtime).values())


def test_unicode_agent_windows_use_reviewed_abstract_and_exact_canonical_and_slice_hashes(client, runtime):
    proj = project(client)
    # Start without an abstract so a correction must drive scope and range validation too.
    lead = batch(client, proj["id"], [work(doi="10.1234/abstract-unicode")])["items"][0]
    original = detail(client, lead["id"])
    assert original["abstract_reviewed"] is False and original["abstract_review_revision"] == 0
    corrected = "🧪𠮷é e\u0301 世界. " * 251 + "UNREAD_FIXTURE_TAIL"
    checked(client.post(f'/v1/discovered-works/{lead["id"]}/metadata-reviews',
                        json=abstract_review(corrected)), 201)
    path = f'/v1/discovered-works/{lead["id"]}'
    parts, start = [], 0
    while start < len(corrected):
        page = checked(client.get(path, params={
            "view": "agent", "abstract_start": start, "abstract_limit": 137, "limit": 1,
        }))
        end = min(start + 137, len(corrected))
        assert page["abstract"] == corrected[start:end]
        assert page["abstract_length"] == len(corrected)
        assert page["abstract_sha256"] == digest(corrected)
        assert page["abstract_window_sha256"] == digest(corrected[start:end])
        assert page["abstract_range"] == {
            "start_offset": start, "end_offset": end, "total_length": len(corrected),
            "next_offset": end if end < len(corrected) else None,
            "offset_unit": "unicode_code_points",
        }
        assert_metadata_abstract(page, abstract_revision=1)
        assert "provider_display" not in page
        assert all("overrides_json" not in row for row in page["metadata_reviews"])
        if end < corrected.index("UNREAD_FIXTURE_TAIL"):
            assert "UNREAD_FIXTURE_TAIL" not in json.dumps(page, ensure_ascii=False)
        parts.append(page["abstract"])
        start = end
    assert "".join(parts) == corrected
    eof = checked(client.get(path, params={"view": "agent", "abstract_start": len(corrected)}))
    assert eof["abstract"] == "" and eof["abstract_window_sha256"] == digest("")
    assert eof["abstract_range"]["next_offset"] is None
    assert client.get(path, params={"view": "agent", "abstract_start": len(corrected) + 1}).status_code == 422
    listed = checked(client.get('/v1/discovered-works', params={"project_id": proj["id"], "view": "agent"}))
    summary = listed["items"][0]
    assert "abstract" not in summary and "provider_display" not in summary
    assert summary["abstract_length"] == len(corrected)
    assert summary["abstract_sha256"] == digest(corrected)
    assert_metadata_abstract(summary, abstract_revision=1)
    assert all(value == 0 for value in archived_counts(runtime).values())


def test_abstract_review_accepts_verified_evidence_without_url_but_not_foreign_evidence(client, runtime):
    proj, other = project(client), project(client, "Other synthetic evidence project")
    lead = batch(client, proj["id"], [work()])["items"][0]
    local_evidence = evidence(client, proj["id"], ingestion(client, proj["id"]))
    foreign_evidence = evidence(client, other["id"], ingestion(client, other["id"]))
    before_counts = archived_counts(runtime)
    payload = abstract_review("A synthetic abstract checked against an existing source anchor.")
    payload.pop("source_url")
    path = f'/v1/discovered-works/{lead["id"]}/metadata-reviews'
    for evidence_id in (str(uuid4()), foreign_evidence["id"]):
        assert client.post(path, json={**payload, "evidence_ids": [evidence_id]}).status_code == 404
    current = checked(client.post(path, json={**payload, "evidence_ids": [local_evidence["id"]]}), 201)
    assert current["metadata_reviews"][0]["evidence_ids"] == [local_evidence["id"]]
    assert current["metadata_reviews"][0]["source_url"] is None
    assert_metadata_abstract(current, abstract_revision=1)
    assert current["readings"] == []
    assert archived_counts(runtime) == before_counts


async def test_opt_in_stdio_editor_reviews_abstract_and_reads_bounded_canonical_text(
    client, runtime, request,
):
    bridge = request.getfixturevalue("scholarly_http_bridge")
    proj = project(client)
    supplied = work(doi="10.1234/mcp-reviewed-abstract", abstract="Original provider-only abstract fixture.")
    lead = batch(client, proj["id"], [supplied])["items"][0]
    original = detail(client, lead["id"])
    corrected = "Reviewed 🧪𠮷 e\u0301 abstract metadata. " * 20
    args = {"work_id": lead["id"], "review": abstract_review(corrected)}
    async with mcp_session(bridge, RESEARCHER, editor_tools=True) as session:
        await call_error(session, "review_discovered_work_metadata", args, status=403)
    async with mcp_session(bridge, EDITOR, editor_tools=True) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        schema = tools["review_discovered_work_metadata"].inputSchema
        abstract_schema = schema["$defs"]["DisplayMetadataPatch"]["properties"]["abstract"]
        assert any(option.get("maxLength") == 100000 for option in abstract_schema.get("anyOf", [abstract_schema]))
        reviewed = await call_json(session, "review_discovered_work_metadata", args)
        assert_metadata_abstract(reviewed, abstract_revision=1)
        assert "abstract" not in reviewed
        await call_error(session, "review_discovered_work_metadata", args, status=409)
        current = await call_json(session, "read_discovered_work", {
            "work_id": lead["id"], "abstract_start": 9, "abstract_limit": 23,
        })
        assert current == direct_agent_get(
            bridge, f'/v1/discovered-works/{lead["id"]}', view="agent", offset=0, limit=10,
            abstract_start=9, abstract_limit=23,
        )
        assert current["abstract"] == corrected[9:32]
        assert current["abstract_sha256"] == digest(corrected)
        assert current["abstract_window_sha256"] == digest(corrected[9:32])
        assert_metadata_abstract(current, abstract_revision=1)
        review_id = current["metadata_reviews"][0]["id"]
        record = await call_json(session, "read_scholarly_record", {
            "work_id": lead["id"], "record_kind": "metadata_review", "record_id": review_id, "limit": 16000,
        })
        saved = json.loads(record["text"])
        assert saved["overrides_json"]["abstract"] == corrected
        assert saved["actor_json"]["role"] == "editor"
        assert record["record_sha256"] == record["text_sha256"] == digest(record["text"])
    after = detail(client, lead["id"])
    assert after["abstract"] == corrected
    assert after["provider_display"]["abstract"] == supplied["abstract"]
    assert after["observations"] == original["observations"]
    assert all(value == 0 for value in archived_counts(runtime).values())
    review_requests = [request for request in bridge.requests if request["method"] == "POST"]
    assert [(request["authorization"], request["status"]) for request in review_requests] == [
        (f"Bearer {RESEARCHER}", 403), (f"Bearer {EDITOR}", 201), (f"Bearer {EDITOR}", 409),
    ]
