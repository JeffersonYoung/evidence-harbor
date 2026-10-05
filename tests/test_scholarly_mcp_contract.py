"""Transport-only guards, separate from the real stdio/domain parity suite."""

from uuid import uuid4

import pytest

from backend import mcp_server


def test_older_api_protocol_cannot_receive_scholarly_mutation(monkeypatch):
    calls = []

    def old_api(method, path, payload=None):
        calls.append((method, path, payload))
        return {'protocol': 'legacy'}

    monkeypatch.setattr(mcp_server, 'request', old_api)
    with pytest.raises(ValueError, match='Upgrade the API'):
        mcp_server.intake_discovered_works(str(uuid4()), [{'title': 'Protocol fixture', 'doi': '10.1234/protocol'}])
    assert calls == [('GET', '/v1/discovered-works/capabilities', None)]


def test_legacy_unbounded_response_is_not_forwarded_as_agent_projection(monkeypatch):
    def mixed_api(method, path, payload=None):
        return {'protocol': 'scholarly-mcp-v1'} if path.endswith('/capabilities') else {'abstract': 'UNBOUNDED_LEGACY_DATA'}

    monkeypatch.setattr(mcp_server, 'request', mixed_api)
    with pytest.raises(ValueError, match='required bounded scholarly projection') as error:
        mcp_server.read_discovered_work(str(uuid4()))
    assert 'UNBOUNDED_LEGACY_DATA' not in str(error.value)


def test_utf8_budget_rejects_before_any_http(monkeypatch):
    monkeypatch.setattr(mcp_server, 'request', lambda *args, **kwargs: pytest.fail('Oversized payload reached HTTP'))
    with pytest.raises(ValueError, match='256000 bytes'):
        mcp_server.intake_discovered_works(str(uuid4()), [{'title': 'Large abstract', 'doi': '10.1234/large', 'abstract': '字' * 100000}])
    with pytest.raises(ValueError, match='pagination'):
        mcp_server.list_discovered_works(str(uuid4()), limit=51)


def test_editor_function_itself_fails_closed_when_not_opted_in(monkeypatch):
    monkeypatch.delenv('EVIDENCEHARBOR_ENABLE_EDITOR_TOOLS', raising=False)
    monkeypatch.setattr(mcp_server, 'request', lambda *args, **kwargs: pytest.fail('Disabled editor tool reached API'))
    with pytest.raises(ValueError, match='disabled'):
        mcp_server.review_discovered_work_metadata(str(uuid4()), {})
