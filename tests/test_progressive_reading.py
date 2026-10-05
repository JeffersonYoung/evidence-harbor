"""Bounded reads preserve immutable Unicode anchors without returning unread block text."""

import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from backend import mcp_server
from tests.helpers import digest
from tests.test_api_workflow import checked, evidence, ingestion, project


@pytest.fixture
def saved_source(client):
    proj = project(client, "Progressive Unicode reading")
    original = (
        "Opening evidence describes the study design and its explicit limits.\n\n"
        "🧪𠮷é Results: repeated evidence. More context 🧭 then repeated evidence. "
        "UNREADTAILSENTINEL remains outside a short reading window.\n\n"
        "Final paragraph records uncertainty and alternative explanations."
    )
    operation = ingestion(client, proj["id"], original)
    url = f'/v1/documents/{operation["result_json"]["document_id"]}/content'
    full = checked(client.get(url))
    assert len(full["blocks"]) == 3
    return {"project": proj, "operation": operation, "url": url, "full": full}


def assert_window(result, full, start, end, *, block_id=None, stop=None):
    """Compare returned slices against the authoritative, unmodified full response."""
    stop = len(full["content"]) if stop is None else stop
    scope_start = (
        next(block["start_offset"] for block in full["blocks"] if block["id"] == block_id)
        if block_id is not None else 0
    )
    assert result["content"] == full["content"][start:end]
    assert result["reading_range"] == {
        "start_offset": start,
        "end_offset": end,
        "total_length": len(full["content"]),
        "next_offset": end if end < stop else None,
        "has_more": end < stop,
        "offset_unit": "unicode_code_points",
        "scope_start_offset": scope_start,
        "scope_end_offset": stop,
    }
    expected = [
        block
        for block in full["blocks"]
        if start < end
        and block["start_offset"] < end
        and block["end_offset"] > start
        and (block_id is None or block["id"] == block_id)
    ]
    assert [block["id"] for block in result["blocks"]] == [block["id"] for block in expected]
    for actual, canonical in zip(result["blocks"], expected, strict=True):
        lo, hi = max(start, canonical["start_offset"]), min(end, canonical["end_offset"])
        for key in ("id", "representation_id", "start_offset", "end_offset", "locator_json"):
            assert actual[key] == canonical[key]
        assert actual["text"] == full["content"][lo:hi]
        assert actual["content_hash"] == digest(actual["text"])
        assert actual["canonical_content_hash"] == canonical["content_hash"]
        assert actual["text_range"] == {
            "block_start_offset": lo - canonical["start_offset"],
            "block_end_offset": hi - canonical["start_offset"],
            "document_start_offset": lo,
            "document_end_offset": hi,
            "truncated": lo != canonical["start_offset"] or hi != canonical["end_offset"],
        }


def test_unbounded_read_retains_existing_full_response(client, saved_source):
    full = saved_source["full"]
    assert "reading_range" not in full
    assert full["content"].startswith("Opening evidence")
    assert "UNREADTAILSENTINEL" in full["content"]
    for block in full["blocks"]:
        assert block["text"] == full["content"][block["start_offset"] : block["end_offset"]]
        assert block["content_hash"] == digest(block["text"])
        assert "text_range" not in block
    assert checked(client.get(saved_source["url"])) == full


def test_unicode_pagination_reconstructs_document_and_does_not_create_evidence(client, saved_source):
    full, url = saved_source["full"], saved_source["url"]
    start, chunks = 0, []
    while start < len(full["content"]):
        response = checked(client.get(url, params={"start": start, "limit": 13}))
        end = min(start + 13, len(full["content"]))
        assert_window(response, full, start, end)
        assert len(response["content"]) <= 13
        assert all(len(block["text"]) <= 13 for block in response["blocks"])
        chunks.append(response["content"])
        if not response["reading_range"]["has_more"]:
            break
        assert response["reading_range"]["next_offset"] > start
        start = response["reading_range"]["next_offset"]
    assert "".join(chunks) == full["content"]
    assert "🧪𠮷" in "".join(chunks)
    assert checked(client.get(f'/v1/projects/{saved_source["project"]["id"]}'))["evidence"] == []
    assert checked(client.get(url)) == full


def test_clipped_block_does_not_leak_unread_text_through_other_fields(client, saved_source):
    full = saved_source["full"]
    block = full["blocks"][1]
    response = checked(client.get(saved_source["url"], params={"start": block["start_offset"], "limit": 6}))
    assert_window(response, full, block["start_offset"], block["start_offset"] + 6)
    assert response["content"].startswith("🧪𠮷é")
    assert "unreadtailsentinel" not in json.dumps(response, ensure_ascii=False).casefold()


def test_range_between_blocks_returns_separators_without_unread_blocks(client, saved_source):
    full = saved_source["full"]
    start = full["blocks"][0]["end_offset"]
    end = full["blocks"][1]["start_offset"]
    response = checked(client.get(saved_source["url"], params={"start": start, "limit": end - start}))
    assert_window(response, full, start, end)
    assert response["content"] == "\n\n"
    assert response["blocks"] == []


def test_default_and_maximum_ranges_are_bounded_while_legacy_full_read_remains_available(client):
    proj = project(client)
    operation = ingestion(client, proj["id"], "🧪𠮷 measured evidence and limitations. " * 1200)
    url = f'/v1/documents/{operation["result_json"]["document_id"]}/content'
    full = checked(client.get(url))
    assert len(full["content"]) > 32000
    default = checked(client.get(url, params={"start": 0}))
    assert_window(default, full, 0, 8000)
    only_limit = checked(client.get(url, params={"limit": 17}))
    assert_window(only_limit, full, 0, 17)
    maximum = checked(client.get(url, params={"start": 2, "limit": 32000}))
    assert_window(maximum, full, 2, 32002)


def test_selected_block_defaults_to_its_start_and_stops_at_its_end(client, saved_source):
    full = saved_source["full"]
    block = full["blocks"][1]
    response = checked(client.get(saved_source["url"], params={"block_id": block["id"]}))
    assert_window(
        response, full, block["start_offset"], block["end_offset"],
        block_id=block["id"], stop=block["end_offset"],
    )
    assert response["content"] == block["text"]
    assert response["blocks"][0]["text_range"]["truncated"] is False


def test_exact_evidence_offsets_select_second_repeated_quote_after_non_bmp_characters(client, saved_source):
    full = saved_source["full"]
    block = full["blocks"][1]
    quote = "repeated evidence."
    first = block["text"].index(quote)
    second = block["text"].index(quote, first + len(quote))
    start = block["start_offset"] + second - 3
    response = checked(client.get(saved_source["url"], params={
        "block_id": block["id"], "start": start, "limit": len(quote) + 4,
    }))
    assert_window(
        response, full, start, start + len(quote) + 4,
        block_id=block["id"], stop=block["end_offset"],
    )
    read_block = response["blocks"][0]
    canonical_offset = read_block["text_range"]["block_start_offset"] + read_block["text"].index(quote)
    assert canonical_offset == second and canonical_offset != first
    assert len(block["text"][:second].encode("utf-16-le")) // 2 > second
    registered = checked(client.post('/v1/evidence', json={
        "project_id": saved_source["project"]["id"], "block_id": read_block["id"],
        "quote": quote, "start_offset": canonical_offset,
    }), 201)
    assert registered["start_offset"] == second
    assert registered["end_offset"] == second + len(quote)
    assert registered["content_hash"] == digest(quote)
    stored = checked(client.get(f'/v1/evidence/{registered["id"]}'))
    assert stored["block"]["text"] == block["text"]
    assert stored["block"]["text"][registered["start_offset"] : registered["end_offset"]] == quote


@pytest.mark.parametrize("params", [
    {"start": -1}, {"start": "not-an-integer"}, {"start": "1.5"},
    {"limit": 0}, {"limit": -1}, {"limit": 32001}, {"limit": "not-an-integer"},
])
def test_invalid_ranges_are_rejected(client, saved_source, params):
    assert client.get(saved_source["url"], params=params).status_code == 422


def test_document_eof_is_empty_and_out_of_bounds_start_is_rejected(client, saved_source):
    full = saved_source["full"]
    total = len(full["content"])
    eof = checked(client.get(saved_source["url"], params={"start": total, "limit": 10}))
    assert_window(eof, full, total, total)
    assert eof["blocks"] == []
    assert client.get(saved_source["url"], params={"start": total + 1, "limit": 10}).status_code == 422


def test_selected_block_rejects_starts_outside_its_own_coordinates(client, saved_source):
    block = saved_source["full"]["blocks"][1]
    for start in (block["start_offset"] - 1, block["end_offset"] + 1):
        response = client.get(saved_source["url"], params={"block_id": block["id"], "start": start, "limit": 10})
        assert response.status_code == 422, response.text


def test_blocks_from_another_document_or_missing_blocks_are_not_readable(client, saved_source):
    other = ingestion(client, saved_source["project"]["id"], "Another independent source has its own immutable blocks.")
    for block_id in (str(uuid4()), "unknown-block", other["result_json"]["block_ids"][0]):
        response = client.get(saved_source["url"], params={"block_id": block_id, "limit": 12})
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("with_block", [False, True])
def test_bounded_reads_do_not_cross_workspace_boundaries(client, saved_source, with_block):
    params = {"version": 1, "start": 0, "limit": 12}
    if with_block:
        params["block_id"] = saved_source["full"]["blocks"][0]["id"]
    response = client.get(saved_source["url"], params=params, headers={
        "Authorization": "Bearer owner-b", "X-Workspace-ID": "workspace-a",
    })
    assert response.status_code == 404
    assert "Opening evidence" not in response.text


def test_source_version_read_uses_original_representation_after_reprocessing(client, saved_source):
    full = saved_source["full"]
    capture_id = saved_source["operation"]["result_json"]["capture_id"]
    reprocess = checked(client.post(f'/v1/captures/{capture_id}/reprocess', json={}), 202)
    operation = checked(client.get(f'/v1/operations/{reprocess["id"]}'))
    assert operation["status"] == "succeeded"
    assert operation["result_json"]["representation_id"] != full["representation_id"]
    old_block = full["blocks"][1]
    response = checked(client.get(saved_source["url"], params={"version": 1, "block_id": old_block["id"], "limit": 7}))
    assert_window(
        response, full, old_block["start_offset"], old_block["start_offset"] + 7,
        block_id=old_block["id"], stop=old_block["end_offset"],
    )
    assert response["representation_id"] == full["representation_id"]
    assert client.get(saved_source["url"], params={
        "version": 1, "block_id": operation["result_json"]["block_ids"][0], "limit": 7,
    }).status_code == 404
    assert client.get(saved_source["url"], params={"version": 999, "limit": 7}).status_code == 404


def test_historical_report_ranges_read_saved_version_not_current_projection(client, saved_source):
    quote = "Opening evidence"
    anchor = evidence(client, saved_source["project"]["id"], saved_source["operation"], quote=quote)
    old_text = "# Original 🧪𠮷\n\nOpening evidence supports the historical interpretation."
    draft = {
        "project_id": saved_source["project"]["id"], "title": "Original report", "content": old_text,
        "claims": [{"text": quote, "evidence_ids": [anchor["id"]]}],
    }
    first = checked(client.post('/v1/proposals', json=draft), 201)
    report = checked(client.post(f'/v1/proposals/{first["id"]}/publish', json={}))
    newer = checked(client.post('/v1/proposals', json={
        **draft, "title": "Revised report", "content": "# Revised\n\nOpening evidence has revised limits.",
        "target_document_id": report["id"], "base_version": 1,
    }), 201)
    checked(client.post(f'/v1/proposals/{newer["id"]}/publish', json={"expected_version": 1}))
    url = f'/v1/documents/{report["id"]}/content'
    old_full = checked(client.get(url, params={"version": 1}))
    assert old_full["content"] == old_text and old_full["version"] == 1
    old_part = checked(client.get(url, params={"version": 1, "start": 10, "limit": 12}))
    assert_window(old_part, old_full, 10, 22)
    assert old_part["version"] == 1 and old_part["title"] == "Original report"
    current_full = checked(client.get(url))
    assert current_full["version"] == 2 and current_full["content"] != old_text
    current_part = checked(client.get(url, params={"start": 10, "limit": 12}))
    assert_window(current_part, current_full, 10, 22)
    assert client.get(url, params={"version": 1, "block_id": saved_source["full"]["blocks"][0]["id"]}).status_code == 404


@pytest.mark.parametrize("options,expected", [
    ({}, {"limit": ["8000"]}),
    ({"start": 11, "limit": 19, "version": 2}, {"start": ["11"], "limit": ["19"], "version": ["2"]}),
])
def test_mcp_read_document_forwards_explicit_bounded_query(monkeypatch, options, expected):
    document_id = str(uuid4())
    calls = []
    real_client = httpx.Client

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"content": "bounded", "blocks": []})

    monkeypatch.setenv("EVIDENCEHARBOR_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("EVIDENCEHARBOR_API_TOKEN", "fixture-researcher-token")
    monkeypatch.setattr(mcp_server.httpx, 'Client', lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    result = mcp_server.read_document(document_id, **options)
    assert result == {"content": "bounded", "blocks": []}
    assert len(calls) == 1 and calls[0].method == "GET"
    parsed = urlsplit(str(calls[0].url))
    assert parsed.path == f'/v1/documents/{document_id}/content'
    assert parse_qs(parsed.query) == expected


def test_mcp_block_read_leaves_start_unset_for_server_block_default(monkeypatch):
    document_id, block_id = str(uuid4()), str(uuid4())
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"content": "saved block"}

    monkeypatch.setattr(mcp_server, 'request', request)
    assert mcp_server.read_document(document_id, block_id=block_id) == {"content": "saved block"}
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert parse_qs(urlsplit(calls[0][1]).query) == {"limit": ["8000"], "block_id": [block_id]}
