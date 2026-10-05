#!/usr/bin/env python3
"""Operator/empirical CLI sharing the exact REST domain path used by UI and MCP.

Secrets are read from environment, never CLI flags or result files.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description="EvidenceHarbor CLI")
    parser.add_argument("--api", default=os.getenv("EVIDENCEHARBOR_API_URL", "http://127.0.0.1:8000"))
    commands = parser.add_subparsers(dest="command", required=True)
    project = commands.add_parser("project")
    project.add_argument("name")
    project.add_argument("--description", default="")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("project_id")
    ingest.add_argument("--url")
    ingest.add_argument("--file", type=Path)
    ingest.add_argument("--title", default="")
    ingest.add_argument("--original-url")
    ingest.add_argument("--wait", action="store_true")
    search = commands.add_parser("search")
    search.add_argument("project_id")
    search.add_argument("query")
    context = commands.add_parser("context")
    context.add_argument("project_id")
    read = commands.add_parser("read")
    read.add_argument("document_id")
    evidence = commands.add_parser("evidence")
    evidence.add_argument("project_id")
    evidence.add_argument("block_id")
    evidence.add_argument("--quote-file", type=Path, required=True)
    proposal = commands.add_parser("propose")
    proposal.add_argument("json_file", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("proposal_id")
    publish.add_argument("--expected-version", type=int)
    export = commands.add_parser("export")
    export.add_argument("project_id")
    export.add_argument("output", type=Path)
    raw = commands.add_parser("request")
    raw.add_argument("method")
    raw.add_argument("path")
    raw.add_argument("--json-file", type=Path)
    args = parser.parse_args()
    token = os.getenv("EVIDENCEHARBOR_API_TOKEN", os.getenv("API_TOKEN", ""))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.api, headers=headers, timeout=120, trust_env=False) as client:

        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.is_error:
                raise SystemExit(f"API {response.status_code}: {response.text}")
            return response

        if args.command == "project":
            result = call(
                "POST", "/v1/projects", json={"name": args.name, "description": args.description}
            ).json()
        elif args.command == "ingest":
            if bool(args.url) == bool(args.file):
                raise SystemExit("Choose exactly one of --url or --file")
            if args.file:
                data = args.file.read_bytes()
                fields = {"project_id": args.project_id, "title": args.title or args.file.name}
                if args.original_url:
                    fields["original_url"] = args.original_url
                result = call(
                    "POST",
                    "/v1/ingestions/upload",
                    data=fields,
                    files={"file": (args.file.name, data)},
                    headers={
                        "Idempotency-Key": f"import:{args.project_id}:{hashlib.sha256(data).hexdigest()}"
                    },
                ).json()
            else:
                result = call(
                    "POST",
                    "/v1/ingestions",
                    json={
                        "project_id": args.project_id,
                        "type": "url",
                        "url": args.url,
                        "title": args.title or None,
                    },
                ).json()
            if args.wait:
                operation_id = result.get("operation_id", result.get("id"))
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    result = call("GET", f"/v1/operations/{operation_id}").json()
                    if result["status"] in ("succeeded", "failed"):
                        break
                    time.sleep(1)
                else:
                    raise SystemExit(f"Operation still pending: {operation_id}")
        elif args.command == "search":
            result = call(
                "POST", "/v1/search", json={"project_id": args.project_id, "query": args.query, "limit": 100}
            ).json()
        elif args.command == "context":
            result = call("GET", f"/v1/projects/{args.project_id}").json()
        elif args.command == "read":
            result = call("GET", f"/v1/documents/{args.document_id}/content").json()
        elif args.command == "evidence":
            result = call(
                "POST",
                "/v1/evidence",
                json={
                    "project_id": args.project_id,
                    "block_id": args.block_id,
                    "quote": args.quote_file.read_text(),
                },
            ).json()
        elif args.command == "propose":
            result = call("POST", "/v1/proposals", json=json.loads(args.json_file.read_text())).json()
        elif args.command == "publish":
            result = call(
                "POST",
                f"/v1/proposals/{args.proposal_id}/publish",
                json={"expected_version": args.expected_version},
            ).json()
        elif args.command == "export":
            response = call("GET", f"/v1/projects/{args.project_id}/export")
            args.output.write_bytes(response.content)
            result = {"path": str(args.output), "sha256": hashlib.sha256(response.content).hexdigest()}
        else:
            result = call(
                args.method.upper(),
                args.path,
                json=json.loads(args.json_file.read_text()) if args.json_file else None,
            ).json()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
