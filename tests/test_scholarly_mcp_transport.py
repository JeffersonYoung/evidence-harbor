"""Actual stdio MCP -> loopback HTTP -> FastAPI -> isolated SQLite parity.

The HTTP bridge only transports requests into the ordinary application. It does
not simulate authentication, responses, scholarly rules, storage or MCP tools.
All credentials below are test-owned ephemeral roles; no external service or
model is contacted and no environment credentials are passed to the MCP child.
"""

import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import func, select

from backend import models as m
from tests.test_api_workflow import checked, project
from tests.test_scholarly_leads import batch, detail, scoped_ingestion, work

ROOT = Path(__file__).resolve().parents[1]
RESEARCHER = "scholarly-mcp-fixture-researcher"
READER = "scholarly-mcp-fixture-reader"
EDITOR = "scholarly-mcp-fixture-editor"
OTHER_WORKSPACE = "scholarly-mcp-fixture-other-workspace"
SCHOLARLY_TOOLS = {
    "list_discovered_works",
    "read_discovered_work",
    "read_scholarly_record",
    "intake_discovered_works",
    "link_scholarly_reading",
    "record_scholarly_observation",
}


@pytest.fixture
def scholarly_http_bridge(runtime, tmp_path):
    """Expose the real fixture app over HTTP with no inherited owner header."""
    from backend.api import create_app

    runtime.settings.api_tokens.update({
        RESEARCHER: {"workspace_id": "workspace-a", "role": "researcher"},
        READER: {"workspace_id": "workspace-a", "role": "reader"},
        EDITOR: {"workspace_id": "workspace-a", "role": "editor"},
        OTHER_WORKSPACE: {"workspace_id": "workspace-b", "role": "researcher"},
    })
    app = create_app(settings_override=runtime.settings, session_factory=runtime.session_factory)
    requests = []
    with TestClient(app) as api:
        class Handler(BaseHTTPRequestHandler):
            def relay(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {
                    key: value for key, value in self.headers.items()
                    if key.lower() not in {"host", "connection", "content-length"}
                }
                response = api.request(self.command, self.path, headers=headers, content=body)
                requests.append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(body) if body else None,
                    "status": response.status_code,
                })
                self.send_response(response.status_code)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = relay
            do_POST = relay

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield SimpleNamespace(
                url=f"http://127.0.0.1:{server.server_port}",
                requests=requests,
                api=api,
                settings=runtime.settings,
                tmp_path=tmp_path,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            assert not thread.is_alive()


@asynccontextmanager
async def mcp_session(bridge, token=RESEARCHER, *, editor_tools=False):
    # stdio_client merges only its small safe default environment, not os.environ.
    # Keep even import-time database/object-store configuration test-local.
    environment = {
        "EVIDENCEHARBOR_API_URL": bridge.url,
        "EVIDENCEHARBOR_API_TOKEN": token,
        "DATABASE_URL": bridge.settings.database_url,
        "BLOB_DIR": str(bridge.settings.blob_dir),
        "RAW_STORAGE_DIR": str(bridge.settings.blob_dir),
        "STORAGE_BACKEND": "local",
        "DEMO_MODE": "false",
        "PYTHONUNBUFFERED": "1",
        "EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS": str(editor_tools).lower(),
    }
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "backend.mcp_server"], cwd=ROOT, env=environment,
    )
    with (bridge.tmp_path / f"mcp-{token or 'missing-token'}.stderr").open("w") as stderr:
        async with (
            stdio_client(parameters, errlog=stderr) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "EvidenceHarbor"
            yield session


async def call_json(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert not result.isError, result.model_dump()
    if result.structuredContent is not None:
        assert isinstance(result.structuredContent, dict)
        return result.structuredContent
    return json.loads("".join(item.text for item in result.content if item.type == "text"))


async def call_error(session, name, arguments, *, status=None, contains=None):
    result = await session.call_tool(name, arguments)
    assert result.isError, result.model_dump()
    message = "\n".join(item.text for item in result.content if item.type == "text")
    if status is not None:
        assert f"EvidenceHarbor API {status}:" in message, message
    if contains is not None:
        assert contains.casefold() in message.casefold(), message
    return message


def direct_agent_get(bridge, path, **params):
    return checked(bridge.api.get(
        path, params=params, headers={"Authorization": f"Bearer {RESEARCHER}"},
    ))


def assert_unread(record, scope="metadata_only"):
    assert record["content_scope"] == scope
    assert record["fulltext_ready"] is False
    assert record["evidence_eligible"] is False
    assert record["fulltext_read"] is False


async def test_stdio_discovery_has_typed_bounded_scholarly_tools_and_no_editor_authority(
    scholarly_http_bridge,
):
    bridge = scholarly_http_bridge
    async with mcp_session(bridge) as session:
        discovered = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert SCHOLARLY_TOOLS <= discovered.keys()
        assert len(discovered) == 15
        assert not any(
            word in name for name in discovered
            for word in ("publish", "shell", "delete", "schedule", "password", "admin", "review_discovered")
        )
        intake = discovered["intake_discovered_works"].inputSchema
        items = intake["properties"]["items"]
        assert items["minItems"] == 1 and items["maxItems"] == 20
        assert items["items"]["$ref"].endswith("/WorkInput")
        assert intake["$defs"]["WorkInput"]["properties"]["abstract"]["maxLength"] == 100000
        assert set(intake["required"]) == {"project_id", "items"}
        record = discovered["read_scholarly_record"].inputSchema["properties"]
        assert set(record["record_kind"]["enum"]) == {"observation", "reading", "metadata_review"}
        assert record["start"]["minimum"] == 0
        assert record["limit"]["default"] == 2000
        assert record["limit"]["maximum"] <= 32000
        listing = discovered["list_discovered_works"].inputSchema["properties"]
        assert listing["offset"]["minimum"] == 0
        assert listing["limit"]["default"] == 20 and listing["limit"]["maximum"] <= 50
        detail_schema = discovered["read_discovered_work"].inputSchema["properties"]
        assert detail_schema["limit"]["default"] == 10
        assert detail_schema["abstract_limit"]["default"] == 2000
        link_schema = discovered["link_scholarly_reading"].inputSchema["properties"]
        assert set(link_schema["content_scope"]["enum"]) == {"abstract", "fulltext"}
        assert link_schema["fulltext_reviewed"]["default"] is False
        observation_schema = discovered["record_scholarly_observation"].inputSchema
        assert set(observation_schema["required"]) == {"work_id", "observation"}
        await call_error(session, "review_discovered_work_metadata", {})
    assert bridge.requests == []


async def test_stdio_intake_dedup_and_domain_history_match_real_rest(
    client, runtime, scholarly_http_bridge,
):
    bridge = scholarly_http_bridge
    proj = project(client, "Actual MCP bibliography")
    supplied = work(
        "Original MCP supplied title", doi="https://doi.org/10.1234/MCP.1",
        metadata={"provider": "test fixture", "screening": {"decision": "candidate", "reason": "title match"}},
    )
    async with mcp_session(bridge) as session:
        first = await call_json(session, "intake_discovered_works", {
            "project_id": proj["id"], "items": [supplied],
        })
        assert first["created"] == 1 and first["deduplicated"] == 0
        work_id = first["items"][0]["id"]
        assert_unread(first["items"][0])
        original = detail(client, work_id)
        replay = await call_json(session, "intake_discovered_works", {
            "project_id": proj["id"], "items": [supplied],
        })
        assert replay["created"] == 0 and replay["deduplicated"] == 1
        assert replay["items"][0]["id"] == work_id
        assert detail(client, work_id)["observations"] == original["observations"]
        changed = {**supplied, "doi": "doi:10.1234/mcp.1", "title": "Provider title variant",
                   "abstract": "Only this abstract was supplied. The paper is not saved or read.",
                   "metadata": {"screening": {"decision": "included", "reason": "abstract matches topic"},
                                "fulltext_ready": True, "papers_read": 1000}}
        merged = await call_json(session, "intake_discovered_works", {
            "project_id": proj["id"], "items": [changed],
        })
        assert merged["created"] == 0 and merged["deduplicated"] == 1
        assert merged["items"][0]["id"] == work_id
        after = detail(client, work_id)
        assert after["title"] == supplied["title"]
        assert len(after["observations"]) == 2
        assert original["observations"][0] in after["observations"]
        assert after["readings"] == []
        assert_unread(after, "abstract")
        listed = await call_json(session, "list_discovered_works", {
            "project_id": proj["id"], "offset": 0, "limit": 1,
        })
        assert listed == direct_agent_get(
            bridge, "/v1/discovered-works", project_id=proj["id"], offset=0, limit=1, view="agent",
        )
        assert listed["total"] == 1 and listed["items"][0]["id"] == work_id
        assert_unread(listed["items"][0], "abstract")
        current = await call_json(session, "read_discovered_work", {"work_id": work_id})
        assert current == direct_agent_get(
            bridge, f"/v1/discovered-works/{work_id}", view="agent", offset=0, limit=10,
            abstract_start=0, abstract_limit=2000,
        )
        assert_unread(current, "abstract")
    assert all(request["authorization"] == f"Bearer {RESEARCHER}" for request in bridge.requests)
    with runtime.session_factory() as database:
        for model in (m.Source, m.Capture, m.Representation, m.Document, m.Evidence, m.Operation):
            assert database.scalar(select(func.count()).select_from(model)) == 0


async def test_stdio_reader_writes_and_foreign_workspace_are_denied_by_real_api(
    client, scholarly_http_bridge,
):
    bridge = scholarly_http_bridge
    proj = project(client)
    lead = batch(client, proj["id"], [work(doi="10.1234/mcp-auth")])["items"][0]
    saved = scoped_ingestion(client, proj["id"], "fulltext")["result_json"]
    observation = detail(client, lead["id"])["observations"][0]
    intake_args = {"project_id": proj["id"], "items": [work("Forbidden write")]}
    reading_args = {
        "work_id": lead["id"], "document_id": saved["document_id"], "content_scope": "fulltext",
        "review_note": "Verified the test-owned saved fulltext source.", "fulltext_reviewed": True,
    }
    observation_args = {"work_id": lead["id"], "observation": work(doi="10.1234/mcp-auth")}
    async with mcp_session(bridge, READER) as session:
        listed = await call_json(session, "list_discovered_works", {"project_id": proj["id"]})
        assert listed["total"] == 1
        assert (await call_json(session, "read_discovered_work", {"work_id": lead["id"]}))["id"] == lead["id"]
        await call_json(session, "read_scholarly_record", {
            "work_id": lead["id"], "record_kind": "observation", "record_id": observation["id"],
        })
        await call_error(session, "intake_discovered_works", intake_args, status=403)
        await call_error(session, "link_scholarly_reading", reading_args, status=403)
        await call_error(session, "record_scholarly_observation", observation_args, status=403)
    async with mcp_session(bridge, OTHER_WORKSPACE) as session:
        for name, arguments in (
            ("list_discovered_works", {"project_id": proj["id"]}),
            ("read_discovered_work", {"work_id": lead["id"]}),
            ("read_scholarly_record", {"work_id": lead["id"], "record_kind": "observation",
                                       "record_id": observation["id"]}),
            ("intake_discovered_works", intake_args),
            ("link_scholarly_reading", reading_args),
            ("record_scholarly_observation", observation_args),
        ):
            await call_error(session, name, arguments, status=404)
    assert detail(client, lead["id"])["readings"] == []
    assert checked(client.get("/v1/discovered-works", params={"project_id": proj["id"]}))["total"] == 1
    statuses = [(item["authorization"], item["status"]) for item in bridge.requests]
    assert statuses.count((f"Bearer {READER}", 403)) == 3
    assert statuses.count((f"Bearer {OTHER_WORKSPACE}", 404)) == 6


async def test_stdio_reading_links_preserve_saved_provenance_and_reject_abstract_upgrade(
    client, scholarly_http_bridge,
):
    bridge = scholarly_http_bridge
    proj = project(client)
    lead = batch(client, proj["id"], [work(doi="10.1234/mcp-readings")])["items"][0]
    known_abstract = scoped_ingestion(client, proj["id"], "abstract")["result_json"]
    unrelated = scoped_ingestion(
        client, proj["id"], "fulltext", source_url="https://example.org/unrelated-mcp-paper",
    )["result_json"]
    fulltext = scoped_ingestion(client, proj["id"], "fulltext")["result_json"]
    arguments = {
        "work_id": lead["id"], "content_scope": "fulltext", "fulltext_reviewed": True,
        "review_note": "Inspected the test-owned saved artifact and confirmed it contains fulltext.",
    }
    async with mcp_session(bridge) as session:
        await call_error(session, "link_scholarly_reading", {
            **arguments, "document_id": known_abstract["document_id"],
        }, status=422, contains="abstract content cannot be promoted")
        await call_error(session, "link_scholarly_reading", {
            **arguments, "document_id": unrelated["document_id"],
        }, status=422, contains="provenance does not match")
        assert detail(client, lead["id"])["readings"] == []
        abstract_link = await call_json(session, "link_scholarly_reading", {
            **arguments, "document_id": known_abstract["document_id"],
            "content_scope": "abstract", "fulltext_reviewed": False,
        })
        assert abstract_link["content_scope"] == "abstract"
        abstract_work = await call_json(session, "read_discovered_work", {"work_id": lead["id"]})
        assert abstract_work["evidence_eligible"] is True and abstract_work["fulltext_ready"] is False
        assert abstract_work["content_scope"] == "abstract" and abstract_work["fulltext_read"] is False
        linked = await call_json(session, "link_scholarly_reading", {
            **arguments, "document_id": fulltext["document_id"],
        })
        again = await call_json(session, "link_scholarly_reading", {
            **arguments, "document_id": fulltext["document_id"],
        })
        # SQLite reloads timestamps as naive UTC, unlike the just-created ORM object.
        assert {key: value for key, value in again.items() if key != "created_at"} == {
            key: value for key, value in linked.items() if key != "created_at"
        }
        assert datetime.fromisoformat(again["created_at"]).replace(tzinfo=UTC) == (
            datetime.fromisoformat(linked["created_at"]).replace(tzinfo=UTC)
        )
        for key in ("document_id", "capture_id", "representation_id"):
            assert linked[key] == fulltext[key]
        capture = checked(client.get(f'/v1/captures/{fulltext["capture_id"]}'))
        rest_link = next(row for row in detail(client, lead["id"])["readings"] if row["id"] == linked["id"])
        assert rest_link["provenance_json"]["binding"] == "recorded_source_locator"
        assert rest_link["provenance_json"]["capture_sha256"] == capture["content_hash"]
        reading_window = await call_json(session, "read_scholarly_record", {
            "work_id": lead["id"], "record_kind": "reading", "record_id": linked["id"], "limit": 16000,
        })
        assert json.loads(reading_window["text"]) == rest_link
        assert reading_window["reading_range"]["next_offset"] is None
        ready = await call_json(session, "read_discovered_work", {"work_id": lead["id"]})
        assert ready["content_scope"] == "fulltext"
        assert ready["evidence_eligible"] is True and ready["fulltext_ready"] is True
        assert ready["fulltext_read"] is False
    assert len(detail(client, lead["id"])["readings"]) == 2
    assert checked(client.get(f'/v1/projects/{proj["id"]}'))["evidence"] == []


async def test_stdio_missing_and_invalid_credentials_fail_closed(client, scholarly_http_bridge):
    bridge = scholarly_http_bridge
    proj = project(client)
    async with mcp_session(bridge, "") as session:
        await call_error(session, "list_discovered_works", {"project_id": proj["id"]}, contains="API_TOKEN")
    assert bridge.requests == []
    async with mcp_session(bridge, "scholarly-mcp-fixture-unknown-token") as session:
        await call_error(session, "list_discovered_works", {"project_id": proj["id"]}, status=401)
    assert len(bridge.requests) == 1 and bridge.requests[0]["status"] == 401


async def test_stdio_bounded_lists_histories_and_unicode_record_windows_preserve_exact_content(
    client, scholarly_http_bridge,
):
    bridge = scholarly_http_bridge
    proj = project(client)
    abstract = "Résumé 世界 🔬. " * 700
    supplied = work(
        "A long title " * 100, doi="10.1234/mcp-progressive", abstract=abstract,
        authors=[f"Author {index} " + "a" * 240 for index in range(30)],
        metadata={"title_abstract_screen": {"decision": "candidate", "reason": "Résumé 世界 " * 700}},
    )
    async with mcp_session(bridge) as session:
        intake = await call_json(session, "intake_discovered_works", {
            "project_id": proj["id"], "items": [supplied, work("Other one"), work("Other two")],
        })
        work_id = intake["items"][0]["id"]
        assert "abstract" not in intake["items"][0]
        assert len(json.dumps(intake, ensure_ascii=False)) < 16000
        for phase in ("adjudication", "substantive_review"):
            result = await call_json(session, "record_scholarly_observation", {
                "work_id": work_id,
                "observation": {**supplied, "metadata": {phase: {"decision": "candidate", "papers_read": 999}}},
            })
            assert result["id"] == work_id
            assert_unread(result, "abstract")
        stored = detail(client, work_id)
        assert len(stored["observations"]) == 3
        # Repeating an observation still uses the shared immutable hash deduplication.
        await call_json(session, "record_scholarly_observation", {
            "work_id": work_id,
            "observation": {**supplied, "metadata": {"substantive_review": {"decision": "candidate", "papers_read": 999}}},
        })
        assert detail(client, work_id)["observations"] == stored["observations"]
        # A target mismatch must neither attach the observation nor leave a newly created lead.
        await call_error(session, "record_scholarly_observation", {
            "work_id": work_id, "observation": work("Unrelated observation", doi="10.1234/not-target"),
        }, status=409, contains="identity does not match")
        assert detail(client, work_id)["observations"] == stored["observations"]
        page = await call_json(session, "list_discovered_works", {
            "project_id": proj["id"], "offset": 0, "limit": 2,
        })
        assert page["total"] == 3 and page["next_offset"] == 2 and len(page["items"]) == 2
        last = await call_json(session, "list_discovered_works", {
            "project_id": proj["id"], "offset": page["next_offset"], "limit": 2,
        })
        assert len(last["items"]) == 1 and last["next_offset"] is None
        assert len({item["id"] for item in page["items"] + last["items"]}) == 3
        assert all("abstract" not in item for item in page["items"] + last["items"])
        first_window = await call_json(session, "read_discovered_work", {
            "work_id": work_id, "limit": 1, "abstract_start": 7, "abstract_limit": 37,
        })
        assert first_window == direct_agent_get(
            bridge, f"/v1/discovered-works/{work_id}", view="agent", offset=0, limit=1,
            abstract_start=7, abstract_limit=37,
        )
        assert len(first_window["title"]) == 500 and first_window["title_length"] == len(supplied["title"])
        assert len(first_window["authors"]) == 20 and first_window["author_count"] == 30
        assert first_window["authors_truncated"] is True
        assert max(map(len, first_window["authors"])) == 200
        assert first_window["abstract"] == abstract[7:44]
        assert first_window["abstract_range"]["next_offset"] == 44
        assert first_window["abstract_range"]["offset_unit"] == "unicode_code_points"
        for collection in ("aliases", "observations", "readings", "metadata_reviews"):
            assert len(first_window[collection]) <= 1
            assert first_window["pages"][collection]["limit"] == 1
        assert first_window["pages"]["observations"]["total"] == 3
        assert first_window["pages"]["observations"]["next_offset"] == 1
        assert "payload" not in first_window["observations"][0]
        assert len(json.dumps(first_window, ensure_ascii=False)) < 10000
        seen = [first_window["observations"][0]["id"]]
        offset = first_window["pages"]["observations"]["next_offset"]
        while offset is not None:
            continuation = await call_json(session, "read_discovered_work", {
                "work_id": work_id, "offset": offset, "limit": 1, "abstract_limit": 1,
            })
            seen += [row["id"] for row in continuation["observations"]]
            offset = continuation["pages"]["observations"]["next_offset"]
        assert len(seen) == len(set(seen)) == 3
        assert set(seen) == {row["id"] for row in stored["observations"]}
        observation = next(row for row in stored["observations"] if row["payload"]["metadata"] == supplied["metadata"])
        pieces, start, expected_hash = [], 0, None
        while start is not None:
            arguments = {"work_id": work_id, "record_kind": "observation",
                         "record_id": observation["id"], "start": start, "limit": 2000}
            window = await call_json(session, "read_scholarly_record", arguments)
            assert window == direct_agent_get(
                bridge, f'/v1/discovered-works/{work_id}/records/observation/{observation["id"]}',
                start=start, limit=2000,
            )
            assert len(window["text"]) <= 2000
            assert window["text_sha256"] == sha256(window["text"].encode()).hexdigest()
            expected_hash = expected_hash or window["record_sha256"]
            assert window["record_sha256"] == expected_hash
            bounds = window["reading_range"]
            assert bounds["offset_unit"] == "unicode_code_points"
            assert bounds["start_offset"] == start and bounds["end_offset"] == start + len(window["text"])
            assert bounds["has_more"] is (bounds["next_offset"] is not None)
            pieces.append(window["text"])
            start = bounds["next_offset"]
        reconstructed = "".join(pieces)
        assert json.loads(reconstructed) == observation
        assert sha256(reconstructed.encode()).hexdigest() == expected_hash
        assert len(reconstructed) == bounds["total_length"]
        empty = await call_json(session, "read_scholarly_record", {
            "work_id": work_id, "record_kind": "observation", "record_id": observation["id"],
            "start": len(reconstructed), "limit": 2000,
        })
        assert empty["text"] == "" and empty["reading_range"]["next_offset"] is None
        await call_error(session, "read_scholarly_record", {
            "work_id": work_id, "record_kind": "observation", "record_id": observation["id"],
            "start": len(reconstructed) + 1,
        }, status=422)
        # Same-project record IDs cannot be used under another work's route.
        await call_error(session, "read_scholarly_record", {
            "work_id": intake["items"][1]["id"], "record_kind": "observation", "record_id": observation["id"],
        }, status=404)


async def test_stdio_input_limits_and_path_validation_stop_before_http(client, scholarly_http_bridge):
    bridge = scholarly_http_bridge
    proj = project(client)
    async with mcp_session(bridge) as session:
        invalid_calls = [
            ("intake_discovered_works", {"project_id": proj["id"], "items": []}),
            ("intake_discovered_works", {"project_id": proj["id"], "items": [work()] * 21}),
            ("intake_discovered_works", {"project_id": proj["id"], "items": [work(abstract="a" * 90000)] * 4}),
            ("list_discovered_works", {"project_id": proj["id"], "limit": 100000}),
            ("read_discovered_work", {"work_id": proj["id"], "abstract_limit": 100000}),
            ("read_scholarly_record", {"work_id": proj["id"], "record_id": proj["id"], "record_kind": "capture"}),
            ("read_scholarly_record", {"work_id": proj["id"], "record_id": proj["id"],
                                       "record_kind": "observation", "limit": 100000}),
            ("read_scholarly_record", {"work_id": proj["id"], "record_id": "../../admin",
                                       "record_kind": "observation"}),
            ("read_discovered_work", {"work_id": "../proposals/publish"}),
            ("link_scholarly_reading", {"work_id": proj["id"], "document_id": "../admin",
                                        "content_scope": "fulltext", "review_note": "Invalid target identity"}),
            ("record_scholarly_observation", {"work_id": "//attacker.example.org", "observation": work()}),
        ]
        for name, arguments in invalid_calls:
            await call_error(session, name, arguments)
    assert bridge.requests == []


async def test_opt_in_editor_tool_keeps_role_cas_and_immutable_review_history(
    client, scholarly_http_bridge,
):
    bridge = scholarly_http_bridge
    proj = project(client)
    supplied = work("Provider supplied title", doi="10.1234/mcp-reviewed", authors=["Provider Name"])
    lead = batch(client, proj["id"], [supplied])["items"][0]
    original = detail(client, lead["id"])
    review = {
        "expected_revision": 0, "changes": {"authors": ["Verified Author"], "title": "Reviewed title"},
        "reason": "Checked the publisher metadata for this test-owned scholarly record.",
        "source_url": "https://doi.org/10.1234/mcp-reviewed",
    }
    arguments = {"work_id": lead["id"], "review": review}
    async with mcp_session(bridge, RESEARCHER, editor_tools=True) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert len(names) == 16 and "review_discovered_work_metadata" in names
        await call_error(session, "review_discovered_work_metadata", arguments, status=403)
    assert detail(client, lead["id"])["review_revision"] == 0
    async with mcp_session(bridge, EDITOR, editor_tools=True) as session:
        reviewed = await call_json(session, "review_discovered_work_metadata", arguments)
        assert reviewed["review_revision"] == 1
        assert reviewed["title"] == "Reviewed title" and reviewed["authors"] == ["Verified Author"]
        assert_unread(reviewed)
        await call_error(session, "review_discovered_work_metadata", arguments, status=409, contains="revision changed")
        current = await call_json(session, "read_discovered_work", {"work_id": lead["id"]})
        assert current["review_revision"] == 1
        assert current["pages"]["metadata_reviews"]["total"] == 1
        review_id = current["metadata_reviews"][0]["id"]
        window = await call_json(session, "read_scholarly_record", {
            "work_id": lead["id"], "record_kind": "metadata_review", "record_id": review_id, "limit": 16000,
        })
        record = json.loads(window["text"])
        assert record["actor_json"]["role"] == "editor"
        assert record["reason"] == review["reason"]
        assert record["source_url"] == review["source_url"]
        assert window["reading_range"]["next_offset"] is None
        assert window["record_sha256"] == sha256(window["text"].encode()).hexdigest()
        after = detail(client, lead["id"])
        assert record == after["metadata_reviews"][0]
        assert after["observations"] == original["observations"]
        assert after["provider_display"]["title"] == supplied["title"]
        assert after["provider_display"]["authors"] == supplied["authors"]
    review_requests = [item for item in bridge.requests if item["method"] == "POST" and "/metadata-reviews" in item["path"]]
    assert [(item["authorization"], item["status"]) for item in review_requests] == [
        (f"Bearer {RESEARCHER}", 403), (f"Bearer {EDITOR}", 201), (f"Bearer {EDITOR}", 409),
    ]
