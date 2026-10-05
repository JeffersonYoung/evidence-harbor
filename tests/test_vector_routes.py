"""Optional vector capability must fail clearly without expanding tenant or role authority."""
from uuid import uuid4

from sqlalchemy import func, select

from backend import domain
from backend import models as m
from tests.test_api_workflow import checked, ingestion, project


def test_vector_index_sqlite_failure_is_explicit_without_enqueuing_assets(client, runtime):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    representation_id = operation['result_json']['representation_id']
    response = client.post(f'/v1/representations/{representation_id}/vector-index', json={'provider': 'local_hash'})
    assert response.status_code == 503
    assert 'PostgreSQL' in response.json()['detail']
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.Operation).where(m.Operation.kind == 'vector_index')) == 0
        assert session.scalar(select(func.count()).select_from(m.IndexGeneration).where(m.IndexGeneration.index_kind == 'vector')) == 0


def test_vector_route_requires_admin_and_scopes_representation_first(client, runtime):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    path = f'/v1/representations/{operation["result_json"]["representation_id"]}/vector-index'
    runtime.settings.api_tokens['reader-a'] = {'workspace_id': 'workspace-a', 'role': 'reader'}
    assert client.post(path, headers={'Authorization': 'Bearer reader-a'}, json={}).status_code == 403
    assert client.post(path, headers={'Authorization': 'Bearer owner-b'}, json={}).status_code == 404
    assert client.post(path, json={'provider': 'arbitrary-code'}).status_code == 422


def test_hybrid_without_vector_generation_is_actual_scoped_lexical_search(client):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    body = {'project_id': proj['id'], 'query': 'tree canopy', 'generation_ids': []}
    hybrid = checked(client.post('/v1/hybrid-search', json=body))
    lexical = checked(client.post('/v1/search', json={'project_id': proj['id'], 'query': 'tree canopy'}))
    assert hybrid == lexical
    assert hybrid['mode'] == 'lexical' and hybrid['vector_available'] is False
    assert hybrid['results'][0]['block_id'] in operation['result_json']['block_ids']
    assert client.post('/v1/hybrid-search', json=body, headers={'Authorization': 'Bearer owner-b'}).status_code == 404
    body['generation_ids'] = [str(uuid4())]
    unavailable = client.post('/v1/hybrid-search', json=body)
    assert unavailable.status_code == 503
    assert 'PostgreSQL' in unavailable.json()['detail']


def test_index_generation_listing_is_scoped_and_references_saved_representation(client):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    listing = checked(client.get(f'/v1/index-generations?project_id={proj["id"]}'))['items']
    assert len(listing) == 1
    assert listing[0]['index_kind'] == 'lexical'
    assert listing[0]['representation_id'] == operation['result_json']['representation_id']
    assert client.get(f'/v1/index-generations?project_id={proj["id"]}',
                      headers={'Authorization': 'Bearer owner-b'}).status_code == 404


def test_generic_vector_operation_cannot_claim_success_on_sqlite(client, runtime):
    proj = project(client)
    original = ingestion(client, proj['id'])
    with runtime.session_factory() as session:
        operation = domain.create_operation(session, 'workspace-a', proj['id'],
            {'representation_id': original['result_json']['representation_id'], 'provider': 'local_hash'},
            kind='vector_index')
        session.commit()
        operation_id = operation.id
    failed = domain.execute_operation(operation_id, runtime.session_factory)
    assert failed['status'] == 'failed'
    assert 'PostgreSQL' in failed['error']
    assert failed['result_json'] is None
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.IndexGeneration).where(m.IndexGeneration.index_kind == 'vector')) == 0
