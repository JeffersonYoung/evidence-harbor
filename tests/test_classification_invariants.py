"""Source identity can never silently inherit a different sharing classification."""

import pytest
from sqlalchemy import func, select

from backend import models as m
from tests.test_api_workflow import checked, project


@pytest.mark.parametrize(
    "original,incoming", [("public", None), ("internal", "public"), ("sensitive", "public")]
)
@pytest.mark.parametrize("change_bytes", [False, True])
def test_same_source_identity_rejects_classification_change(
    client, runtime, original, incoming, change_bytes
):
    proj = project(client)
    uri = "https://example.org/classified-report"
    original_bytes = b"Original evidence under the declared sharing classification."

    def upload(raw, classification):
        data = {"project_id": proj["id"], "original_url": uri}
        if classification is not None:
            data["classification"] = classification
        queued = checked(
            client.post(
                "/v1/ingestions/upload", data=data, files={"file": ("report.txt", raw, "text/plain")}
            ),
            202,
        )
        return checked(client.get(f"/v1/operations/{queued['id']}"))

    initial = upload(original_bytes, original)
    assert initial["status"] == "succeeded", initial
    source_id = initial["result_json"]["source_id"]
    initial_capture_id = initial["result_json"]["capture_id"]
    incoming_bytes = (
        b"New bytes that must not inherit the earlier classification." if change_bytes else original_bytes
    )
    refused = upload(incoming_bytes, incoming)
    assert refused["status"] == "failed"
    assert "classification is immutable" in refused["error"]
    assert "access scope or representation variant" in refused["error"]
    assert refused["result_json"] is None
    source = checked(client.get(f"/v1/sources/{source_id}"))
    assert source["classification"] == original
    captures = checked(client.get(f"/v1/sources/{source_id}/captures"))
    assert [item["id"] for item in captures] == [initial_capture_id]
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.Source)) == 1
        assert session.scalar(select(func.count()).select_from(m.Representation)) == 1
        assert session.scalar(select(func.count()).select_from(m.Document)) == 1


def test_reprocessing_keeps_internal_classification_and_cannot_override_it(client):
    proj = project(client)
    queued = checked(
        client.post(
            "/v1/ingestions",
            json={
                "project_id": proj["id"],
                "type": "text",
                "text": "Private research evidence stays internal.",
            },
        ),
        202,
    )
    initial = checked(client.get(f"/v1/operations/{queued['id']}"))
    assert initial["status"] == "succeeded"
    capture_id = initial["result_json"]["capture_id"]
    assert (
        client.post(f"/v1/captures/{capture_id}/reprocess", json={"classification": "public"}).status_code
        == 422
    )
    replay = checked(client.post(f"/v1/captures/{capture_id}/reprocess", json={}), 202)
    completed = checked(client.get(f"/v1/operations/{replay['id']}"))
    assert completed["status"] == "succeeded"
    source = checked(client.get(f"/v1/sources/{initial['result_json']['source_id']}"))
    assert source["classification"] == "internal"
