"""Actual local S3-compatible server tests, not a mocked boto3 client."""

import hashlib
import os
from uuid import uuid4

import boto3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import create_app
from backend.config import Settings
from backend.db import Base
from backend.storage import get_store


@pytest.fixture
def s3_api(tmp_path, monkeypatch):
    endpoint = os.getenv("EVIDENCEHARBOR_TEST_S3")
    if not endpoint:
        pytest.skip("Live local S3-compatible service not configured")
    bucket = "test-" + uuid4().hex
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=bucket)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", bucket)
    monkeypatch.setenv("S3_ENDPOINT_URL", endpoint)
    monkeypatch.setenv("S3_PREFIX", "evidence")
    engine = create_engine(
        f"sqlite:///{tmp_path}/workspace.sqlite", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = Settings(
        demo_mode=True, demo_workspace_id="s3-fixture", inline_worker=True, blob_dir=tmp_path / "unused"
    )
    with TestClient(create_app(config, factory)) as api:
        yield api, get_store(), client, bucket
    engine.dispose()


def test_live_s3_upload_pipeline_evidence_export_and_reprocess(s3_api):
    api, store, _client, _bucket = s3_api
    project = api.post("/v1/projects", json={"name": "S3 pipeline"}).json()
    raw = b"S3 evidence remains exactly traceable across reprocessing and exports."
    response = api.post(
        "/v1/ingestions/upload",
        data={"project_id": project["id"]},
        files={"file": ("evidence.txt", raw, "text/plain")},
    )
    assert response.status_code == 202, response.text
    operation = api.get("/v1/operations/" + response.json()["id"]).json()
    assert operation["status"] == "succeeded", operation
    assert store.get(hashlib.sha256(raw).hexdigest()) == raw
    hit = api.post("/v1/search", json={"project_id": project["id"], "query": "evidence"}).json()["results"][0]
    evidence = api.post(
        "/v1/evidence",
        json={"project_id": project["id"], "block_id": hit["block_id"], "quote": "exactly traceable"},
    ).json()
    assert api.get("/v1/evidence/" + evidence["id"]).status_code == 200
    assert api.get("/v1/captures/" + hit["capture_id"] + "/raw").content == raw
    exported = api.get("/v1/projects/" + project["id"] + "/export")
    assert exported.status_code == 200 and exported.content[:2] == b"PK"
    rep = api.post("/v1/captures/" + hit["capture_id"] + "/reprocess")
    assert rep.status_code == 202, rep.text
    result = api.get("/v1/operations/" + rep.json()["id"]).json()
    assert result["status"] == "succeeded", result
    assert api.get("/v1/evidence/" + evidence["id"]).status_code == 200


def test_live_s3_immutable_deduplicated_put(s3_api):
    _, store, client, bucket = s3_api
    one = store.put(b"immutable S3 bytes", "text/plain")
    two = store.put(b"immutable S3 bytes", "text/plain")
    assert one.sha256 == two.sha256 and one.key == two.key
    assert store.verify(one.sha256)
    objects = client.list_objects_v2(Bucket=bucket, Prefix=one.key)
    assert len(objects.get("Contents", [])) == 1


def test_live_s3_recovery_set_roundtrip_is_not_independent_restore_acceptance(s3_api, tmp_path):
    """Real S3 byte transport of synthetic artifacts; no native DB restore claim."""
    from backend.recovery import COMPONENTS, FORMAT, retrieve_set, timestamp, upload_set

    _, store, client, bucket = s3_api
    roots = {}
    for name in COMPONENTS:
        root = tmp_path / "snapshot" / name
        root.mkdir(parents=True)
        (root / "fixture.bin").write_bytes((name + " synthetic backup artifact").encode())
        roots[name] = str(root)
    spec = {
        "format": FORMAT, "engine": "postgresql-temporal", "run_id": "ci-fixture",
        "snapshot_id": "synthetic-1", "snapshot_at": timestamp(), "source_failure_domain": "disposable-ci-host",
        "consistency": "writers-stopped", "operator": "test fixture",
        "quiescence_evidence": "Synthetic fixture has no services or live writers",
        "schema_revision": "0009", "release_commit": "synthetic-fixture",
        "configuration_secrets_excluded": True, "components": roots,
    }
    manifest_hash = upload_set(spec, store)
    restored = tmp_path / "fresh-download"
    receipt = retrieve_set(store, manifest_hash, restored)
    assert receipt["all_bytes_verified"] and not receipt["native_restore_tested"]
    assert receipt["components"] == sorted(COMPONENTS)
    for name in COMPONENTS:
        assert (restored / name / "fixture.bin").read_bytes() == (name + " synthetic backup artifact").encode()
    with pytest.raises(ValueError, match="must not exist"):
        retrieve_set(store, manifest_hash, restored)
    # A missing required remote object prevents another successful receipt/download.
    import json

    from backend.storage import StorageError

    manifest = json.loads(store.get(manifest_hash))
    chunk = manifest["files"][0]["chunks"][0]
    client.delete_object(Bucket=bucket, Key=store._key(chunk))
    with pytest.raises(StorageError):
        retrieve_set(store, manifest_hash, tmp_path / "incomplete")
    assert not (tmp_path / "incomplete").exists()
