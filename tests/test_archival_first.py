"""Raw acquisition survives failed parsing and is the sole input to later recovery."""

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend import db, domain, processing
from backend import models as m
from backend.api import create_app
from backend.security import FetchResult
from tests.helpers import digest
from tests.test_api_workflow import checked, project
from tests.test_processing import config_with


@pytest.fixture
def failed_parser(monkeypatch, runtime):
    original = processing.process_document
    visible_before_parsing = []

    def fail(raw, *_args, **_kwargs):
        # A separate connection sees only committed captures, not this worker's pending transaction.
        with runtime.session_factory() as session:
            ids = list(session.scalars(select(m.Capture.id).where(m.Capture.content_hash == digest(raw))))
            visible_before_parsing.append(ids)
        raise processing.PipelineError("Synthetic parser failure after raw archival")

    monkeypatch.setattr(processing, "process_document", fail)
    return original, visible_before_parsing


def finished(client, operation_id):
    return checked(client.get(f'/v1/operations/{operation_id}'))


def assert_no_derived_resources(runtime):
    with runtime.session_factory() as session:
        for model in (m.Representation, m.Block, m.Document, m.IndexGeneration, m.Evidence):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def assert_failed_but_archived(client, runtime, operation_id, raw):
    operation = finished(client, operation_id)
    assert operation["status"] == "failed", operation
    result = operation["result_json"]
    assert result["archival_complete"] is True
    assert result["source_id"] and result["capture_id"]
    assert "representation_id" not in result and "document_id" not in result
    capture = checked(client.get(f'/v1/captures/{result["capture_id"]}'))
    assert capture["content_hash"] == digest(raw)
    assert capture["byte_size"] == len(raw)
    assert capture["source_id"] == result["source_id"]
    assert capture["representations"] == []
    diagnostic = next(item for item in capture["processing_operations"] if item["id"] == operation_id)
    assert diagnostic["status"] == "failed" and diagnostic["error"]
    downloaded = client.get(f'/v1/captures/{result["capture_id"]}/raw')
    assert downloaded.status_code == 200 and downloaded.content == raw
    assert_no_derived_resources(runtime)
    return operation, capture


@pytest.mark.parametrize("filename,media_type,raw", [
    ("malformed.pdf", "application/pdf", b"%PDF-1.7\nSynthetic malformed fixture without objects or xref.\n"),
    ("empty.html", "text/html", b"<!doctype html><html><head><title>Empty fixture</title></head><body></body></html>"),
])
def test_real_parse_failure_keeps_raw_capture_export_and_workspace_isolation(client, runtime, filename, media_type, raw):
    proj = project(client)
    submitted = checked(client.post('/v1/ingestions/upload', data={"project_id": proj["id"]},
        files={"file": (filename, raw, media_type)}), 202)
    operation, capture = assert_failed_but_archived(client, runtime, submitted["id"], raw)
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.Source)) == 1
        assert session.scalar(select(func.count()).select_from(m.Capture)) == 1
    exported = client.get(f'/v1/projects/{proj["id"]}/export')
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["resources"]["documents"] == []
        assert any(item["id"] == capture["id"] for item in manifest["resources"]["captures"])
        saved = next(item for item in manifest["objects"] if item["sha256"] == digest(raw))
        assert archive.read(saved["path"]) == raw
    other = {"Authorization": "Bearer owner-b", "X-Workspace-ID": "workspace-a"}
    for path in (
        f'/v1/captures/{capture["id"]}', f'/v1/captures/{capture["id"]}/raw',
        f'/v1/sources/{operation["result_json"]["source_id"]}',
        f'/v1/operations/{submitted["id"]}', f'/v1/projects/{proj["id"]}/export',
    ):
        assert client.get(path, headers=other).status_code == 404
    assert client.post(f'/v1/operations/{submitted["id"]}/retry', headers=other, json={}).status_code == 404
    assert client.post(f'/v1/captures/{capture["id"]}/reprocess', headers=other, json={}).status_code == 404


def test_url_retry_uses_committed_capture_without_refetching_changed_origin(client, runtime, monkeypatch, failed_parser):
    proj = project(client)
    original = b"Original saved evidence remains authoritative after a parser failure."
    origin = {"body": original}
    requested = []

    def fetch(url, **_kwargs):
        requested.append(url)
        return FetchResult(origin["body"], url, "text/plain", 200, {"content-type": "text/plain"})

    monkeypatch.setattr(processing, "safe_fetch", fetch)
    submitted = checked(client.post('/v1/ingestions', json={
        "project_id": proj["id"], "type": "url", "url": "https://example.org/archival-first",
    }), 202)
    failed, capture = assert_failed_but_archived(client, runtime, submitted["id"], original)
    assert failed_parser[1] == [[capture["id"]]]
    origin["body"] = b"Changed origin text must not silently replace the already archived reading."
    monkeypatch.setattr(processing, "process_document", failed_parser[0])
    checked(client.post(f'/v1/operations/{submitted["id"]}/retry', json={}), 202)
    recovered = finished(client, submitted["id"])
    assert recovered["status"] == "succeeded", recovered
    assert recovered["result_json"]["capture_id"] == capture["id"]
    assert recovered["result_json"]["source_id"] == failed["result_json"]["source_id"]
    document = checked(client.get(f'/v1/documents/{recovered["result_json"]["document_id"]}/content'))
    assert document["content"] == original.decode()
    assert requested == ["https://example.org/archival-first"]
    with runtime.session_factory() as session:
        for model in (m.Capture, m.Representation, m.Document):
            assert session.scalar(select(func.count()).select_from(model)) == 1
    assert client.post(f'/v1/operations/{submitted["id"]}/retry', json={}).status_code == 409


def test_upload_retry_after_new_app_and_factory_survives_missing_collector_file(client, runtime, monkeypatch, failed_parser):
    proj = project(client)
    raw = b"A staged upload remains recoverable from its durable original capture after restart."
    staged = runtime.settings.blob_dir / "collector" / "temporary-upload.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(raw)
    with runtime.session_factory() as session:
        operation = domain.create_operation(session, "workspace-a", proj["id"], {
            "type": "upload", "blob_path": str(staged), "media_type": "text/plain",
            "filename": "temporary-upload.txt", "title": "Durable staged upload",
        })
        session.commit()
        operation_id = operation.id
    domain.execute_operation(operation_id, runtime.session_factory, settings_override=runtime.settings)
    _, capture = assert_failed_but_archived(client, runtime, operation_id, raw)
    assert failed_parser[1] == [[capture["id"]]]
    staged.unlink()
    monkeypatch.setattr(processing, "process_document", failed_parser[0])
    fresh_engine = db.make_engine(runtime.settings.database_url)
    fresh_factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    try:
        app = create_app(settings_override=runtime.settings, session_factory=fresh_factory)
        with TestClient(app, headers={"Authorization": "Bearer owner-a"}) as restarted:
            assert finished(restarted, operation_id)["result_json"]["capture_id"] == capture["id"]
            checked(restarted.post(f'/v1/operations/{operation_id}/retry', json={}), 202)
            recovered = finished(restarted, operation_id)
            assert recovered["status"] == "succeeded", recovered
            assert recovered["result_json"]["capture_id"] == capture["id"]
            assert restarted.get(f'/v1/captures/{capture["id"]}/raw').content == raw
        with fresh_factory() as session:
            for model in (m.Capture, m.Representation, m.Document):
                assert session.scalar(select(func.count()).select_from(model)) == 1
    finally:
        fresh_engine.dispose()
    assert not staged.exists()


def test_archived_failed_capture_can_be_reprocessed_with_an_explicit_pipeline(client, runtime, monkeypatch, failed_parser):
    proj = project(client)
    raw = ("Saved evidence and its limitations remain exactly traceable. " * 10).encode()
    submitted = checked(client.post('/v1/ingestions/upload', data={"project_id": proj["id"]},
        files={"file": ("source.txt", raw, "text/plain")}), 202)
    _, capture = assert_failed_but_archived(client, runtime, submitted["id"], raw)
    monkeypatch.setattr(processing, "process_document", failed_parser[0])
    reprocessed = checked(client.post(f'/v1/captures/{capture["id"]}/reprocess', json={
        "pipeline_config": config_with("source_map", max_block_chars=100),
    }), 202)
    recovered = finished(client, reprocessed["id"])
    assert recovered["status"] == "succeeded", recovered
    assert recovered["result_json"]["capture_id"] == capture["id"]
    assert len(recovered["result_json"]["block_ids"]) > 1
    assert finished(client, submitted["id"])["status"] == "failed"
    saved = checked(client.get(f'/v1/captures/{capture["id"]}'))
    assert len(saved["representations"]) == 1
    assert {item["id"] for item in saved["processing_operations"]} >= {submitted["id"], reprocessed["id"]}


def test_corrupted_archived_raw_fails_retry_closed_without_reacquiring_source(client, runtime, monkeypatch, failed_parser):
    proj = project(client)
    raw = b"Archived evidence that will be deliberately corrupted only inside this isolated test."
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        return FetchResult(raw, url, "text/plain", 200, {})

    monkeypatch.setattr(processing, "safe_fetch", fetch)
    submitted = checked(client.post('/v1/ingestions', json={
        "project_id": proj["id"], "type": "url", "url": "https://example.org/corrupt-retry",
    }), 202)
    _, capture = assert_failed_but_archived(client, runtime, submitted["id"], raw)
    with runtime.session_factory() as session:
        stored = session.get(m.Capture, capture["id"])
        path = Path(stored.blob_path)
        path.chmod(0o600)
        path.write_bytes(b"Corrupted bytes")
    monkeypatch.setattr(processing, "process_document", failed_parser[0])
    checked(client.post(f'/v1/operations/{submitted["id"]}/retry', json={}), 202)
    rejected = finished(client, submitted["id"])
    assert rejected["status"] == "failed", rejected
    assert rejected["result_json"]["capture_id"] == capture["id"]
    assert len(calls) == 1
    assert_no_derived_resources(runtime)
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.Capture)) == 1


def test_private_url_rejection_still_creates_no_raw_capture(client, runtime):
    proj = project(client)
    submitted = checked(client.post('/v1/ingestions', json={
        "project_id": proj["id"], "type": "url", "url": "http://169.254.169.254/latest/meta-data/",
    }), 202)
    rejected = finished(client, submitted["id"])
    assert rejected["status"] == "failed"
    assert not rejected["result_json"] or not rejected["result_json"].get("archival_complete")
    assert_no_derived_resources(runtime)
    with runtime.session_factory() as session:
        for model in (m.Source, m.Capture):
            assert session.scalar(select(func.count()).select_from(model)) == 0
