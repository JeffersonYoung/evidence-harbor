"""User review is consequential: partial acceptance, protected sections and historical claims."""
from sqlalchemy import select

from backend import models as m
from tests.test_api_workflow import checked, evidence, ingestion, project


def staged(client, project_id, evidence_id, content, claims, **target):
    return checked(client.post('/v1/proposals', json={
        'project_id': project_id, 'title': 'Reviewed report', 'content': content,
        'claims': [{'text': claim, 'evidence_ids': [evidence_id]} for claim in claims], **target}), 201)


def test_partial_acceptance_publishes_only_selected_claim_and_retains_review_history(client, runtime):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], operation)
    approved, declined = 'The study records reduced heat.', 'This interpretation requires more review.'
    proposal = staged(client, proj['id'], ev['id'], approved + '\n\n' + declined, [approved, declined])
    published = checked(client.post(f'/v1/proposals/{proposal["id"]}/publish',
                                    json={'accepted_claim_indices': [0]}))
    assert approved in published['content']
    assert declined not in published['content']
    saved = checked(client.get(f'/v1/proposals/{proposal["id"]}'))
    assert saved['accepted_claim_indices'] == [0]
    assert declined in saved['content']
    with runtime.session_factory() as session:
        claims = list(session.scalars(select(m.ClaimRecord).where(
            m.ClaimRecord.proposal_id == proposal['id']).order_by(m.ClaimRecord.ordinal)))
        assert [claim.review_status for claim in claims] == ['accepted', 'rejected']
        assert all(claim.last_reviewed_at is not None for claim in claims)
        revision = session.scalar(select(m.DocumentVersion).where(
            m.DocumentVersion.document_id == published['id']))
        assert revision.claim_ids == [claims[0].id]
        assert revision.evidence_ids == [ev['id']]


def test_invalid_partial_acceptance_does_not_publish(client):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], operation)
    proposal = staged(client, proj['id'], ev['id'], ev['quote'], [ev['quote']])
    for selected in ([], [-1], [1]):
        response = client.post(f'/v1/proposals/{proposal["id"]}/publish', json={'accepted_claim_indices': selected})
        assert response.status_code == 422
    assert checked(client.get(f'/v1/proposals/{proposal["id"]}'))['status'] == 'pending'


def test_manual_section_lock_blocks_changes_and_allows_unlocked_edit(client):
    proj = project(client)
    operation = ingestion(client, proj['id'])
    ev = evidence(client, proj['id'], operation)
    claim = ev['quote']
    baseline = '# Findings\n' + claim + '\n\n# Notes\nOriginal caveat.\n'
    initial = staged(client, proj['id'], ev['id'], baseline, [claim])
    report = checked(client.post(f'/v1/proposals/{initial["id"]}/publish', json={}))
    locked = checked(client.patch(f'/v1/documents/{report["id"]}/locks', json={'sections': ['Findings']}))
    assert locked['locked_sections'] == ['Findings']
    target = {'target_document_id': report['id'], 'base_version': 1}
    unsafe = staged(client, proj['id'], ev['id'], baseline.replace(claim, claim + ' Altered interpretation.'),
                    [claim], **target)
    denied = client.post(f'/v1/proposals/{unsafe["id"]}/publish', json={'expected_version': 1})
    assert denied.status_code == 409
    assert 'locked' in denied.json()['detail']
    assert checked(client.get(f'/v1/documents/{report["id"]}'))['content'] == baseline
    changed_notes = baseline.replace('Original caveat.', 'Updated caveat.')
    allowed = staged(client, proj['id'], ev['id'], changed_notes, [claim], **target)
    updated = checked(client.post(f'/v1/proposals/{allowed["id"]}/publish', json={'expected_version': 1}))
    assert updated['version'] == 2 and updated['content'] == changed_notes
    old = checked(client.get(f'/v1/documents/{report["id"]}/content?version=1'))
    assert old['content'] == baseline
    assert client.patch(f'/v1/documents/{report["id"]}/locks', json={'sections': ['Missing heading']}).status_code == 422
