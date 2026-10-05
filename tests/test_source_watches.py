"""Source watches must preserve policy, deduplicate retries, and remain tenant-admin scoped."""
from sqlalchemy import func, select

from backend import models as m
from backend.scheduling import SourceWatch, enqueue_watch
from tests.test_api_workflow import checked, project
from tests.test_processing import config_with


def test_watch_queue_preserves_pipeline_and_deduplicates_only_same_tick(client, runtime):
    proj = project(client)
    pipeline = config_with('quality', min_characters=50)
    watch = checked(client.post('/v1/source-watches', json={
        'project_id': proj['id'], 'locator': 'https://93.184.216.34/evidence',
        'interval_seconds': 3600, 'pipeline_config': pipeline}), 201)
    with runtime.session_factory() as session:
        first = enqueue_watch(session, watch['id'], 'same-scheduled-tick')
        session.commit()
        repeated = enqueue_watch(session, watch['id'], 'same-scheduled-tick')
        session.commit()
        next_tick = enqueue_watch(session, watch['id'], 'next-scheduled-tick')
        session.commit()
        assert repeated['operation_id'] == first['operation_id']
        assert next_tick['operation_id'] != first['operation_id']
        operation = session.get(m.Operation, first['operation_id'])
        assert operation.input_json['type'] == 'url'
        assert operation.input_json['url'] == watch['locator']
        assert operation.input_json.get('pipeline_config', operation.input_json.get('pipeline')) == pipeline
        assert session.scalar(select(func.count()).select_from(m.OperationDispatch)) == 2
        session.get(SourceWatch, watch['id']).enabled = False
        session.commit()
        assert enqueue_watch(session, watch['id'], 'disabled-tick')['status'] == 'disabled'
        assert session.scalar(select(func.count()).select_from(m.Operation)) == 2


def test_watch_rejects_private_destination_invalid_pipeline_and_cross_workspace(client, runtime):
    proj = project(client)
    for payload in (
        {'locator': 'http://127.0.0.1/internal'},
        {'locator': 'https://93.184.216.34/evidence', 'pipeline_config': {'version': 1, 'stages': []}},
        {'locator': 'https://93.184.216.34/evidence', 'interval_seconds': 1},
    ):
        response = client.post('/v1/source-watches', json={'project_id': proj['id'], **payload})
        assert response.status_code == 422, response.text
    runtime.settings.api_tokens['researcher-a'] = {'workspace_id': 'workspace-a', 'role': 'researcher'}
    assert client.post('/v1/source-watches', headers={'Authorization': 'Bearer researcher-a'},
        json={'project_id': proj['id'], 'locator': 'https://93.184.216.34/'}).status_code == 403
    assert client.get(f'/v1/source-watches?project_id={proj["id"]}',
                      headers={'Authorization': 'Bearer owner-b'}).status_code == 404
