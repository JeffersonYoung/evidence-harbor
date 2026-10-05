"""Optional real PostgreSQL checks, isolated in a fresh private schema per test.

Run with EVIDENCEHARBOR_TEST_POSTGRES=postgresql+psycopg://... or use the
integration harness. These tests never drop or modify the public schema.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from backend import config, db, domain
from backend import models as m
from tests.test_domain_integrity import evidence_for, ingest_text, make_project

POSTGRES_URL = os.getenv('EVIDENCEHARBOR_TEST_POSTGRES')
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason='Set EVIDENCEHARBOR_TEST_POSTGRES for real PostgreSQL tests')


@pytest.fixture
def pg_runtime(tmp_path, monkeypatch):
    schema = 'regression_' + uuid4().hex
    administrative = create_engine(POSTGRES_URL)
    with administrative.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(POSTGRES_URL, connect_args={'options': f'-csearch_path={schema},public'})
    settings = config.Settings(database_url=POSTGRES_URL, blob_dir=tmp_path / 'blobs',
                               demo_mode=False, api_tokens={'owner-a': 'workspace-a'}, inline_worker=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False, info={'settings': settings})
    monkeypatch.setattr(config, 'settings', settings)
    monkeypatch.setattr(domain, 'settings', settings)
    monkeypatch.setenv('STORAGE_BACKEND', 'local')
    monkeypatch.setenv('RAW_STORAGE_DIR', str(settings.blob_dir))
    try:
        db.init_db(engine)
        yield SimpleNamespace(settings=settings, engine=engine, session_factory=factory)
    finally:
        engine.dispose()
        with administrative.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administrative.dispose()


def test_postgresql_fts_indexes_and_retrieves_english_and_chinese(pg_runtime):
    project_id = make_project(pg_runtime)
    op = ingest_text(pg_runtime, project_id, 'Tree canopy reduces heat.\n\n政策证据：风险降低 20%。')
    with pg_runtime.session_factory() as session:
        english = domain.search(session, 'workspace-a', project_id, 'tree canopy')['results']
        chinese = domain.search(session, 'workspace-a', project_id, '风险降低')['results']
        assert english and chinese
        assert 'Tree canopy reduces heat.' in english[0]['text']
        assert '风险降低' in chinese[0]['text']
        assert all(hit['capture_id'] == op['result_json']['capture_id'] for hit in english + chinese)
        index_def = session.scalar(text("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'ix_blocks_fts'"))
        assert index_def and 'USING gin' in index_def


def test_postgresql_concurrent_publication_has_exactly_one_cas_winner(pg_runtime):
    project_id = make_project(pg_runtime)
    op = ingest_text(pg_runtime, project_id)
    evidence_id = evidence_for(pg_runtime, project_id, op)
    claim = 'Policy evidence records measurable results.'
    claims = [{'text': claim, 'evidence_ids': [evidence_id]}]
    with pg_runtime.session_factory() as session:
        initial = domain.create_proposal(session, 'workspace-a', project_id, 'Report', claim, claims)
        report = domain.publish_proposal(session, 'workspace-a', initial.id)
        session.commit()
        document_id = report.id
        proposals = [domain.create_proposal(session, 'workspace-a', project_id, f'Revision {number}',
                     claim + f'\n\nInterpretation {number}.', claims, document_id, 1) for number in (1, 2)]
        session.commit()
        proposal_ids = [proposal.id for proposal in proposals]
    barrier = Barrier(2)

    def publish(proposal_id):
        with pg_runtime.session_factory() as session:
            barrier.wait(timeout=10)
            try:
                report = domain.publish_proposal(session, 'workspace-a', proposal_id, 1)
                session.commit()
                return 200, report.content
            except domain.DomainError as error:
                session.rollback()
                return error.status_code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, proposal_ids))
    assert sorted(status for status, _ in results) == [200, 409]
    with pg_runtime.session_factory() as session:
        report = session.get(m.Document, document_id)
        assert report.version == 2
        assert report.content == next(content for status, content in results if status == 200)
        assert session.scalar(select(func.count()).select_from(m.DocumentVersion).where(
            m.DocumentVersion.document_id == document_id)) == 2
        assert sorted(session.get(m.Proposal, identifier).status for identifier in proposal_ids) == ['pending', 'published']


def test_postgresql_worker_redelivery_does_not_duplicate_assets(pg_runtime):
    project_id = make_project(pg_runtime)
    with pg_runtime.session_factory() as session:
        operation = domain.create_operation(session, 'workspace-a', project_id,
            {'type': 'text', 'text': 'Durable concurrent source evidence.'}, idempotency_key='worker-redelivery')
        session.commit()
        operation_id = operation.id
    barrier = Barrier(3)

    def execute(_):
        barrier.wait(timeout=10)
        return domain.execute_operation(operation_id, pg_runtime.session_factory, force_resume=True,
                                        settings_override=pg_runtime.settings)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(execute, range(3)))
    assert all(result['status'] == 'succeeded' for result in results)
    assert len({result['result_json']['capture_id'] for result in results}) == 1
    with pg_runtime.session_factory() as session:
        for model in (m.Capture, m.Representation, m.Document, m.IndexGeneration):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_postgresql_cached_model_response_recovers_after_transaction_failure(pg_runtime, monkeypatch):
    from backend import providers
    from tests.test_external_retry_safety import prepare_run

    _, operation_id = prepare_run(pg_runtime)
    paid_calls, proposal_attempts = [], []
    original_create = domain.create_proposal

    class SuccessfulProvider:
        def research(self, **kwargs):
            paid_calls.append(kwargs)
            return providers.LocalExtractiveProvider().research(**kwargs)

    def transient_proposal_failure(*args, **kwargs):
        proposal_attempts.append(True)
        if len(proposal_attempts) == 1:
            raise domain.DomainError('Simulated failure after durable provider result caching', 503)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(providers, 'get_provider', lambda name: SuccessfulProvider())
    monkeypatch.setattr(domain, 'create_proposal', transient_proposal_failure)
    failed = domain.execute_operation(operation_id, pg_runtime.session_factory)
    assert failed['status'] == 'failed'
    with pg_runtime.session_factory() as session:
        reservation = session.scalar(select(m.ExternalCallReservation))
        assert reservation.status == 'cached'
        assert session.scalar(select(func.count()).select_from(m.Evidence)) == 0
    recovered = domain.execute_operation(operation_id, pg_runtime.session_factory, force_resume=True)
    assert recovered['status'] == 'succeeded', recovered.get('error')
    assert len(paid_calls) == 1
    assert len(proposal_attempts) == 2
    with pg_runtime.session_factory() as session:
        proposal = session.get(m.Proposal, recovered['result_json']['proposal_id'])
        evidence_ids = [identifier for claim in proposal.claims for identifier in claim['evidence_ids']]
        assert set(evidence_ids) == {item['id'] for item in paid_calls[0]['evidence']}
        assert domain.validate_evidence(session, 'workspace-a', proposal.project_id, evidence_ids)


def test_postgresql_vector_http_runner_publishes_generation_and_hybrid_results(pg_runtime):
    from fastapi.testclient import TestClient

    from backend.api import create_app
    from backend.vector_index import create_schema
    from tests.test_api_workflow import checked

    with pg_runtime.engine.begin() as connection:
        create_schema(connection)
    project_id = make_project(pg_runtime)
    saved = ingest_text(pg_runtime, project_id, 'Tree canopy reduces heat. Policy evidence records benefits.')
    app = create_app(settings_override=pg_runtime.settings, session_factory=pg_runtime.session_factory)
    with TestClient(app, headers={'Authorization': 'Bearer owner-a'}) as client:
        requested = checked(client.post(
            f'/v1/representations/{saved["result_json"]["representation_id"]}/vector-index',
            json={'provider': 'local_hash'}), 202)
        finished = checked(client.get(f'/v1/operations/{requested["id"]}'))
        assert finished['status'] == 'succeeded', finished.get('error')
        assert finished['result_json']['semantic_model'] is False
        generation_id = finished['result_json']['index_generation_id']
        generations = checked(client.get(f'/v1/index-generations?project_id={project_id}'))['items']
        assert any(item['id'] == generation_id and item['index_kind'] == 'vector' for item in generations)
        hybrid = checked(client.post('/v1/hybrid-search', json={
            'project_id': project_id, 'query': 'tree canopy', 'generation_ids': [generation_id],
            'provider': 'local_hash'}))
        assert hybrid['mode'] == 'hybrid_rrf'
        assert hybrid['vector_available'] is True and hybrid['semantic_model'] is False
        assert hybrid['results'][0]['block_id'] in saved['result_json']['block_ids']
        assert hybrid['results'][0]['capture_id'] == saved['result_json']['capture_id']
