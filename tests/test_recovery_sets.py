"""Synthetic native-backup transport and fail-closed durability admission.

The fake is only the S3 protocol boundary: production immutable storage, recovery
validation, CLI orchestration and Settings startup validation run unchanged.
These tests do not establish a successful PostgreSQL/Temporal native restore.
"""
from __future__ import annotations

import copy
import importlib
import io
import json
import os
import signal
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from backend import recovery
from backend.config import Settings
from backend.storage import LocalContentStore, S3ContentStore, StorageError
from scripts import recovery as recovery_cli


class ConditionalConflict(Exception):
    response: ClassVar[dict] = {"ResponseMetadata": {"HTTPStatusCode": 412}}


class MemoryS3:
    """Conditional creates, verified reads, and controlled failure injection."""

    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url="https://backup.example.test")
        self.objects = {}
        self.events = []
        self.puts = []
        self.read_bodies = []
        self.before_put = None
        self.before_get = None

    def put_object(self, **request):
        assert request["IfNoneMatch"] == "*"
        assert request["Metadata"]["sha256"] == recovery.digest(request["Body"])
        key = (request["Bucket"], request["Key"])
        self.puts.append(request)
        self.events.append(("put", key))
        if self.before_put:
            self.before_put(request)
        if key in self.objects:
            raise ConditionalConflict()
        self.objects[key] = request["Body"]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, **request):
        key = (request["Bucket"], request["Key"])
        self.events.append(("get", key))
        if self.before_get:
            self.before_get(request)
        body = io.BytesIO(self.objects[key])
        self.read_bodies.append(body)
        return {"Body": body}


@pytest.fixture(autouse=True)
def isolated_recovery_environment(monkeypatch):
    for name in (
        "DURABLE_CONTINUOUS_RUN", "DURABILITY_POLICY", "DURABILITY_RECEIPT",
        "DURABILITY_RESTORE_REPORT", "RECOVERY_S3_BUCKET", "RECOVERY_S3_PREFIX",
        "API_TOKEN", "API_TOKENS_JSON",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def remote():
    client = MemoryS3()
    store = S3ContentStore("synthetic-backup-bucket", prefix="recovery-fixtures", client=client)
    return client, store


@pytest.fixture
def native_set(tmp_path, monkeypatch):
    # Small chunks exercise multipart file reconstruction without large fixtures.
    monkeypatch.setattr(recovery, "CHUNK_BYTES", 64)
    paths = {
        "research_database": "research.dump",
        "temporal_persistence": "temporal.dump",
        "temporal_visibility": "visibility.dump",
        "objects": "raw/aa/synthetic-object",
        "configuration": "operator.json",
    }
    roots, expected = {}, {}
    for component, relative in paths.items():
        root = tmp_path / "native" / component
        source = root / relative
        source.parent.mkdir(parents=True)
        data = (f"Synthetic {component} backup bytes.\n" * 5).encode()
        source.write_bytes(data)
        roots[component] = str(root)
        expected[f"{component}/{relative}"] = data
    spec = {
        "format": recovery.FORMAT,
        "engine": "postgresql-temporal",
        "run_id": "synthetic-run-001",
        "snapshot_id": "snapshot-001",
        "snapshot_at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        "source_failure_domain": "primary-host-a",
        "consistency": "writers-stopped",
        "operator": "synthetic-operator",
        "quiescence_evidence": "Synthetic fixture; API, workers and schedules stopped",
        "schema_revision": "synthetic-schema-revision",
        "release_commit": "a" * 40,
        "configuration_secrets_excluded": True,
        "components": roots,
    }
    return spec, expected


@pytest.fixture
def uploaded(native_set, remote):
    spec, expected = native_set
    client, store = remote
    manifest_hash = recovery.upload_set(spec, store)
    manifest = json.loads(store.get(manifest_hash))
    return SimpleNamespace(
        spec=spec, expected=expected, client=client, store=store,
        manifest_hash=manifest_hash, manifest=manifest,
    )


def save_manifest(uploaded, manifest):
    return uploaded.store.put(recovery.canonical(manifest), "application/json").sha256


def configure_cli(monkeypatch, store, *arguments):
    monkeypatch.setenv("RECOVERY_S3_BUCKET", store.bucket)
    monkeypatch.setenv("RECOVERY_S3_PREFIX", store.prefix)

    def configured_store(bucket, *, prefix):
        assert (bucket, prefix) == (store.bucket, store.prefix)
        return store

    monkeypatch.setattr(recovery_cli, "S3ContentStore", configured_store)
    monkeypatch.setattr(sys, "argv", ["recovery.py", *map(str, arguments)])


def test_complete_set_is_immutable_verified_and_downloaded_to_fresh_directory(
    uploaded, tmp_path,
):
    client, store = uploaded.client, uploaded.store
    receipt = recovery.retrieve_set(store, uploaded.manifest_hash, tmp_path / "restored")
    assert receipt["manifest_sha256"] == uploaded.manifest_hash
    assert receipt["run_id"] == uploaded.spec["run_id"]
    assert receipt["snapshot_id"] == uploaded.spec["snapshot_id"]
    assert receipt["components"] == sorted(recovery.COMPONENTS)
    assert receipt["verified_files"] == 5
    assert receipt["verified_bytes"] == sum(map(len, uploaded.expected.values()))
    assert receipt["all_bytes_verified"] is True
    assert receipt["native_restore_tested"] is False
    assert receipt["destination"] == {
        "bucket": store.bucket, "prefix": store.prefix,
        "endpoint": "https://backup.example.test",
    }
    manifest = json.loads((tmp_path / "restored/recovery-manifest.json").read_bytes())
    assert manifest == uploaded.manifest
    for item in manifest["files"]:
        raw = uploaded.expected[item["path"]]
        assert (tmp_path / "restored" / item["path"]).read_bytes() == raw
        assert item["sha256"] == recovery.digest(raw)
        assert item["bytes"] == len(raw)
        assert len(item["chunks"]) > 1
    # Manifest is installed last; every attempted put has immediate readback.
    manifest_key = (store.bucket, store._key(uploaded.manifest_hash))
    assert client.puts[-1]["Key"] == manifest_key[1]
    for index, (operation, key) in enumerate(client.events):
        if operation == "put":
            assert client.events[index + 1] == ("get", key)
    assert all(body.closed for body in client.read_bodies)
    assert not list(tmp_path.glob(".eh-recovery-*"))


def test_repeated_upload_deduplicates_chunks_without_overwriting(native_set, remote, monkeypatch):
    spec, expected = native_set
    client, store = remote
    monkeypatch.setattr(recovery, "timestamp", lambda: "2026-10-05T12:00:00+00:00")
    first_hash = recovery.upload_set(spec, store)
    original_objects = dict(client.objects)
    second_hash = recovery.upload_set(spec, store)
    assert first_hash == second_hash
    assert client.objects == original_objects
    assert len(client.puts) > len(client.objects)
    assert recovery.retrieve_set(store, second_hash)["verified_bytes"] == sum(map(len, expected.values()))


def test_empty_files_and_repeated_content_round_trip(native_set, remote, tmp_path):
    spec, expected = native_set
    object_root = tmp_path / "native/objects"
    (object_root / "empty").write_bytes(b"")
    duplicate = expected["research_database/research.dump"]
    (object_root / "duplicate").write_bytes(duplicate)
    _, store = remote
    manifest_hash = recovery.upload_set(spec, store)
    receipt = recovery.retrieve_set(store, manifest_hash, tmp_path / "restored")
    assert receipt["verified_files"] == 7
    assert (tmp_path / "restored/objects/empty").read_bytes() == b""
    assert (tmp_path / "restored/objects/duplicate").read_bytes() == duplicate


@pytest.mark.parametrize("bad_path", [
    "../escaped", "/absolute", "objects/../../escaped", "objects/./file",
    "objects//file", "objects\\escaped", "objects/" + "nested/" * 16 + "file",
])
def test_manifest_path_traversal_is_refused_before_download(uploaded, tmp_path, bad_path):
    manifest = copy.deepcopy(uploaded.manifest)
    manifest["files"][0]["path"] = bad_path
    manifest_hash = save_manifest(uploaded, manifest)
    destination = tmp_path / "download"
    with pytest.raises(ValueError, match="path"):
        recovery.retrieve_set(uploaded.store, manifest_hash, destination)
    assert not destination.exists()
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("damage", [
    "missing_component", "duplicate_path", "unknown_component", "wrong_component_path",
    "negative_size", "boolean_size", "bad_file_hash", "bad_chunk_hash", "missing_chunks",
    "wrong_engine", "not_quiesced", "secrets_not_excluded",
])
def test_incomplete_or_invalid_manifest_is_refused(uploaded, tmp_path, damage):
    manifest = copy.deepcopy(uploaded.manifest)
    item = manifest["files"][0]
    if damage == "missing_component":
        manifest["files"].pop()
    elif damage == "duplicate_path":
        manifest["files"].append(copy.deepcopy(item))
    elif damage == "unknown_component":
        item["component"] = "unapproved"
    elif damage == "wrong_component_path":
        item["path"] = "objects/foreign-component.dump"
    elif damage == "negative_size":
        item["bytes"] = -1
    elif damage == "boolean_size":
        item["bytes"] = True
    elif damage == "bad_file_hash":
        item["sha256"] = "invalid"
    elif damage == "bad_chunk_hash":
        item["chunks"][0] = "invalid"
    elif damage == "missing_chunks":
        item["chunks"].pop()
    elif damage == "wrong_engine":
        manifest["engine"] = "sqlite"
    elif damage == "not_quiesced":
        manifest["consistency"] = "live-writers"
    elif damage == "secrets_not_excluded":
        manifest["configuration_secrets_excluded"] = False
    destination = tmp_path / "download"
    with pytest.raises(ValueError):
        recovery.retrieve_set(uploaded.store, save_manifest(uploaded, manifest), destination)
    assert not destination.exists()


@pytest.mark.parametrize("raw", [b"{broken", b"null", b"[]", b'"manifest"'])
def test_malformed_manifest_is_deliberately_rejected(remote, tmp_path, raw):
    _, store = remote
    manifest_hash = store.put(raw).sha256
    with pytest.raises(ValueError):
        recovery.retrieve_set(store, manifest_hash, tmp_path / "download")
    assert not (tmp_path / "download").exists()


def test_non_object_file_record_is_deliberately_rejected(uploaded):
    manifest = copy.deepcopy(uploaded.manifest)
    manifest["files"][0] = None
    with pytest.raises(ValueError):
        recovery.retrieve_set(uploaded.store, save_manifest(uploaded, manifest))


@pytest.mark.parametrize("bound", ["MAX_MANIFEST_BYTES", "MAX_FILES", "MAX_TOTAL_BYTES"])
def test_manifest_resource_bounds_prevent_download(uploaded, tmp_path, monkeypatch, bound):
    monkeypatch.setattr(recovery, bound, 1)
    with pytest.raises(ValueError, match="bound|limit"):
        recovery.retrieve_set(uploaded.store, uploaded.manifest_hash, tmp_path / "download")
    assert not (tmp_path / "download").exists()


@pytest.mark.parametrize("damage", ["missing_manifest", "missing_chunk", "corrupt_chunk", "wrong_size", "wrong_hash"])
def test_missing_or_corrupt_remote_data_never_installs_partial_download(uploaded, tmp_path, damage):
    manifest = copy.deepcopy(uploaded.manifest)
    manifest_hash = uploaded.manifest_hash
    store, client = uploaded.store, uploaded.client
    item = manifest["files"][2]  # Fail after earlier files have already staged successfully.
    chunk_key = (store.bucket, store._key(item["chunks"][0]))
    if damage == "missing_manifest":
        del client.objects[(store.bucket, store._key(manifest_hash))]
    elif damage == "missing_chunk":
        del client.objects[chunk_key]
    elif damage == "corrupt_chunk":
        client.objects[chunk_key] = b"X" * len(client.objects[chunk_key])
    elif damage == "wrong_size":
        item["chunks"][0] = store.put(b"short").sha256
        manifest_hash = save_manifest(uploaded, manifest)
    elif damage == "wrong_hash":
        item["sha256"] = "0" * 64
        manifest_hash = save_manifest(uploaded, manifest)
    with pytest.raises((ValueError, StorageError)):
        recovery.retrieve_set(store, manifest_hash, tmp_path / "download")
    assert not (tmp_path / "download").exists()
    assert not list(tmp_path.glob(".eh-recovery-*"))
    assert all(body.closed for body in client.read_bodies)


@pytest.mark.parametrize("destination_kind", ["directory", "file", "dangling_symlink"])
def test_download_refuses_existing_destination(uploaded, tmp_path, destination_kind):
    destination = tmp_path / "download"
    if destination_kind == "directory":
        destination.mkdir()
        (destination / "keep").write_text("existing content")
    elif destination_kind == "file":
        destination.write_text("existing content")
    else:
        destination.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="must not exist"):
        recovery.retrieve_set(uploaded.store, uploaded.manifest_hash, destination)
    if destination_kind == "directory":
        assert (destination / "keep").read_text() == "existing content"
    elif destination_kind == "file":
        assert destination.read_text() == "existing content"
    else:
        assert destination.is_symlink()


@pytest.mark.parametrize("damage", ["missing_component", "empty_component", "symlink", "credential_file", "local_adapter"])
def test_upload_refuses_incomplete_or_unsafe_source(native_set, remote, tmp_path, damage):
    spec, _ = native_set
    client, store = remote
    if damage == "missing_component":
        del spec["components"]["research_database"]
    elif damage == "empty_component":
        (tmp_path / "native/research_database/research.dump").unlink()
    elif damage == "symlink":
        (tmp_path / "native/configuration/link").symlink_to(tmp_path / "native/research_database")
    elif damage == "credential_file":
        (tmp_path / "native/configuration/.env").write_text("SYNTHETIC_PLACEHOLDER=value")
    else:
        store = LocalContentStore(tmp_path / "local")
    with pytest.raises(TypeError if damage == "local_adapter" else ValueError):
        recovery.upload_set(spec, store)
    assert not any(request["ContentType"] == "application/json" for request in client.puts)


@pytest.mark.parametrize("mutation", ["append", "replace_same_size"])
def test_source_change_during_upload_never_commits_manifest(native_set, remote, tmp_path, mutation):
    spec, _ = native_set
    client, store = remote
    source = tmp_path / "native/configuration/operator.json"
    original = source.read_bytes()

    def mutate_once(request):
        client.before_put = None
        if mutation == "append":
            with source.open("ab") as stream:
                stream.write(b"changed while reading")
        else:
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(b"X" * len(original))
            replacement.replace(source)

    client.before_put = mutate_once
    with pytest.raises(ValueError, match="changed during upload"):
        recovery.upload_set(spec, store)
    assert not any(request["ContentType"] == "application/json" for request in client.puts)


def test_corrupt_existing_chunk_cannot_be_overwritten_by_upload(native_set, remote):
    spec, _ = native_set
    client, store = remote
    first_hash = recovery.upload_set(spec, store)
    manifest = json.loads(store.get(first_hash))
    chunk_hash = manifest["files"][0]["chunks"][0]
    key = (store.bucket, store._key(chunk_hash))
    client.objects[key] = b"corrupt remote content"
    with pytest.raises(StorageError, match="digest mismatch"):
        recovery.upload_set(spec, store)
    assert client.objects[key] == b"corrupt remote content"


def test_cli_upload_issues_receipt_only_after_complete_readback(native_set, remote, tmp_path, monkeypatch, capsys):
    spec, _ = native_set
    _, store = remote
    spec_path, receipt_path = tmp_path / "spec.json", tmp_path / "receipt.json"
    recovery.write_new(spec_path, spec)
    configure_cli(monkeypatch, store, "upload", spec_path, receipt_path)
    assert recovery_cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    receipt = recovery.read_json(receipt_path)
    assert output["manifest_sha256"] == receipt["manifest_sha256"]
    assert receipt["verified_files"] == 5
    assert receipt["all_bytes_verified"] is True
    assert output["native_restore_tested"] is False


@pytest.mark.parametrize("phase", ["upload", "manifest_readback", "full_verification"])
def test_interrupted_cli_operation_never_issues_success_receipt(
    native_set, remote, tmp_path, monkeypatch, capsys, phase,
):
    spec, _ = native_set
    client, store = remote
    spec_path, receipt_path = tmp_path / "spec.json", tmp_path / "receipt.json"
    recovery.write_new(spec_path, spec)
    configure_cli(monkeypatch, store, "upload", spec_path, receipt_path)
    manifest_gets = 0

    def fail_put(request):
        if len(client.puts) == 2:
            raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")

    def fail_manifest_get(request):
        nonlocal manifest_gets
        matching = next((put for put in client.puts if put["Key"] == request["Key"]), None)
        if matching and matching["ContentType"] == "application/json":
            manifest_gets += 1
            if manifest_gets == (1 if phase == "manifest_readback" else 2):
                raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")

    if phase == "upload":
        client.before_put = fail_put
    else:
        client.before_get = fail_manifest_get
    assert recovery_cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "no success receipt issued" in output.err
    assert "SYNTHETIC_PRIVATE_ERROR" not in output.err
    assert not receipt_path.exists()
    if phase == "upload":
        assert not any(request["ContentType"] == "application/json" for request in client.puts)


def test_cli_adapter_setup_failure_is_sanitized(remote, tmp_path, monkeypatch, capsys):
    _, store = remote
    receipt_path = tmp_path / "receipt.json"
    configure_cli(monkeypatch, store, "verify", "0" * 64, receipt_path)

    def unavailable_store(*args, **kwargs):
        raise StorageError("SYNTHETIC_PRIVATE_CONFIGURATION_ERROR")

    monkeypatch.setattr(recovery_cli, "S3ContentStore", unavailable_store)
    assert recovery_cli.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "no success receipt issued" in output.err
    assert "SYNTHETIC_PRIVATE_CONFIGURATION_ERROR" not in output.err
    assert not receipt_path.exists()


def test_cli_never_overwrites_prior_receipt(uploaded, tmp_path, monkeypatch, capsys):
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("prior verification history")
    configure_cli(monkeypatch, uploaded.store, "verify", uploaded.manifest_hash, receipt_path)
    before = len(uploaded.client.events)
    with pytest.raises(SystemExit) as error:
        recovery_cli.main()
    assert error.value.code == 2
    assert "never overwrite" in capsys.readouterr().err
    assert receipt_path.read_text() == "prior verification history"
    assert len(uploaded.client.events) == before


@pytest.fixture
def admission(uploaded):
    now = datetime.now(UTC)
    receipt = recovery.retrieve_set(uploaded.store, uploaded.manifest_hash)
    receipt["verified_at"] = now.isoformat()
    policy = {
        "format": "evidenceharbor-durability-policy-v1",
        "run_id": uploaded.spec["run_id"],
        "source_failure_domain": uploaded.spec["source_failure_domain"],
        "backup_failure_domain": "separate-backup-account-b",
        "operator": "synthetic-operator",
        "independence_evidence": "Synthetic independent account and host declaration",
        "independent_failure_domain_attested": True,
        "maximum_backup_age_seconds": 300,
        "maximum_restore_test_age_seconds": 3600,
        "destination": copy.deepcopy(receipt["destination"]),
    }
    report = {
        "format": "evidenceharbor-restore-attestation-v1",
        "run_id": policy["run_id"],
        "manifest_sha256": receipt["manifest_sha256"],
        "backup_failure_domain": policy["backup_failure_domain"],
        "restore_failure_domain": "clean-restore-host-c",
        "operator": "synthetic-operator",
        "evidence_reference": "synthetic://native-restore-report",
        "passed": True,
        "tested_at": now.isoformat(),
        "checks": dict.fromkeys((
            "research_database", "temporal_history", "raw_objects",
            "evidence_locators", "pending_job_recovery",
        ), True),
    }
    return policy, receipt, report, now


def test_explicit_independent_native_restore_attestation_admits_exact_set(admission):
    policy, receipt, report, now = admission
    result = recovery.validate_durable_admission(policy, receipt, report, now=now)
    assert result["admitted"] is True
    assert result["run_id"] == policy["run_id"]
    assert result["manifest_sha256"] == receipt["manifest_sha256"]
    assert receipt["native_restore_tested"] is False


@pytest.mark.parametrize("damage", [
    "local_backend", "same_domain", "unattested_independence", "stale_backup", "stale_snapshot",
    "stale_restore", "restore_before_snapshot", "unsupported_engine",
    "future_backup", "future_restore", "naive_timestamp", "wrong_receipt_run", "wrong_report_run",
    "wrong_manifest", "wrong_destination", "wrong_source", "partial_components", "unverified_bytes",
    "missing_report", "missing_policy", "missing_receipt", "failed_restore", "same_restore_host",
    "wrong_backup_domain", "missing_check", "failed_check", "empty_evidence",
])
def test_durable_admission_rejects_incomplete_local_stale_or_mismatched_evidence(admission, damage):
    policy, receipt, report, now = admission
    if damage == "local_backend":
        receipt["backend"] = "local"
    elif damage == "same_domain":
        policy["backup_failure_domain"] = "  PRIMARY-HOST-A  "
    elif damage == "unattested_independence":
        policy["independent_failure_domain_attested"] = False
    elif damage == "stale_backup":
        receipt["verified_at"] = (now - timedelta(seconds=301)).isoformat()
    elif damage == "stale_snapshot":
        receipt["snapshot_at"] = (now - timedelta(seconds=301)).isoformat()
    elif damage == "restore_before_snapshot":
        report["tested_at"] = (now - timedelta(seconds=31)).isoformat()
    elif damage == "unsupported_engine":
        receipt["engine"] = "sqlite"
    elif damage == "stale_restore":
        report["tested_at"] = (now - timedelta(seconds=3601)).isoformat()
    elif damage == "future_backup":
        receipt["verified_at"] = (now + timedelta(seconds=61)).isoformat()
    elif damage == "future_restore":
        report["tested_at"] = (now + timedelta(seconds=61)).isoformat()
    elif damage == "naive_timestamp":
        receipt["verified_at"] = now.replace(tzinfo=None).isoformat()
    elif damage == "wrong_receipt_run":
        receipt["run_id"] = "unrelated-run"
    elif damage == "wrong_report_run":
        report["run_id"] = "unrelated-run"
    elif damage == "wrong_manifest":
        report["manifest_sha256"] = "0" * 64
    elif damage == "wrong_destination":
        receipt["destination"]["bucket"] = "different-backup-bucket"
    elif damage == "wrong_source":
        receipt["source_failure_domain"] = "unrelated-source"
    elif damage == "partial_components":
        receipt["components"].pop()
    elif damage == "unverified_bytes":
        receipt["all_bytes_verified"] = False
    elif damage == "missing_report":
        report = {}
    elif damage == "missing_policy":
        policy = {}
    elif damage == "missing_receipt":
        receipt = {}
    elif damage == "failed_restore":
        report["passed"] = False
    elif damage == "same_restore_host":
        report["restore_failure_domain"] = "  PRIMARY-HOST-A  "
    elif damage == "wrong_backup_domain":
        report["backup_failure_domain"] = "unrelated-backup"
    elif damage == "missing_check":
        del report["checks"]["temporal_history"]
    elif damage == "failed_check":
        report["checks"]["pending_job_recovery"] = False
    elif damage == "empty_evidence":
        report["evidence_reference"] = " "
    with pytest.raises(ValueError):
        recovery.validate_durable_admission(policy, receipt, report, now=now)


@pytest.mark.parametrize("endpoint", [
    "http://backup.example.test", "https://localhost", "https://127.0.0.1",
    "https://[::1]", "https://user:synthetic@backup.example.test",
    "https://backup.example.test?token=synthetic", "https://backup.example.test#fragment", "",
])
def test_durable_admission_refuses_loopback_or_unsafe_endpoint_identity(admission, endpoint):
    policy, receipt, report, now = admission
    policy["destination"]["endpoint"] = endpoint
    receipt["destination"]["endpoint"] = endpoint
    with pytest.raises(ValueError, match="endpoint|HTTPS"):
        recovery.validate_durable_admission(policy, receipt, report, now=now)


def test_cli_preflight_writes_receipt_only_after_native_restore_admission(
    admission, uploaded, tmp_path, monkeypatch, capsys,
):
    policy, _, report, _ = admission
    policy_path, report_path = tmp_path / "policy.json", tmp_path / "restore.json"
    recovery.write_new(policy_path, policy)
    recovery.write_new(report_path, report)
    receipt_path = tmp_path / "preflight-receipt.json"
    configure_cli(monkeypatch, uploaded.store, "preflight", policy_path,
                  uploaded.manifest_hash, report_path, receipt_path)
    assert recovery_cli.main() == 0
    assert json.loads(capsys.readouterr().out)["admitted"] is True
    assert recovery.read_json(receipt_path)["all_bytes_verified"] is True
    report["checks"]["temporal_history"] = False
    report_path.write_text(json.dumps(report))
    failed_receipt = tmp_path / "failed-receipt.json"
    configure_cli(monkeypatch, uploaded.store, "preflight", policy_path,
                  uploaded.manifest_hash, report_path, failed_receipt)
    assert recovery_cli.main() == 1
    assert capsys.readouterr().out == ""
    assert not failed_receipt.exists()


@pytest.mark.parametrize("declared", ["true", "1", "yes", "TRUE", "true "])
def test_settings_fail_closed_when_durability_declared_without_evidence(monkeypatch, declared):
    monkeypatch.setenv("DURABLE_CONTINUOUS_RUN", declared)
    with pytest.raises(ValueError, match="requires policy"):
        Settings(database_url="sqlite://", api_tokens={})


def test_settings_do_not_require_durable_admission_for_undeclared_development(monkeypatch):
    monkeypatch.setenv("DURABLE_CONTINUOUS_RUN", "false")
    monkeypatch.setenv("DURABILITY_POLICY", "/does/not/exist")
    assert recovery.check_declared_durability() is None
    assert Settings(database_url="sqlite://", api_tokens={}).database_url == "sqlite://"


@pytest.mark.parametrize("damage", [None, "stale", "wrong_run", "missing_file", "malformed_json", "partial_restore"])
def test_settings_validate_declared_evidence_files(admission, tmp_path, monkeypatch, damage):
    policy, receipt, report, now = admission
    if damage == "stale":
        receipt["verified_at"] = (now - timedelta(seconds=301)).isoformat()
    elif damage == "wrong_run":
        receipt["run_id"] = "unrelated-run"
    elif damage == "partial_restore":
        report["checks"].pop("pending_job_recovery")
    monkeypatch.setenv("DURABLE_CONTINUOUS_RUN", "true")
    for variable, data in (
        ("DURABILITY_POLICY", policy), ("DURABILITY_RECEIPT", receipt),
        ("DURABILITY_RESTORE_REPORT", report),
    ):
        path = tmp_path / f"{variable}.json"
        recovery.write_new(path, data)
        monkeypatch.setenv(variable, str(path))
    if damage == "missing_file":
        (tmp_path / "DURABILITY_RESTORE_REPORT.json").unlink()
    elif damage == "malformed_json":
        (tmp_path / "DURABILITY_RECEIPT.json").write_text("not json")
    if damage is None:
        settings = Settings(database_url="postgresql+psycopg://synthetic/database", inline_worker=False, api_tokens={})
        assert settings.inline_worker is False
    else:
        with pytest.raises((ValueError, OSError)):
            Settings(database_url="postgresql+psycopg://synthetic/database", inline_worker=False, api_tokens={})


@pytest.mark.parametrize("declared", ["tru", "enabled", "2"])
def test_settings_reject_misspelled_durability_declaration(monkeypatch, declared):
    monkeypatch.setenv("DURABLE_CONTINUOUS_RUN", declared)
    with pytest.raises(ValueError, match="explicit true/false"):
        Settings(database_url="sqlite://", api_tokens={})


@pytest.mark.parametrize("database_url,inline_worker", [
    ("sqlite://", False), ("postgresql+psycopg://synthetic/database", True),
])
def test_declared_durable_settings_require_postgresql_and_temporal(
    admission, tmp_path, monkeypatch, database_url, inline_worker,
):
    policy, receipt, report, _ = admission
    monkeypatch.setenv("DURABLE_CONTINUOUS_RUN", "true")
    for variable, data in (
        ("DURABILITY_POLICY", policy), ("DURABILITY_RECEIPT", receipt),
        ("DURABILITY_RESTORE_REPORT", report),
    ):
        path = tmp_path / f"{variable}.json"
        recovery.write_new(path, data)
        monkeypatch.setenv(variable, str(path))
    with pytest.raises(ValueError, match="PostgreSQL.*Temporal"):
        Settings(database_url=database_url, inline_worker=inline_worker, api_tokens={})


@pytest.mark.parametrize("with_existing_file", [False, True])
def test_download_race_cannot_replace_destination_or_issue_receipt(
    uploaded, tmp_path, monkeypatch, capsys, with_existing_file,
):
    destination, receipt_path = tmp_path / "download", tmp_path / "receipt.json"
    original_install = recovery.install_new_directory
    raced_inode = None

    def install_with_racing_destination(source, target):
        nonlocal raced_inode
        target.mkdir()
        raced_inode = target.stat().st_ino
        if with_existing_file:
            (target / "existing").write_text("concurrently created data")
        original_install(source, target)

    monkeypatch.setattr(recovery, "install_new_directory", install_with_racing_destination)
    configure_cli(monkeypatch, uploaded.store, "download", uploaded.manifest_hash, destination, receipt_path)
    assert recovery_cli.main() == 1
    assert capsys.readouterr().out == ""
    assert destination.stat().st_ino == raced_inode
    assert not receipt_path.exists()
    assert not list(tmp_path.glob(".eh-recovery-*"))
    assert sorted(path.name for path in destination.iterdir()) == (["existing"] if with_existing_file else [])
    if with_existing_file:
        assert (destination / "existing").read_text() == "concurrently created data"


def test_download_and_receipt_are_private_even_with_permissive_umask(
    uploaded, tmp_path, monkeypatch, capsys,
):
    destination, receipt_path = tmp_path / "download", tmp_path / "receipt.json"
    configure_cli(monkeypatch, uploaded.store, "download", uploaded.manifest_hash, destination, receipt_path)
    previous_umask = os.umask(0o022)
    try:
        assert recovery_cli.main() == 0
    finally:
        os.umask(previous_umask)
    assert json.loads(capsys.readouterr().out)["all_bytes_verified"] is True
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    for path in destination.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("missing_env", ["RECOVERY_S3_BUCKET", "RECOVERY_S3_PREFIX"])
def test_cli_requires_explicit_recovery_destination_before_transport(
    uploaded, tmp_path, monkeypatch, capsys, missing_env,
):
    receipt_path = tmp_path / "receipt.json"
    configure_cli(monkeypatch, uploaded.store, "verify", uploaded.manifest_hash, receipt_path)
    monkeypatch.delenv(missing_env)
    before = len(uploaded.client.events)
    with pytest.raises(SystemExit) as error:
        recovery_cli.main()
    assert error.value.code == 2
    assert "Explicit RECOVERY_S3_BUCKET and RECOVERY_S3_PREFIX" in capsys.readouterr().err
    assert len(uploaded.client.events) == before
    assert not receipt_path.exists()


def test_metadata_file_read_is_bounded(tmp_path, monkeypatch):
    metadata = tmp_path / "oversize.json"
    metadata.write_text('{"larger": "than the test bound"}')
    monkeypatch.setattr(recovery, "MAX_MANIFEST_BYTES", 10)
    with pytest.raises(ValueError, match="size bound"):
        recovery.read_json(metadata)


@pytest.mark.parametrize("service", ["worker", "dispatcher"])
@pytest.mark.parametrize("damage", ["missing_evidence", "missing_file", "malformed_metadata"])
@pytest.mark.asyncio
async def test_temporal_processes_fail_durability_admission_before_connecting(
    tmp_path, monkeypatch, service, damage,
):
    module = importlib.import_module(f"backend.{service}")
    # BaseException escapes the processes' reconnect loops if admission regresses.
    connect = AsyncMock(side_effect=SystemExit("Unexpected Temporal connection"))
    monkeypatch.setattr(module.Client, "connect", connect)
    monkeypatch.setenv("DURABLE_CONTINUOUS_RUN", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic/database")
    monkeypatch.setenv("INLINE_WORKER", "false")
    monkeypatch.setenv("LOCAL_WORKER", "false")
    if damage != "missing_evidence":
        path = tmp_path / "admission.json"
        if damage == "malformed_metadata":
            path.write_text("{malformed")
        for variable in ("DURABILITY_POLICY", "DURABILITY_RECEIPT", "DURABILITY_RESTORE_REPORT"):
            monkeypatch.setenv(variable, str(path))
    with pytest.raises((ValueError, FileNotFoundError)):
        await module.main()
    connect.assert_not_called()


@pytest.mark.parametrize("contents", [
    b"DB_PASSWORD=synthetic-placeholder",
    b'{"api_key": "synthetic-placeholder"}',
    b"AWS_SECRET_ACCESS_KEY=synthetic-placeholder",
    b"TOKEN: synthetic-placeholder",
    b"-----BEGIN PRIVATE KEY-----\nsynthetic-placeholder",
    b"-----BEGIN RSA PRIVATE KEY-----\nsynthetic-placeholder",
    b"AKIA" + b"A" * 16,
    b"github_pat_" + b"a" * 24,
    b"ghp_" + b"a" * 24,
])
def test_configuration_credential_patterns_are_rejected_before_transmission(
    native_set, remote, tmp_path, monkeypatch, contents,
):
    spec, _ = native_set
    client, store = remote
    # A reviewed configuration file is bounded below the production chunk size.
    monkeypatch.setattr(recovery, "CHUNK_BYTES", 8 * 1024 * 1024)
    (tmp_path / "native/configuration/operator.json").write_bytes(b"Reviewed config\n" + contents)
    with pytest.raises(ValueError, match="credential material"):
        recovery.upload_set(spec, store)
    assert client.puts == []
    assert client.objects == {}


def test_oversized_configuration_is_rejected_before_transmission(native_set, remote, tmp_path):
    spec, _ = native_set
    client, store = remote
    (tmp_path / "native/configuration/operator.json").write_bytes(b"X" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="limited to 1 MiB"):
        recovery.upload_set(spec, store)
    assert client.puts == []
    assert client.objects == {}


@pytest.mark.parametrize("ancestor", ["component_root", "nested_directory"])
def test_source_ancestor_swap_to_symlink_never_transmits_outside_bytes(
    native_set, remote, tmp_path, monkeypatch, ancestor,
):
    spec, _ = native_set
    client, store = remote
    configuration = tmp_path / "native/configuration"
    if ancestor == "nested_directory":
        (configuration / "reviewed").mkdir()
        (configuration / "operator.json").rename(configuration / "reviewed/operator.json")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_bytes = b"SYNTHETIC_PRIVATE_OUTSIDE_BYTES"
    (outside / "operator.json").write_bytes(outside_bytes)
    original_open = recovery.open_snapshot_file
    intercepted = []

    def swap_ancestor_after_stat(root, relative, expected):
        intercepted.append((root, relative))
        assert Path(root) == configuration
        target = Path(root) if ancestor == "component_root" else Path(root) / "reviewed"
        target.rename(target.with_name(target.name + "-original"))
        target.symlink_to(outside, target_is_directory=True)
        return original_open(root, relative, expected)

    monkeypatch.setattr(recovery, "open_snapshot_file", swap_ancestor_after_stat)
    with pytest.raises(OSError):
        recovery.upload_set(spec, store)
    assert len(intercepted) == 1
    assert client.puts == []
    assert client.objects == {}
    assert (outside / "operator.json").read_bytes() == outside_bytes


def test_replaced_regular_file_is_rejected_before_transmission(native_set, remote, tmp_path, monkeypatch):
    spec, _ = native_set
    client, store = remote
    original_open = recovery.open_snapshot_file

    def replace_leaf_after_stat(root, relative, expected):
        source = Path(root) / relative
        replacement = tmp_path / "replacement-file"
        replacement.write_bytes(b"X" * expected.st_size)
        replacement.replace(source)
        return original_open(root, relative, expected)

    monkeypatch.setattr(recovery, "open_snapshot_file", replace_leaf_after_stat)
    with pytest.raises(ValueError, match="changed before opening"):
        recovery.upload_set(spec, store)
    assert client.puts == []
    assert client.objects == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"), reason="POSIX FIFO test")
def test_source_leaf_swap_to_fifo_is_rejected_without_blocking(
    native_set, remote, monkeypatch,
):
    spec, _ = native_set
    client, store = remote
    original_open = recovery.open_snapshot_file

    def replace_leaf_with_fifo_after_stat(root, relative, expected):
        source = Path(root) / relative
        source.unlink()
        os.mkfifo(source, 0o600)
        return original_open(root, relative, expected)

    def fail_if_blocked(signum, frame):
        raise TimeoutError("Recovery upload blocked on a swapped FIFO")

    monkeypatch.setattr(recovery, "open_snapshot_file", replace_leaf_with_fifo_after_stat)
    previous_handler = signal.signal(signal.SIGALRM, fail_if_blocked)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        with pytest.raises(ValueError, match="changed before opening"):
            recovery.upload_set(spec, store)
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)
    assert client.puts == []
    assert client.objects == {}
