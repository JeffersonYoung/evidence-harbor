"""MCP adds no publication authority and cannot redirect the API bearer credential."""
from uuid import uuid4

import httpx
import pytest

from backend import mcp_server


@pytest.mark.parametrize('identifier', ['../proposals/publish', '//attacker.example.org/',
                                        'valid-id?token=steal', '', '\\..\\admin'])
def test_tool_resource_ids_cannot_inject_request_paths(monkeypatch, identifier):
    monkeypatch.setattr(mcp_server, 'request', lambda *a, **kw: pytest.fail('Invalid resource reached HTTP'))
    for function in (mcp_server.get_project_context, mcp_server.read_document,
                     mcp_server.get_evidence, mcp_server.get_ingestion_status):
        with pytest.raises(ValueError, match='UUID'):
            function(identifier)


@pytest.mark.parametrize('url', ['http://api.example.org', 'http://10.0.0.1:8000',
                                'https://user:password@example.org', 'file:///etc/passwd',
                                'https://example.org/?token=secret', 'https://example.org/#fragment'])
def test_api_base_url_rejects_credentials_insecure_remote_and_non_http(url):
    with pytest.raises(ValueError):
        mcp_server.validated_base_url(url)


@pytest.mark.parametrize('url', ['http://127.0.0.1:8000', 'http://[::1]:8000',
                                'http://localhost:8000', 'https://api.example.org'])
def test_supported_api_base_urls(url):
    assert mcp_server.validated_base_url(url + '/') == url


def test_mcp_bearer_is_never_forwarded_to_redirect_destination(monkeypatch):
    monkeypatch.setenv('EVIDENCEHARBOR_API_URL', 'https://api.example.org')
    monkeypatch.setenv('EVIDENCEHARBOR_API_TOKEN', 'fixture-researcher-token')
    requests, options = [], []
    real_client = httpx.Client

    def transport(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://attacker.example.org/collect'})

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(mcp_server.httpx, 'Client', client)
    with pytest.raises(ValueError, match='302'):
        mcp_server.get_project_context(str(uuid4()))
    assert len(requests) == 1
    assert requests[0].url.host == 'api.example.org'
    assert requests[0].headers['authorization'] == 'Bearer fixture-researcher-token'
    assert options[0]['trust_env'] is False
    assert options[0]['follow_redirects'] is False


def test_mcp_requires_explicit_token_before_any_http(monkeypatch):
    monkeypatch.delenv('EVIDENCEHARBOR_API_TOKEN', raising=False)
    monkeypatch.setattr(mcp_server.httpx, 'Client', lambda **kw: pytest.fail('Unauthenticated HTTP attempted'))
    with pytest.raises(ValueError, match='API_TOKEN'):
        mcp_server.get_project_context(str(uuid4()))


async def test_mcp_exposes_research_tools_without_admin_or_publish_authority():
    tools = await mcp_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {'get_project_context', 'search_library', 'read_document', 'create_evidence',
            'get_evidence', 'propose_research_update'} <= names
    assert not any(word in name for name in names for word in ('publish', 'shell', 'delete', 'schedule', 'password'))
