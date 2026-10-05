"""Real HTTP-to-database workflows; no mocked ingestion, search or research success."""
import io
import json
import zipfile
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend import models as m
from tests.helpers import digest, text_pdf


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def project(client, name='Evidence research'):
    return checked(client.post('/v1/projects', json={'name': name}), 201)


def ingestion(client, project_id, text='Tree canopy reduces heat. Policy evidence records measurable benefits.', key=None):
    headers = {'Idempotency-Key': key} if key else {}
    operation = checked(client.post('/v1/ingestions', headers=headers,
        json={'project_id': project_id, 'type': 'text', 'text': text, 'title': 'Primary evidence'}), 202)
    finished = checked(client.get(f'/v1/operations/{operation["id"]}'))
    assert finished['status'] == 'succeeded', finished
    return finished


def evidence(client, project_id, operation, quote='Tree canopy reduces heat.'):
    return checked(client.post('/v1/evidence', json={'project_id': project_id,
        'block_id': operation['result_json']['block_ids'][0], 'quote': quote}), 201)


def test_http_research_cycle_reaches_staged_proposal_publish_and_portable_export(client):
    proj = project(client)
    source_text = 'Tree canopy reduces heat. Policy evidence records measurable benefits.'
    op = ingestion(client, proj['id'], source_text)
    content = checked(client.get(f'/v1/documents/{op["result_json"]["document_id"]}/content'))
    assert content['content'] == source_text
    assert content['capture']['content_hash'] == digest(source_text)
    hit = checked(client.post('/v1/search', json={'project_id': proj['id'], 'query': 'tree canopy'}))['results'][0]
    assert hit['block_id'] in op['result_json']['block_ids']
    ev = evidence(client, proj['id'], op)
    assert ev['quote'] == 'Tree canopy reduces heat.'
    assert ev['content_hash'] == digest(ev['quote'])
    saved_ev = checked(client.get(f'/v1/evidence/{ev["id"]}'))
    assert saved_ev['block']['text'][ev['start_offset']:ev['end_offset']] == ev['quote']
    run = checked(client.post('/v1/research-runs', json={
        'project_id': proj['id'], 'question': 'What evidence exists about tree canopy heat?', 'provider': 'local'}), 202)
    completed = checked(client.get(f'/v1/research-runs/{run["id"]}'))
    assert completed['status'] == 'succeeded', completed
    events = completed['events']
    assert [event['sequence'] for event in events] == list(range(1, len(events) + 1))
    assert {'search.completed', 'evidence.collected', 'proposal.created', 'run.succeeded'} <= {
        event['type'] for event in events}
    replay = checked(client.get(f'/v1/research-runs/{run["id"]}/events?after={events[1]["sequence"]}'))
    assert [event['id'] for event in replay] == [event['id'] for event in events[2:]]
    proposal_id = completed['result_json']['proposal_id']
    staged = checked(client.get(f'/v1/proposals/{proposal_id}'))
    assert staged['status'] == 'pending'
    assert staged['published_document_id'] is None
    allowed_ids = {item['id'] for item in staged['evidence']}
    assert allowed_ids
    for claim in staged['claims']:
        assert claim['text'] in staged['content']
        assert set(claim['evidence_ids']) <= allowed_ids
    published = checked(client.post(f'/v1/proposals/{proposal_id}/publish', json={}))
    assert published['kind'] == 'report' and published['version'] == 1
    repeated = checked(client.post(f'/v1/proposals/{proposal_id}/publish', json={}))
    assert repeated['id'] == published['id'] and repeated['version'] == 1
    exported = client.get(f'/v1/projects/{proj["id"]}/export')
    assert exported.status_code == 200
    assert exported.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['project']['id'] == proj['id']
        assert any(item['id'] == ev['id'] for item in manifest['resources']['evidence'])
        assert archive.read(f'documents/{published["id"]}.md').decode() == published['content']
        for obj in manifest['objects']:
            assert digest(archive.read(obj['path'])) == obj['sha256']


@pytest.mark.parametrize('filename,media_type,raw,expected', [
    ('saved.html', 'text/html', b'<html><head><title>Saved report</title></head><body><article><p>HTML policy evidence is preserved.</p></article></body></html>', 'HTML policy evidence is preserved.'),
    ('paper.pdf', 'application/pdf', text_pdf('First PDF evidence page.', 'Second PDF limitations page.'), 'Second PDF limitations page.'),
    ('notes.txt', 'text/plain', '政策证据：风险降低 20%。'.encode(), '政策证据：风险降低 20%。'),
])
def test_upload_actual_formats_preserves_raw_and_source_map(client, filename, media_type, raw, expected):
    proj = project(client)
    submitted = checked(client.post('/v1/ingestions/upload', data={'project_id': proj['id']},
        files={'file': (filename, raw, media_type)}, headers={'Idempotency-Key': f'file-{filename}'}), 202)
    assert 'blob_path' not in submitted['input_json']
    operation = checked(client.get(f'/v1/operations/{submitted["id"]}'))
    assert operation['status'] == 'succeeded', operation
    assert 'blob_path' not in operation['input_json']
    capture = checked(client.get(f'/v1/captures/{operation["result_json"]["capture_id"]}'))
    assert capture['content_hash'] == digest(raw)
    assert capture['byte_size'] == len(raw)
    assert 'blob_path' not in capture
    representation = checked(client.get(f'/v1/representations/{operation["result_json"]["representation_id"]}'))
    assert expected in representation['text']
    assert representation['content_hash'] == digest(representation['text'])
    for block in representation['blocks']:
        assert representation['text'][block['start_offset']:block['end_offset']] == block['text']
        assert block['content_hash'] == digest(block['text'])
    if media_type == 'application/pdf':
        assert [block['locator_json']['page'] for block in representation['blocks']] == [1, 2]


def test_reprocessing_adds_representation_without_moving_old_evidence(client):
    proj = project(client)
    original = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], original)
    old_evidence = checked(client.get(f'/v1/evidence/{ev["id"]}'))
    capture_id = original['result_json']['capture_id']
    request = checked(client.post(f'/v1/captures/{capture_id}/reprocess', json={},
                                  headers={'Idempotency-Key': 'reprocess-once'}), 202)
    operation = checked(client.get(f'/v1/operations/{request["id"]}'))
    assert operation['status'] == 'succeeded', operation
    assert operation['result_json']['representation_id'] != original['result_json']['representation_id']
    after = checked(client.get(f'/v1/evidence/{ev["id"]}'))
    assert after == old_evidence
    capture = checked(client.get(f'/v1/captures/{capture_id}'))
    assert len(capture['representations']) == 2
    assert capture['content_hash'] == digest('Tree canopy reduces heat. Policy evidence records measurable benefits.')
    captures = checked(client.get(f'/v1/sources/{original["result_json"]["source_id"]}/captures'))
    assert len(captures) == 1
    repeated = checked(client.post(f'/v1/captures/{capture_id}/reprocess', json={},
                                   headers={'Idempotency-Key': 'reprocess-once'}), 202)
    assert repeated['id'] == request['id']
    assert len(checked(client.get(f'/v1/captures/{capture_id}'))['representations']) == 2


def test_http_idempotency_and_conflict(client, runtime):
    proj = project(client)
    first = ingestion(client, proj['id'], key='once')
    second = ingestion(client, proj['id'], key='once')
    assert first['id'] == second['id']
    response = client.post('/v1/ingestions', headers={'Idempotency-Key': 'once'}, json={
        'project_id': proj['id'], 'type': 'text', 'text': 'Different content'})
    assert response.status_code == 409
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.Capture)) == 1
        assert session.scalar(select(func.count()).select_from(m.Operation)) == 1


def test_workspace_isolation_is_enforced_on_every_resource(client):
    proj = project(client)
    op = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], op)
    claims = [{'text': ev['quote'], 'evidence_ids': [ev['id']]}]
    proposal = checked(client.post('/v1/proposals', json={
        'project_id': proj['id'], 'title': 'Scoped report', 'content': ev['quote'], 'claims': claims}), 201)
    other = {'Authorization': 'Bearer owner-b', 'X-Workspace-ID': 'workspace-a'}
    paths = [f'/v1/projects/{proj["id"]}', f'/v1/projects/{proj["id"]}/export',
             f'/v1/operations/{op["id"]}', f'/v1/sources/{op["result_json"]["source_id"]}',
             f'/v1/captures/{op["result_json"]["capture_id"]}',
             f'/v1/representations/{op["result_json"]["representation_id"]}',
             f'/v1/documents/{op["result_json"]["document_id"]}/content',
             f'/v1/evidence/{ev["id"]}', f'/v1/proposals/{proposal["id"]}']
    for path in paths:
        response = client.get(path, headers=other)
        assert response.status_code == 404, (path, response.text)
    assert checked(client.get('/v1/projects', headers=other)) == []
    assert client.post('/v1/search', headers=other, json={'project_id': proj['id'], 'query': 'tree'}).status_code == 404
    assert client.post(f'/v1/proposals/{proposal["id"]}/publish', headers=other, json={}).status_code == 404


def test_http_citations_reject_missing_and_cross_project_ids(client):
    first, other = project(client), project(client, 'Other project')
    op = ingestion(client, first['id'])
    ev = evidence(client, first['id'], op)
    for citation_id in (ev['id'], str(uuid4())):
        response = client.post('/v1/proposals', json={'project_id': other['id'], 'title': 'Bad report',
            'content': 'Tree canopy reduces heat.',
            'claims': [{'text': 'Tree canopy reduces heat.', 'evidence_ids': [citation_id]}]})
        assert response.status_code == 404
    wrong_block = client.post('/v1/evidence', json={'project_id': other['id'],
        'block_id': op['result_json']['block_ids'][0], 'quote': ev['quote']})
    assert wrong_block.status_code == 404
    fabricated = client.post('/v1/evidence', json={'project_id': first['id'],
        'block_id': op['result_json']['block_ids'][0], 'quote': 'Invented finding'})
    assert fabricated.status_code == 422


def test_authentication_is_required_and_cannot_be_selected_by_workspace_header(client):
    for auth in ('', 'Bearer incorrect-token'):
        response = client.get('/v1/projects', headers={'Authorization': auth, 'X-Workspace-ID': 'workspace-a'})
        assert response.status_code == 401
        assert response.headers['www-authenticate'] == 'Bearer'
    assert checked(client.get('/v1/me'))['workspace_id'] == 'workspace-a'


def test_reader_and_researcher_cannot_publish_or_administer(client, runtime):
    proj = project(client)
    op = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], op)
    runtime.settings.api_tokens.update({
        'reader-a': {'workspace_id': 'workspace-a', 'role': 'reader'},
        'researcher-a': {'workspace_id': 'workspace-a', 'role': 'researcher'},
    })
    reader = {'Authorization': 'Bearer reader-a'}
    researcher = {'Authorization': 'Bearer researcher-a'}
    assert client.get(f'/v1/projects/{proj["id"]}', headers=reader).status_code == 200
    assert client.post('/v1/projects', headers=reader, json={'name': 'Unauthorized'}).status_code == 403
    assert client.post('/v1/ingestions', headers=reader, json={
        'project_id': proj['id'], 'text': 'Unauthorized'}).status_code == 403
    proposal = checked(client.post('/v1/proposals', headers=researcher, json={
        'project_id': proj['id'], 'title': 'Staged report', 'content': ev['quote'],
        'claims': [{'text': ev['quote'], 'evidence_ids': [ev['id']]}]}), 201)
    assert client.post(f'/v1/proposals/{proposal["id"]}/publish', headers=researcher, json={}).status_code == 403
    assert client.post('/v1/subscriptions', headers=researcher, json={'project_id': proj['id']}).status_code == 403
    assert checked(client.get(f'/v1/proposals/{proposal["id"]}'))['status'] == 'pending'


def test_empty_and_oversized_uploads_rejected_before_processing(client, runtime):
    proj = project(client)
    runtime.settings.max_upload_bytes = 8
    for raw, status in ((b'', 422), (b'123456789', 413)):
        response = client.post('/v1/ingestions/upload', data={'project_id': proj['id']},
                               files={'file': ('source.txt', raw, 'text/plain')})
        assert response.status_code == status
    assert checked(client.get(f'/v1/projects/{proj["id"]}'))['operations'] == []


def test_private_url_ingestion_fails_without_creating_capture_or_index(client, runtime):
    proj = project(client)
    requested = checked(client.post('/v1/ingestions', json={
        'project_id': proj['id'], 'type': 'url', 'url': 'http://169.254.169.254/latest/meta-data/'}), 202)
    operation = checked(client.get(f'/v1/operations/{requested["id"]}'))
    assert operation['status'] == 'failed'
    assert 'private' in operation['error'].lower() or 'non-public' in operation['error'].lower()
    assert operation['result_json'] is None
    state = checked(client.get(f'/v1/projects/{proj["id"]}'))
    assert state['documents'] == []
    with runtime.session_factory() as session:
        for model in (m.Capture, m.Representation, m.Block, m.IndexGeneration):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_private_webhook_is_rejected_as_validation_error(client):
    proj = project(client)
    response = client.post('/v1/subscriptions', json={
        'project_id': proj['id'], 'target_url': 'http://127.0.0.1/private'})
    assert response.status_code == 422, response.text


def test_no_evidence_research_has_observable_failure_and_no_proposal(client):
    proj = project(client)
    requested = checked(client.post('/v1/research-runs', json={
        'project_id': proj['id'], 'question': 'No matching evidence exists', 'provider': 'local'}), 202)
    run = checked(client.get(f'/v1/research-runs/{requested["id"]}'))
    assert run['status'] == 'failed'
    assert 'No local evidence' in run['error']
    assert run['events'][-1]['type'] == 'run.failed'
    assert checked(client.get(f'/v1/projects/{proj["id"]}'))['proposals'] == []


def test_portable_export_includes_project_config_and_markdown_without_runtime_state(client, runtime):
    from backend.scheduling import SourceWatch
    from tests.test_processing import config_with

    proj = project(client, 'Portable research')
    ingestion(client, proj['id'])
    pipeline = checked(client.post('/v1/pipelines', json={
        'project_id': proj['id'], 'name': 'Versioned pipeline', 'config': config_with()}), 201)
    watch = checked(client.post('/v1/source-watches', json={
        'project_id': proj['id'], 'locator': 'https://example.org/feed', 'source_type': 'rss',
        'enabled': False, 'research_question': 'What changed?', 'pipeline_config': config_with()}), 201)
    with runtime.session_factory() as session:
        row = session.get(SourceWatch, watch['id'])
        row.connector_state = {'private_cursor': 'excluded-runtime-state'}
        row.schedule_version = 'excluded-schedule-state'
        session.commit()
    other = project(client, 'Other project')
    checked(client.post('/v1/pipelines', json={
        'project_id': other['id'], 'name': 'Other pipeline', 'config': config_with()}), 201)
    exported = client.get(f'/v1/projects/{proj["id"]}/export')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        resources = manifest['resources']
        assert [p['id'] for p in resources['pipeline_revisions']] == [pipeline['id']]
        assert resources['source_watches'][0]['id'] == watch['id']
        assert resources['source_watches'][0]['enabled'] is False
        assert 'connector_state' not in resources['source_watches'][0]
        assert 'schedule_version' not in resources['source_watches'][0]
        assert 'subscriptions' not in resources
        for name in ('INDEX', 'BRIEF', 'REPORT', 'QUESTIONS', 'CHANGELOG', 'SOURCES'):
            assert archive.read(f'{name}.md').decode().startswith('# ')
        assert 'Other project' not in archive.read('manifest.json').decode()
