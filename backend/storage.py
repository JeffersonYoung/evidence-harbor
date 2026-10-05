"""Immutable, SHA-256 addressed raw objects. Reads always verify their digest."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_STORED_BYTES = 100 * 1024 * 1024


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    key: str
    size: int
    backend: str

    @property
    def relative_path(self) -> str:
        return self.key


def object_key(digest: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise StorageError("Invalid SHA-256 object identifier")
    return f"raw/{digest[:2]}/{digest}"


class ContentStore(Protocol):
    def put(self, data: bytes, media_type: str = "application/octet-stream") -> StoredObject: ...
    def get(self, digest: str) -> bytes: ...
    def verify(self, digest: str) -> bool: ...


class LocalContentStore:
    """Atomic create-if-absent installation; an existing object is never overwritten."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, digest: str) -> Path:
        path = self.root / object_key(digest)
        if not path.resolve().is_relative_to(self.root):
            raise StorageError("Object path escapes storage root")
        return path

    def put(self, data: bytes, media_type: str = "application/octet-stream") -> StoredObject:
        if not isinstance(data, bytes):
            raise TypeError("Raw storage accepts bytes only")
        digest = hashlib.sha256(data).hexdigest()
        destination = self._path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix=".raw-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o444)
            try:
                # link is atomic and, unlike rename/replace, cannot overwrite an object.
                os.link(tmp, destination)
            except FileExistsError:
                pass
            directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(tmp)
        if self.get(digest) != data:
            raise StorageError("Existing content-addressed object failed verification")
        return StoredObject(digest, object_key(digest), len(data), "local")

    def get(self, digest: str) -> bytes:
        path = self._path(digest)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                data = stream.read(MAX_STORED_BYTES + 1)
                if len(data) > MAX_STORED_BYTES:
                    raise StorageError("Raw object exceeds the read limit")
        except OSError as exc:
            raise StorageError("Raw object unavailable") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise StorageError("Raw object digest mismatch")
        return data

    def verify(self, digest: str) -> bool:
        try:
            self.get(digest)
            return True
        except StorageError:
            return False


class S3ContentStore:
    """S3 adapter requires conditional writes; there is no unsafe overwrite fallback.

    Enable bucket versioning/Object Lock at deployment for administrator-level retention.
    This adapter guarantees immutability against application writes and verifies reads.
    """

    def __init__(self, bucket: str, *, client=None, prefix: str = ""):
        if not bucket:
            raise StorageError("An S3 bucket is required")
        if client is None:
            import boto3
            from botocore.config import Config

            # Explicit keys avoid accidental instance-metadata credential discovery.
            access = os.environ.get("S3_ACCESS_KEY_ID")
            secret = os.environ.get("S3_SECRET_ACCESS_KEY")
            if not access or not secret:
                raise StorageError("Explicit S3 credentials are required")
            endpoint = os.environ.get("S3_ENDPOINT_URL") or None
            if endpoint:
                import ipaddress
                from urllib.parse import urlsplit

                parsed = urlsplit(endpoint)
                if (
                    parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or not parsed.hostname
                ):
                    raise StorageError("S3 endpoint must not contain credentials, query or fragment")
                try:
                    local = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    local = parsed.hostname == "localhost"
                if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
                    raise StorageError("S3 endpoints require HTTPS except literal loopback test services")
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=os.environ.get("S3_REGION", "us-east-1"),
                aws_access_key_id=access,
                aws_secret_access_key=secret,
                aws_session_token=os.environ.get("S3_SESSION_TOKEN") or None,
                config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2}, proxies={}),
            )
        self.client, self.bucket = client, bucket
        self.prefix = prefix.strip("/")

    def _key(self, digest: str) -> str:
        return "/".join(filter(None, (self.prefix, object_key(digest))))

    def put(self, data: bytes, media_type: str = "application/octet-stream") -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        key = self._key(digest)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=media_type,
                IfNoneMatch="*",
                Metadata={"sha256": digest},
            )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise StorageError("S3 conditional object creation failed") from exc
        if self.get(digest) != data:
            raise StorageError("S3 object failed read-after-write verification")
        return StoredObject(digest, key, len(data), "s3")

    def get(self, digest: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(digest))
            stream = response["Body"]
            try:
                data = stream.read(MAX_STORED_BYTES + 1)
                if len(data) > MAX_STORED_BYTES:
                    raise StorageError("S3 object exceeds the read limit")
            finally:
                stream.close()
        except Exception as exc:
            raise StorageError("S3 raw object unavailable") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise StorageError("S3 raw object digest mismatch")
        return data

    def verify(self, digest: str) -> bool:
        try:
            self.get(digest)
            return True
        except StorageError:
            return False


def get_store() -> ContentStore:
    backend = os.environ.get("STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        return S3ContentStore(os.environ.get("S3_BUCKET", ""), prefix=os.environ.get("S3_PREFIX", ""))
    if backend != "local":
        raise StorageError("Unknown storage backend")
    return LocalContentStore(
        os.environ.get("RAW_STORAGE_DIR", os.environ.get("DATA_DIR", "./data") + "/objects")
    )
