"""Bounded recovery sets and explicit durability admission, not a native database backup engine.

Inputs are operator-created, quiesced native backups. A remote checksum receipt
proves retrieved bytes, never independent infrastructure or a successful DB restore.
"""
from __future__ import annotations

import ctypes
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .storage import S3ContentStore

FORMAT = "evidenceharbor-recovery-set-v1"
CHUNK_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 100000
MAX_TOTAL_BYTES = 100 * 1024**3
COMPONENTS = {"research_database", "temporal_persistence", "temporal_visibility", "objects", "configuration"}
SHA = re.compile(r"[a-f0-9]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def timestamp():
    return datetime.now(UTC).isoformat()


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Recovery metadata exceeds size bound")
    return json.loads(raw)


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f"A bounded {label} is required")
    return value


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid recovery identifier")
    return value


def safe_relative(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        raise ValueError("Unsafe recovery path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")) or len(path.parts) > 16:
        raise ValueError("Unsafe recovery path")
    return path


def validate_header(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("Recovery metadata must be an object")  # noqa: TRY004 - untrusted JSON value
    if manifest.get("format") != FORMAT or manifest.get("engine") != "postgresql-temporal":
        raise ValueError("Only complete PostgreSQL/Temporal recovery sets are supported")
    identifier(manifest.get("run_id"))
    identifier(manifest.get("snapshot_id"))
    nonempty(manifest.get("source_failure_domain"), "source failure domain")
    if manifest.get("consistency") != "writers-stopped":
        raise ValueError("This tool requires a quiesced recovery point with all writers stopped")
    for field in ("operator", "quiescence_evidence", "schema_revision", "release_commit"):
        nonempty(manifest.get(field), field)
    age_seconds(manifest.get("snapshot_at"), datetime.now(UTC))
    if manifest.get("configuration_secrets_excluded") is not True:
        raise ValueError("Configuration must exclude credentials; preserve secrets separately")


def validate_manifest(manifest):
    validate_header(manifest)
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError("Recovery file count outside bounds")
    seen, components, total = set(), set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Recovery file records must be objects")  # noqa: TRY004 - untrusted JSON value
        path = str(safe_relative(item.get("path")))
        component = item.get("component")
        if component not in COMPONENTS or not path.startswith(component + "/") or path in seen:
            raise ValueError("Invalid or duplicate recovery component path")
        seen.add(path)
        components.add(component)
        size = item.get("bytes")
        if type(size) is not int or size < 0 or not SHA.fullmatch(str(item.get("sha256", ""))):
            raise ValueError("Invalid recovery file digest or size")
        total += size
        chunks = item.get("chunks")
        if not isinstance(chunks, list) or len(chunks) != (size + CHUNK_BYTES - 1) // CHUNK_BYTES:
            raise ValueError("Invalid recovery chunk count")
        if any(not SHA.fullmatch(str(value)) for value in chunks):
            raise ValueError("Invalid recovery chunk digest")
    if components != COMPONENTS or total > MAX_TOTAL_BYTES:
        raise ValueError("Incomplete recovery set or total size limit exceeded")
    return manifest


def open_snapshot_file(root, relative, expected):
    """Pin every ancestor without following symlinks before any bytes leave the host."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Safe recovery upload requires descriptor-relative no-follow filesystem support")
    absolute = Path(root).absolute()
    if ".." in absolute.parts:
        raise ValueError("Recovery component roots must not contain parent traversal")
    descriptors = []
    file_fd = None
    try:
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in (*absolute.parts[1:], *PurePosixPath(relative).parts[:-1]):
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        file_fd = os.open(
            PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        actual = os.fstat(file_fd)
        if not stat.S_ISREG(actual.st_mode) or (
            actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns
        ) != (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns):
            raise ValueError("Recovery input changed before opening; no bytes transmitted")
        result, file_fd = file_fd, None
        return result
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def upload_set(spec, store):
    """Commit the manifest last, after immutable chunks have been read back.

    Failed uploads leave harmless unreferenced chunks, never a success receipt.
    No automatic native dump, bucket creation, credentials, retention change or GC.
    """
    validate_header(spec)
    if not isinstance(store, S3ContentStore):
        raise TypeError("Off-host transport requires the explicit S3 adapter")
    roots = spec.get("components", {})
    if not isinstance(roots, dict) or set(roots) != COMPONENTS:
        raise ValueError("All five recovery components are required")
    manifest = {key: spec[key] for key in (
        "run_id", "snapshot_id", "snapshot_at", "engine", "source_failure_domain", "consistency", "operator",
        "quiescence_evidence", "schema_revision", "release_commit", "configuration_secrets_excluded",
    )}
    manifest.update(format=FORMAT, created_at=timestamp(), files=[])
    total = 0
    for component in sorted(COMPONENTS):
        root = Path(roots[component])
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Each recovery component must be a real directory")
        paths = []
        for path in root.rglob("*"):
            paths.append(path)
            if len(paths) > MAX_FILES * 2:
                raise ValueError("Recovery directory entry count exceeds bounds")
        paths.sort()
        if any(p.is_symlink() for p in paths):
            raise ValueError("Symlinks are not allowed in recovery components")
        for source in paths:
            if source.is_dir():
                continue
            if not source.is_file():
                raise ValueError("Only regular recovery files are supported")
            relative = source.relative_to(root).as_posix()
            safe_relative(relative)
            if component == "configuration" and (
                source.name.startswith(".env") or source.suffix.lower() in {".pem", ".key", ".p12"}
            ):
                raise ValueError("Do not bundle credential files as operator configuration")
            before = source.stat()
            if component == "configuration" and before.st_size > 1024 * 1024:
                raise ValueError("Each reviewed configuration file is limited to 1 MiB")
            total += before.st_size
            if total > MAX_TOTAL_BYTES or len(manifest["files"]) >= MAX_FILES:
                raise ValueError("Recovery set exceeds resource bounds")
            hasher, chunks, size = hashlib.sha256(), [], 0
            fd = open_snapshot_file(root, relative, before)
            with os.fdopen(fd, "rb") as stream:
                while data := stream.read(CHUNK_BYTES):
                    if component == "configuration" and re.search(
                        rb"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[A-Z0-9]{16}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|(?:[\"\']?(?:[A-Z0-9_]*PASSWORD|[A-Z0-9_]*SECRET|API_KEY|ACCESS_KEY|TOKEN)[\"\']?)\s*[:=])",
                        data,
                    ):
                        raise ValueError("Configuration resembles credential material; review and remove it")
                    hasher.update(data)
                    size += len(data)
                    if size > before.st_size:
                        raise ValueError("Recovery input changed during upload")
                    chunks.append(store.put(data).sha256)
            after = source.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns
            ) or size != before.st_size:
                raise ValueError("Recovery input changed during upload")
            manifest["files"].append({
                "component": component, "path": component + "/" + relative,
                "bytes": size, "sha256": hasher.hexdigest(), "chunks": chunks,
            })
    validate_manifest(manifest)
    raw = canonical(manifest)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Recovery manifest exceeds resource bounds")
    manifest_hash = store.put(raw, "application/json").sha256
    return manifest_hash


def install_new_directory(source, destination):
    """Atomic no-replace installation; unsupported platforms fail closed."""
    if os.name == "nt":
        # Windows rename refuses any existing destination.
        os.rename(source, destination)
        return
    if sys.platform != "linux":
        raise ValueError("Atomic no-replace download installation currently requires Linux or Windows")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ValueError("Atomic no-replace rename is unavailable on this host")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "Atomic no-replace recovery installation failed")


def retrieve_set(store, manifest_hash, destination=None):
    """Read and hash every component. Optional fresh-directory download, no extraction.

    A dump or archive is downloaded as an opaque file; no pg_restore, shell or
    archive extraction is triggered by remote metadata.
    """
    if not isinstance(store, S3ContentStore) or not SHA.fullmatch(str(manifest_hash)):
        raise ValueError("Explicit S3 adapter and manifest SHA-256 required")
    raw = store.get(manifest_hash)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Recovery manifest exceeds resource bounds")
    manifest = validate_manifest(json.loads(raw))
    target = Path(destination) if destination is not None else None
    if target is not None:
        if target.exists() or target.is_symlink():
            raise ValueError("Restore download destination must not exist")
        target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".eh-recovery-", dir=target.parent if target else None) as temp:
        stage = Path(temp) / "verified"
        stage.mkdir(mode=0o700)
        for item in manifest["files"]:
            hasher, size = hashlib.sha256(), 0
            output = None
            if target is not None:
                path = stage / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                output = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
            try:
                for index, chunk_hash in enumerate(item["chunks"]):
                    data = store.get(chunk_hash)
                    expected = min(CHUNK_BYTES, item["bytes"] - index * CHUNK_BYTES)
                    if len(data) != expected:
                        raise ValueError("Recovery chunk size mismatch")
                    hasher.update(data)
                    size += len(data)
                    if output:
                        output.write(data)
                if size != item["bytes"] or hasher.hexdigest() != item["sha256"]:
                    raise ValueError("Recovery file hash mismatch")
            finally:
                if output:
                    output.close()
        if target is not None:
            write_new(stage / "recovery-manifest.json", manifest)
            if target.exists() or target.is_symlink():
                raise ValueError("Restore download destination must not exist")
            install_new_directory(stage, target)
    return {
        "format": "evidenceharbor-remote-receipt-v1", "manifest_sha256": manifest_hash,
        "run_id": manifest["run_id"], "snapshot_id": manifest["snapshot_id"],
        "snapshot_at": manifest["snapshot_at"],
        "source_failure_domain": manifest["source_failure_domain"],
        "engine": manifest["engine"], "verified_at": timestamp(), "backend": "s3",
        "destination": {"bucket": store.bucket, "prefix": store.prefix,
                        "endpoint": store.client.meta.endpoint_url},
        "verified_files": len(manifest["files"]),
        "verified_bytes": sum(item["bytes"] for item in manifest["files"]),
        "components": sorted(COMPONENTS), "all_bytes_verified": True,
        "native_restore_tested": False,
    }


def age_seconds(value, now):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        age = (now - parsed).total_seconds()
    except (TypeError, ValueError):
        raise ValueError("Invalid recovery timestamp") from None
    if age < -60:
        raise ValueError("Recovery evidence is dated in the future")
    return max(age, 0)


def validate_durable_admission(policy, receipt, restore_report, *, now=None):
    """Validate explicit operator policy and attestations; not an infrastructure audit."""
    now = now or datetime.now(UTC)
    if not all(isinstance(value, dict) for value in (policy, receipt, restore_report)):
        raise ValueError("Durability policy, receipt and restore report must be objects")
    if policy.get("format") != "evidenceharbor-durability-policy-v1":
        raise ValueError("Explicit durability policy required")
    identifier(policy.get("run_id"))
    source = nonempty(policy.get("source_failure_domain"), "source failure domain")
    backup = nonempty(policy.get("backup_failure_domain"), "backup failure domain")
    if source.strip().casefold() == backup.strip().casefold():
        raise ValueError("Same-host/same-failure-domain backups cannot admit a durable run")
    for field in ("operator", "independence_evidence"):
        nonempty(policy.get(field), field)
    if policy.get("independent_failure_domain_attested") is not True:
        raise ValueError("Operator must attest independently recoverable off-host storage")
    for field in ("maximum_backup_age_seconds", "maximum_restore_test_age_seconds"):
        if type(policy.get(field)) is not int or not 60 <= policy[field] <= 31 * 86400:
            raise ValueError("Explicit bounded backup/restore freshness policy required")
    if (
        receipt.get("format") != "evidenceharbor-remote-receipt-v1"
        or receipt.get("backend") != "s3" or receipt.get("all_bytes_verified") is not True
        or receipt.get("engine") != "postgresql-temporal"
        or receipt.get("components") != sorted(COMPONENTS)
        or receipt.get("run_id") != policy["run_id"]
        or receipt.get("source_failure_domain") != source
        or not isinstance(receipt.get("destination"), dict)
        or receipt.get("destination") != policy.get("destination")
        or not SHA.fullmatch(str(receipt.get("manifest_sha256", "")))
        or not isinstance(receipt.get("verified_files"), int) or receipt["verified_files"] < 5
    ):
        raise ValueError("A matching complete remote verification receipt is required")
    endpoint = urlsplit(receipt["destination"].get("endpoint", ""))
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError("Durable admission requires a credential-free HTTPS destination")
    try:
        loopback = ipaddress.ip_address(endpoint.hostname).is_loopback
    except ValueError:
        loopback = endpoint.hostname.lower() == "localhost"
    if loopback or endpoint.query or endpoint.fragment:
        raise ValueError("Loopback or unsafe endpoint cannot establish durable off-host admission")
    if max(age_seconds(receipt.get("verified_at"), now), age_seconds(receipt.get("snapshot_at"), now)) > policy["maximum_backup_age_seconds"]:
        raise ValueError("Remote backup verification is stale")
    if (
        restore_report.get("format") != "evidenceharbor-restore-attestation-v1"
        or restore_report.get("run_id") != policy["run_id"]
        or restore_report.get("manifest_sha256") != receipt["manifest_sha256"]
        or restore_report.get("backup_failure_domain") != backup
        or str(restore_report.get("restore_failure_domain", "")).strip().casefold() == source.strip().casefold()
        or restore_report.get("passed") is not True
    ):
        raise ValueError("Independent native restore attestation for this exact set is required")
    for field in ("operator", "restore_failure_domain", "evidence_reference"):
        nonempty(restore_report.get(field), field)
    required = {"research_database", "temporal_history", "raw_objects", "evidence_locators", "pending_job_recovery"}
    if not isinstance(restore_report.get("checks"), dict) or set(restore_report["checks"]) != required or any(
        value is not True for value in restore_report["checks"].values()
    ):
        raise ValueError("Native database, Temporal, objects, evidence and resume checks must all pass")
    if age_seconds(restore_report.get("tested_at"), now) > age_seconds(receipt.get("snapshot_at"), now):
        raise ValueError("Restore test cannot predate its snapshot")
    if age_seconds(restore_report.get("tested_at"), now) > policy["maximum_restore_test_age_seconds"]:
        raise ValueError("Native restore attestation is stale")
    return {"admitted": True, "run_id": policy["run_id"], "manifest_sha256": receipt["manifest_sha256"],
            "basis": "verified remote bytes plus operator independence/native-restore attestations"}


def check_declared_durability():
    """Startup gate only when the operator explicitly declares a durable continuous run."""
    declaration = os.getenv("DURABLE_CONTINUOUS_RUN", "").strip().lower()
    if declaration in {"", "false", "0", "no"}:
        return None
    if declaration not in {"true", "1", "yes"}:
        raise ValueError("DURABLE_CONTINUOUS_RUN must be an explicit true/false value")
    paths = [os.getenv(key) for key in ("DURABILITY_POLICY", "DURABILITY_RECEIPT", "DURABILITY_RESTORE_REPORT")]
    if not all(paths):
        raise ValueError("Durable continuous run requires policy, fresh remote receipt and native restore report")
    return validate_durable_admission(*(read_json(path) for path in paths))
