"""Changed sources keep old anchors; unchanged fetches add observations, not fake versions."""
from pathlib import Path

from backend import models as m
from tests.helpers import digest
from tests.test_api_workflow import checked, evidence, ingestion, project


def test_saved_web_source_tracks_unchanged_then_changed_content_without_moving_evidence(client):
    proj = project(client)
    old = b'<article><p>Original observed policy evidence.</p></article>'
    new = b'<article><p>Revised observed policy evidence with limitations.</p></article>'

    def upload(raw, key):
        request = checked(client.post('/v1/ingestions/upload',
            data={'project_id': proj['id'], 'original_url': 'https://example.org/report#citation'},
            files={'file': ('saved-page.html', raw, 'text/html')}, headers={'Idempotency-Key': key}), 202)
        result = checked(client.get(f'/v1/operations/{request["id"]}'))
        assert result['status'] == 'succeeded', result['error']
        return result

    first = upload(old, 'first-fetch')
    ev = evidence(client, proj['id'], first, quote='Original observed policy evidence.')
    anchored = checked(client.get(f'/v1/evidence/{ev["id"]}'))
    unchanged = upload(old, 'unchanged-fetch')
    changed = upload(new, 'changed-fetch')
    assert unchanged['id'] != first['id']
    assert unchanged['result_json']['capture_id'] == first['result_json']['capture_id']
    assert unchanged['result_json']['representation_id'] == first['result_json']['representation_id']
    assert changed['result_json']['source_id'] == first['result_json']['source_id']
    assert changed['result_json']['capture_id'] != first['result_json']['capture_id']
    assert checked(client.get(f'/v1/evidence/{ev["id"]}')) == anchored
    captures = checked(client.get(f'/v1/sources/{first["result_json"]["source_id"]}/captures'))
    assert len(captures) == 2
    newest = checked(client.get(f'/v1/captures/{changed["result_json"]["capture_id"]}'))
    original = checked(client.get(f'/v1/captures/{first["result_json"]["capture_id"]}'))
    assert newest['previous_capture_id'] == original['id']
    assert original['content_hash'] == digest(old) and newest['content_hash'] == digest(new)
    assert sorted(item['status'] for item in original['observations']) == ['changed', 'unchanged']
    assert newest['metadata_json']['fetched_by_application'] is False
    assert newest['metadata_json']['provenance'] == 'user_uploaded_saved_source'
    source = checked(client.get(f'/v1/sources/{first["result_json"]["source_id"]}'))
    assert source['canonical_uri'] == 'https://example.org/report'


def test_raw_corruption_blocks_publication_and_portable_export(client, runtime):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], operation)
    proposal = checked(client.post('/v1/proposals', json={
        'project_id': proj['id'], 'title': 'Uncorrupted report', 'content': ev['quote'],
        'claims': [{'text': ev['quote'], 'evidence_ids': [ev['id']]}]}), 201)
    with runtime.session_factory() as session:
        capture = session.get(m.Capture, operation['result_json']['capture_id'])
        raw = Path(capture.blob_path)
        raw.chmod(0o600)
        raw.write_bytes(b'Corrupted storage bytes')
    denied = client.post(f'/v1/proposals/{proposal["id"]}/publish', json={})
    assert denied.status_code == 422
    assert 'integrity' in denied.json()['detail']
    assert client.get(f'/v1/proposals/{proposal["id"]}').status_code == 422
    with runtime.session_factory() as session:
        assert session.get(m.Proposal, proposal['id']).status == 'pending'
    exported = client.get(f'/v1/projects/{proj["id"]}/export')
    assert exported.status_code == 500
    assert 'integrity' in exported.json()['detail']
