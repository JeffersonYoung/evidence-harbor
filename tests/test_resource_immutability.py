"""Source evidence is append-only even through ordinary ORM mutation paths."""
import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.db import Base, make_engine
from backend.models import (
    Block,
    Capture,
    ClaimEvidence,
    ClaimRecord,
    Document,
    DocumentVersion,
    Evidence,
    FetchObservation,
    ImmutableResourceError,
    IndexGeneration,
    Project,
    Proposal,
    Representation,
    ResearchRun,
    RunEvent,
    Source,
)
from tests.helpers import digest


@pytest.fixture
def records():
    engine = make_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        project = Project(workspace_id='test', name='Immutable evidence')
        session.add(project)
        session.flush()
        scope = {'workspace_id': 'test', 'project_id': project.id}
        source = Source(**scope, kind='text', canonical_uri='text://fixture', title='Fixture')
        session.add(source)
        session.flush()
        capture = Capture(**scope, source_id=source.id, content_hash=digest('Evidence.'),
                          media_type='text/plain', blob_path='fixture', byte_size=9)
        session.add(capture)
        session.flush()
        representation = Representation(**scope, capture_id=capture.id, text='Evidence.',
                                        content_hash=digest('Evidence.'))
        session.add(representation)
        session.flush()
        block = Block(**scope, representation_id=representation.id, ordinal=0, text='Evidence.',
                      start_offset=0, end_offset=9, content_hash=digest('Evidence.'))
        index = IndexGeneration(**scope, representation_id=representation.id)
        session.add_all([block, index])
        session.flush()
        evidence = Evidence(**scope, block_id=block.id, capture_id=capture.id, quote='Evidence.',
                            start_offset=0, end_offset=9, content_hash=digest('Evidence.'))
        session.add(evidence)
        document = Document(**scope, title='Source', content='Evidence.', kind='source',
                            source_id=source.id, capture_id=capture.id, representation_id=representation.id)
        run = ResearchRun(**scope, question='What is the evidence?')
        proposal = Proposal(**scope, title='Draft', content='Evidence.', claims=[])
        observation = FetchObservation(**scope, source_id=source.id, capture_id=capture.id,
                                       status='changed', content_hash=capture.content_hash)
        session.add_all([document, run, proposal, observation])
        session.flush()
        revision = DocumentVersion(**scope, document_id=document.id, version=1,
                                   title='Source', content='Evidence.', representation_id=representation.id)
        event = RunEvent(**scope, run_id=run.id, sequence=1, type='run.created', data={})
        claim = ClaimRecord(**scope, proposal_id=proposal.id, ordinal=0, text='Evidence.')
        session.add_all([revision, event, claim])
        session.flush()
        link = ClaimEvidence(**scope, claim_id=claim.id, evidence_id=evidence.id)
        session.add(link)
        session.commit()
        yield session, [source, capture, representation, block, index, evidence, revision, event, observation, link]
    engine.dispose()


@pytest.mark.parametrize('record_index', range(6), ids=['source', 'capture', 'representation', 'block', 'index', 'evidence'])
@pytest.mark.parametrize('mutation', ['update', 'delete'])
def test_source_resource_rejects_mutation(records, record_index, mutation):
    session, resources = records
    resource = resources[record_index]
    identity, old_workspace = resource.id, resource.workspace_id
    if mutation == 'update':
        resource.workspace_id = 'tampered'
    else:
        session.delete(resource)
    with pytest.raises(ImmutableResourceError, match='immutable'):
        session.commit()
    session.rollback()
    persisted = session.get(type(resource), identity)
    assert persisted is not None
    assert persisted.workspace_id == old_workspace


@pytest.mark.parametrize('record_index', range(10), ids=[
    'source', 'capture', 'representation', 'block', 'index', 'evidence', 'version', 'event', 'observation', 'claim_link'])
@pytest.mark.parametrize('mutation', ['update', 'delete'])
def test_database_triggers_reject_bulk_sql_mutation(records, record_index, mutation):
    session, resources = records
    resource = resources[record_index]
    model, identity, workspace = type(resource), resource.id, resource.workspace_id
    statement = (update(model).where(model.id == identity).values(workspace_id='tampered')
                 if mutation == 'update' else delete(model).where(model.id == identity))
    with pytest.raises(DBAPIError, match='immutable'):
        session.execute(statement)
        session.commit()
    session.rollback()
    persisted = session.get(model, identity)
    assert persisted is not None and persisted.workspace_id == workspace
