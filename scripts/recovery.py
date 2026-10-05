"""Prepare/verify off-host recovery sets using an already configured S3 account.

Does not create buckets/credentials or perform a native database restore.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.recovery import (
    read_json,
    retrieve_set,
    upload_set,
    validate_durable_admission,
    write_new,
)
from backend.storage import S3ContentStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    upload = sub.add_parser("upload")
    upload.add_argument("spec", type=Path)
    upload.add_argument("receipt", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("manifest_sha256")
    verify.add_argument("receipt", type=Path)
    download = sub.add_parser("download")
    download.add_argument("manifest_sha256")
    download.add_argument("destination", type=Path)
    download.add_argument("receipt", type=Path)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("policy", type=Path)
    preflight.add_argument("manifest_sha256")
    preflight.add_argument("restore_report", type=Path)
    preflight.add_argument("receipt", type=Path)
    args = parser.parse_args()
    if args.receipt.exists():
        parser.error("Receipt path must not exist; never overwrite verification history")
    # Uses the same explicit credentials/HTTPS/conditional-write protections as raw storage.
    # A separate approved backup bucket/prefix must be selected deliberately.
    bucket = os.getenv("RECOVERY_S3_BUCKET")
    prefix = os.getenv("RECOVERY_S3_PREFIX")
    if not bucket or not prefix:
        parser.error("Explicit RECOVERY_S3_BUCKET and RECOVERY_S3_PREFIX are required")
    try:
        store = S3ContentStore(bucket, prefix=prefix)
        manifest_hash = upload_set(read_json(args.spec), store) if args.command == "upload" else args.manifest_sha256
        receipt = retrieve_set(store, manifest_hash, getattr(args, "destination", None))
        result = {"manifest_sha256": manifest_hash, "all_bytes_verified": True, "native_restore_tested": False}
        if args.command == "preflight":
            result = validate_durable_admission(read_json(args.policy), receipt, read_json(args.restore_report))
        write_new(args.receipt, receipt)
        print(json.dumps(result))
    except Exception:  # noqa: BLE001 - CLI boundary redacts cloud/provider error details
        # Do not print cloud errors or specs that might contain credentials/private paths.
        print("Recovery verification failed; no success receipt issued. Check input metadata and configured destination.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
