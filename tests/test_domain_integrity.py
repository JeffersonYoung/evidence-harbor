"""Real service/pipeline transactions verify citation and publication invariants."""
import io
import json
import zipfile
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend import domain
from backend import models as m
from tests.helpers import digest


def make_project(runtime, workspace='workspace-a', name='Research project'):
    with runtime.session_factory() as session:
        project = domain.create_project(session, workspace, name)
        session.commit()
        return project.id


def ingest_text(runtime, project_id, text='Policy evidence shows a measurable reduction.', key=None):
    with runtime.session_factory() as session:
        operation = domain.create_operation(session, 'workspace-a', project_id,
                                            {'type': 'text', 'text': text, 'title': 'Primary source'},
                                            idempotency_key=key)
        session.commit()
        operation_id = operation.id
    result = domain.execute_operation(operation_id, runtime.session_factory)
    assert result['status'] == 'succeeded', result
    return result


def evidence_for(runtime, project_id, operation, quote='measurable reduction'):
    with runtime.session_factory() as session:
        evidence = domain.create_evidence(session, 'workspace-a', project_id,
                                          operation['result_json']['block_ids'][0], quote)
        session.commit()
        return evidence.id


def test_ingestion_idempotency_and_exact_evidence_integrity(runtime):
    project_id = make_project(runtime)
    raw = '政策证据：风险降低 20%。\n\nEvidence shows a measurable reduction.'
    first = ingest_text(runtime, project_id, raw, key='ingest-once')
    second = ingest_text(runtime, project_id, raw, key='ingest-once')
    assert second['id'] == first['id']
    assert second['result_json'] == first['result_json']
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.Capture)) == 1
        assert session.scalar(select(func.count()).select_from(m.OperationDispatch)) == 1
        capture = session.get(m.Capture, first['result_json']['capture_id'])
        assert capture.content_hash == digest(raw)
        rep = session.get(m.Representation, first['result_json']['representation_id'])
        assert rep.content_hash == digest(rep.text)
        block = session.get(m.Block, first['result_json']['block_ids'][0])
        quote = '风险降低 20%'
        evidence = domain.create_evidence(session, 'workspace-a', project_id, block.id, quote)
        session.commit()
        assert evidence.content_hash == digest(quote)
        assert block.text[evidence.start_offset:evidence.end_offset] == quote
        assert rep.text[evidence.locator_json['representation_start_offset']:
                        evidence.locator_json['representation_end_offset']] == quote
        assert evidence.capture_id == capture.id
        assert domain.validate_evidence(session, 'workspace-a', project_id, [evidence.id]) == [evidence]
        hits = domain.search(session, 'workspace-a', project_id, '风险降低')['results']
        assert any(hit['block_id'] == block.id for hit in hits)
        with pytest.raises(domain.DomainError) as conflict:
            domain.create_operation(session, 'workspace-a', project_id,
                                    {'type': 'text', 'text': 'Different request'}, idempotency_key='ingest-once')
        assert conflict.value.status_code == 409


@pytest.mark.parametrize('quote,start', [('Invented conclusion', None), ('measurable reduction', 0)])
def test_evidence_rejects_non_exact_quote_and_wrong_offset(runtime, quote, start):
    project_id = make_project(runtime)
    operation = ingest_text(runtime, project_id)
    with runtime.session_factory() as session:
        with pytest.raises(domain.DomainError) as error:
            domain.create_evidence(session, 'workspace-a', project_id,
                                   operation['result_json']['block_ids'][0], quote, start)
        assert error.value.status_code == 422
        assert session.scalar(select(func.count()).select_from(m.Evidence)) == 0


@pytest.mark.parametrize('wrong_reference', ['missing', 'other_project', 'other_workspace'])
def test_proposal_rejects_nonexistent_or_out_of_scope_citations(runtime, wrong_reference):
    project_id = make_project(runtime)
    operation = ingest_text(runtime, project_id)
    evidence_id = evidence_for(runtime, project_id, operation)
    if wrong_reference == 'missing':
        evidence_id = str(uuid4())
        target_project, workspace = project_id, 'workspace-a'
    elif wrong_reference == 'other_project':
        target_project, workspace = make_project(runtime), 'workspace-a'
    else:
        workspace = 'workspace-b'
        target_project = make_project(runtime, workspace)
    with runtime.session_factory() as session:
        with pytest.raises(domain.DomainError) as error:
            domain.create_proposal(session, workspace, target_project, 'Unpublishable',
                                   'A measurable reduction was observed.',
                                   [{'text': 'A measurable reduction was observed.', 'evidence_ids': [evidence_id]}])
        assert error.value.status_code == 404
        assert session.scalar(select(func.count()).select_from(m.Proposal)) == 0


def test_compare_and_swap_publication_preserves_prior_versions_and_export(runtime):
    project_id = make_project(runtime)
    operation = ingest_text(runtime, project_id)
    evidence_id = evidence_for(runtime, project_id, operation)
    claim = 'A measurable reduction was observed.'
    claims = [{'text': claim, 'evidence_ids': [evidence_id]}]
    with runtime.session_factory() as session:
        initial = domain.create_proposal(session, 'workspace-a', project_id, 'Initial report', claim, claims)
        document = domain.publish_proposal(session, 'workspace-a', initial.id)
        session.commit()
        doc_id, initial_id = document.id, initial.id
        assert document.version == 1
        assert document.kind == 'report'
        repeated = domain.publish_proposal(session, 'workspace-a', initial_id)
        session.commit()
        assert repeated.id == doc_id and repeated.version == 1
        next_content = claim + '\n\nMethod limits remain.'
        stale_content = claim + '\n\nConflicting concurrent edit.'
        first = domain.create_proposal(session, 'workspace-a', project_id, 'Revision two', next_content,
                                       claims, doc_id, 1)
        stale = domain.create_proposal(session, 'workspace-a', project_id, 'Stale revision', stale_content,
                                       claims, doc_id, 1)
        session.commit()
        first_id, stale_id = first.id, stale.id
    with runtime.session_factory() as session:
        published = domain.publish_proposal(session, 'workspace-a', first_id, expected_version=1)
        session.commit()
        assert published.version == 2
        assert published.content == next_content
    with runtime.session_factory() as session:
        with pytest.raises(domain.DomainError) as conflict:
            domain.publish_proposal(session, 'workspace-a', stale_id, expected_version=1)
        assert conflict.value.status_code == 409
        session.rollback()
        assert session.get(m.Proposal, stale_id).status == 'pending'
        assert session.get(m.Document, doc_id).content == next_content
        versions = list(session.scalars(select(m.DocumentVersion).where(
            m.DocumentVersion.document_id == doc_id).order_by(m.DocumentVersion.version)))
        assert [(version.version, version.content) for version in versions] == [(1, claim), (2, next_content)]
        assert all(version.evidence_ids == [evidence_id] for version in versions)
        archive_bytes = domain.export_project(session, 'workspace-a', project_id)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['project']['id'] == project_id
        saved_evidence = {item['id']: item for item in manifest['resources']['evidence']}
        assert saved_evidence[evidence_id]['quote'] == 'measurable reduction'
        assert archive.read(f'documents/{doc_id}.md').decode() == next_content
        assert len(manifest['objects']) == 1
        for obj in manifest['objects']:
            raw = archive.read(obj['path'])
            assert digest(raw) == obj['sha256']
            assert len(raw) == obj['size']
        assert all('blob_path' not in capture for capture in manifest['resources']['captures'])


def test_source_documents_cannot_be_replaced_by_proposal(runtime):
    project_id = make_project(runtime)
    operation = ingest_text(runtime, project_id)
    evidence_id = evidence_for(runtime, project_id, operation)
    with (runtime.session_factory() as session,
          pytest.raises(domain.DomainError, match='Source documents are immutable')):
        domain.create_proposal(session, 'workspace-a', project_id, 'Unsafe replacement', 'Replacement.',
                               [{'text': 'Replacement.', 'evidence_ids': [evidence_id]}],
                               operation['result_json']['document_id'], 1)


def test_failed_ingestion_is_observable_and_cannot_publish_partial_assets(runtime):
    project_id = make_project(runtime)
    with runtime.session_factory() as session:
        operation = domain.create_operation(session, 'workspace-a', project_id,
                                            {'type': 'text', 'text': '     ', 'title': 'Empty source'})
        session.commit()
        operation_id = operation.id
    failed = domain.execute_operation(operation_id, runtime.session_factory)
    assert failed['status'] == 'failed'
    assert failed['error']
    assert failed['result_json']['archival_complete'] is True
    assert failed['result_json']['capture_id']
    repeated = domain.execute_operation(operation_id, runtime.session_factory)
    assert repeated['status'] == 'failed'
    with runtime.session_factory() as session:
        # Raw archival is durable; a failed quality gate publishes no derived resources.
        assert session.scalar(select(func.count()).select_from(m.Capture)) == 1
        for model in (m.Representation, m.Block, m.Document, m.IndexGeneration):
            assert session.scalar(select(func.count()).select_from(model)) == 0
