"""Known provider mistakes can be corrected without overwriting their provenance."""
import io
import json
import zipfile

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from backend.scholarly import WorkMetadataReview, WorkObservation
from tests.test_api_workflow import checked, project
from tests.test_scholarly_leads import batch, detail, work


def correction(**changes):
    return {'expected_revision': 0, 'changes': changes or {'authors': ['David Bailey', 'Heqing Zhu']},
            'reason': 'Verified authors against the actual paper and publisher metadata',
            'source_url': 'https://doi.org/10.1234/reviewed-paper'}


def test_review_corrects_current_catalogue_export_but_preserves_provider_and_reingestion(client, runtime):
    proj = project(client)
    supplied = work(doi='10.1234/reviewed-paper', authors=['David Bailey', 'Caroline Zhu'])
    lead = batch(client, proj['id'], [supplied])['items'][0]
    path = f'/v1/discovered-works/{lead["id"]}/metadata-reviews'
    reviewed = checked(client.post(path, json=correction()), 201)
    assert reviewed['authors'] == ['David Bailey', 'Heqing Zhu']
    assert reviewed['provider_display']['authors'] == supplied['authors']
    assert reviewed['review_revision'] == 1 and reviewed['review_status'] == 'reviewed_metadata'
    assert reviewed['metadata_reviews'][0]['actor_json']['role'] == 'admin'
    assert reviewed['fulltext_read'] is False and reviewed['evidence_eligible'] is False
    assert client.post(path, json=correction(title='Stale correction')).status_code == 409
    batch(client, proj['id'], [{**supplied, 'metadata': {'refetched': True}}])
    current = detail(client, lead['id'])
    assert current['authors'] == reviewed['authors'] and len(current['observations']) == 2
    assert all(row['payload']['authors'] == supplied['authors'] for row in current['observations'])
    second = correction(title='Reviewed canonical title', venue='Verified Journal', year=2024)
    second['expected_revision'] = 1
    current = checked(client.post(path, json=second), 201)
    assert current['review_revision'] == 2 and current['authors'] == reviewed['authors']
    assert current['venue'] == 'Verified Journal' and current['year'] == 2024
    listing = checked(client.get('/v1/discovered-works', params={'project_id': proj['id']}))
    assert listing['items'][0]['title'] == 'Reviewed canonical title'
    exported = client.get(f'/v1/projects/{proj["id"]}/export')
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    assert manifest['resources']['discovered_works'][0]['authors'] == reviewed['authors']
    assert len(manifest['resources']['scholarly_metadata_reviews']) == 2
    with runtime.session_factory() as session:
        original = session.scalar(select(WorkObservation).where(WorkObservation.work_id == lead['id']))
        assert original.payload['authors'] == supplied['authors']
        with pytest.raises(DBAPIError):
            session.execute(update(WorkMetadataReview).values(reason='Rewrite history'))
            session.commit()
        session.rollback()


def test_review_requires_editor_support_and_same_workspace(client, runtime):
    proj = project(client)
    lead = batch(client, proj['id'], [work()])['items'][0]
    path = f'/v1/discovered-works/{lead["id"]}/metadata-reviews'
    runtime.settings.api_tokens['researcher-review'] = {'workspace_id': 'workspace-a', 'role': 'researcher'}
    assert client.post(path, json=correction(), headers={'Authorization': 'Bearer researcher-review'}).status_code == 403
    assert client.post(path, json=correction(), headers={'Authorization': 'Bearer owner-b'}).status_code == 404
    missing = correction()
    missing.pop('source_url')
    assert client.post(path, json=missing).status_code == 422
    fabricated = correction()
    fabricated['changes']['fulltext_ready'] = True
    assert client.post(path, json=fabricated).status_code == 422
    assert detail(client, lead['id'])['review_revision'] == 0
