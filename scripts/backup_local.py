#!/usr/bin/env python3
"""Portable SQLite + immutable-local-objects snapshots, with verified fresh restore.

Production PostgreSQL/Temporal backups use their native tools; see operations.md.
This utility never overwrites an existing restore destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def backup(database: Path, objects: Path, output: Path):
    if not database.is_file():
        raise ValueError("SQLite database does not exist")
    if output.exists():
        raise ValueError("Refusing to overwrite an existing backup")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        snapshot = Path(temp) / "workspace.sqlite"
        with (
            sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as source,
            sqlite3.connect(snapshot) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
        manifest = {"format": "evidenceharbor-local-backup-v1", "files": []}
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            files = [("workspace.sqlite", snapshot)]
            if objects.exists():
                files += [
                    ("objects/" + str(path.relative_to(objects)), path)
                    for path in objects.rglob("*")
                    if path.is_file() and not path.is_symlink()
                ]
            for name, path in files:
                hasher = hashlib.sha256()
                size = 0
                with path.open("rb") as source, archive.open(name, "w") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
                manifest["files"].append({"path": name, "sha256": hasher.hexdigest(), "bytes": size})
            archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2))
    return manifest


def restore(archive_path: Path, destination: Path):
    if destination.exists():
        raise ValueError("Restore target must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        zipfile.ZipFile(archive_path) as archive,
        tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary,
    ):
        staging = Path(temporary) / "verified"
        staging.mkdir()
        if archive.getinfo("backup-manifest.json").file_size > 16 * 1024 * 1024:
            raise ValueError("Backup manifest is too large")
        manifest = json.loads(archive.read("backup-manifest.json"))
        if manifest.get("format") != "evidenceharbor-local-backup-v1":
            raise ValueError("Unknown backup format")
        total, seen = 0, set()
        for item in manifest["files"]:
            path = Path(item["path"])
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or (str(path) != "workspace.sqlite" and path.parts[0] != "objects")
                or str(path) in seen
                or len(path.parts) > 8
            ):
                raise ValueError("Unsafe backup path")
            seen.add(str(path))
            info = archive.getinfo(item["path"])
            total += info.file_size
            if total > 10 * 1024**3 or info.file_size != item["bytes"]:
                raise ValueError("Backup size bounds exceeded")
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            size = 0
            with archive.open(info) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > info.file_size:
                        raise ValueError("Backup size bounds exceeded")
                    hasher.update(chunk)
                    output.write(chunk)
            if size != item["bytes"] or hasher.hexdigest() != item["sha256"]:
                raise ValueError("Backup hash mismatch")
        if "workspace.sqlite" not in seen:
            raise ValueError("Backup database missing")
        with sqlite3.connect(f"file:{staging / 'workspace.sqlite'}?mode=ro", uri=True) as restored:
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database integrity check failed")
        if destination.exists():
            raise ValueError("Restore target must not exist")
        os.rename(staging, destination)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("database", type=Path)
    create.add_argument("objects", type=Path)
    create.add_argument("output", type=Path)
    recover = sub.add_parser("restore")
    recover.add_argument("archive", type=Path)
    recover.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = (
        backup(args.database, args.objects, args.output)
        if args.command == "create"
        else restore(args.archive, args.destination)
    )
    print(json.dumps({"verified_files": len(result["files"]), "status": "complete"}))


if __name__ == "__main__":
    main()
