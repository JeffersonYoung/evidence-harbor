"""Project takeover must not bypass progressive document reading."""

from urllib.parse import parse_qs, urlsplit

from backend import mcp_server
from backend import models as m
from tests.test_api_workflow import checked, project


def test_context_projects_only_bounded_metadata_not_document_or_operation_text(client, runtime):
    proj = project(client)
    secret = 'UNREAD_SOURCE_SENTINEL'
    with runtime.session_factory() as session:
        for index in range(4):
            session.add(m.Document(workspace_id='workspace-a', project_id=proj['id'],
                title=('Long title ' * 1000) + secret, kind='source', content=('x' * 40000) + secret))
        session.add(m.Document(workspace_id='workspace-a', project_id=proj['id'], title='Current report',
                               kind='report', content=secret, version=3))
        session.add(m.Operation(workspace_id='workspace-a', project_id=proj['id'],
                               kind='ingest', input_json={'text': secret}, request_hash='fixture'))
        session.commit()
    page = checked(client.get(f'/v1/projects/{proj["id"]}/context', params={'limit': 2}))
    assert page['content_included'] is False and page['counts']['documents'] == 5
    assert len(page['documents']) == 2 and page['pages']['documents']['next_offset'] == 2
    assert page['report_baseline']['version'] == 3
    assert secret not in str(page)
    assert all('content' not in doc and len(doc['title']) <= 500 for doc in page['documents'])
    assert all('input_json' not in operation for operation in page['operations'])
    last = checked(client.get(f'/v1/projects/{proj["id"]}/context', params={'offset': 4, 'limit': 2}))
    assert len(last['documents']) == 1 and last['pages']['documents']['next_offset'] is None
    # UI compatibility remains explicit: the old route is not the MCP takeover path.
    assert secret in str(checked(client.get(f'/v1/projects/{proj["id"]}')))
    assert client.get(f'/v1/projects/{proj["id"]}/context', headers={'Authorization': 'Bearer owner-b'}).status_code == 404
    assert client.get(f'/v1/projects/{proj["id"]}/context?limit=51').status_code == 422


def test_questions_are_independently_paginated_and_previews_are_bounded(client):
    proj = project(client)
    text = '😀' * 2100 + 'UNREAD_QUESTION_TAIL'
    q = checked(client.post('/v1/questions', json={'project_id': proj['id'], 'text': text}), 201)
    checked(client.post('/v1/questions', json={'project_id': proj['id'], 'text': 'Another question'}), 201)
    first = checked(client.get('/v1/questions', params={'project_id': proj['id'], 'limit': 1}))
    second = checked(client.get('/v1/questions', params={'project_id': proj['id'], 'offset': 1, 'limit': 1}))
    assert first['total'] == 2 and first['next_offset'] == 1 and second['next_offset'] is None
    previews = first['items'] + second['items']
    long = next(item for item in previews if item['id'] == q['id'])
    assert long['text'] == '😀' * 2000 and long['text_length'] == len(text)
    assert checked(client.get(f'/v1/questions/{q["id"]}'))['text'] == text
    assert client.get('/v1/questions', params={'project_id': proj['id']},
                      headers={'Authorization': 'Bearer owner-b'}).status_code == 404


def test_mcp_context_uses_bounded_route_and_explicit_pagination(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, 'request', lambda method, path: calls.append((method, path)) or {})
    mcp_server.get_project_context('11111111-1111-4111-8111-111111111111')
    parsed = urlsplit(calls[0][1])
    assert parsed.path.endswith('/context')
    assert parse_qs(parsed.query) == {'offset': ['0'], 'limit': ['20']}


def test_large_library_many_questions_and_thousand_leads_stay_bounded(client, runtime):
    from backend.scholarly import DiscoveredWork

    proj = project(client)
    with runtime.session_factory() as session:
        for index in range(14):
            session.add(m.Document(workspace_id='workspace-a', project_id=proj['id'], kind='source',
                                   title=f'Paper {index}', content='x' * 100000 + 'UNREAD_LARGE_BODY'))
        for index in range(80):
            session.add(m.Question(workspace_id='workspace-a', project_id=proj['id'], text='Q' * 50000))
        for index in range(1000):
            session.add(DiscoveredWork(workspace_id='workspace-a', project_id=proj['id'],
                                      canonical_key=f'fixture:{index}', title=f'Discovered {index}',
                                      abstract='UNREAD_ABSTRACT' * 100))
        session.commit()
    response = client.get(f'/v1/projects/{proj["id"]}/context')
    data = checked(response)
    assert data['counts']['documents'] == 14 and data['counts']['questions'] == 80
    assert data['counts']['discovered_works'] == 1000
    assert len(data['questions']) == 20 and data['pages']['questions']['next_offset'] == 20
    assert all(len(item['text']) == 500 and item['text_length'] == 50000 for item in data['questions'])
    assert 'UNREAD_' not in response.text and len(response.content) < 30000


def test_progressive_read_does_not_leak_large_legacy_capture_metadata(client, runtime):
    from backend import domain
    from tests.test_domain_integrity import make_project

    project_id = make_project(runtime)
    with runtime.session_factory() as session:
        saved = domain.store_capture(session, 'workspace-a', project_id, 'fixture:legacy-metadata',
            'text', 'Title' * 1000, b'An exact saved source paragraph.', 'text/plain',
            'An exact saved source paragraph.', metadata={'parser_notes': 'PRIVATE_UNREAD_METADATA' * 10000},
            blob_dir=runtime.settings.blob_dir)
        session.commit()
    response = client.get(f'/v1/documents/{saved["document_id"]}/content?limit=10')
    value = checked(response)
    assert value['content'] == 'An exact s'
    assert 'PRIVATE_UNREAD_METADATA' not in response.text
    assert len(value['title']) == 1000 and value['title_length'] == 5000
    assert value['capture']['metadata_omitted'] is True
    assert len(response.content) < 10000
