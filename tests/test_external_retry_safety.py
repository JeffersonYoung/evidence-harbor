"""Transaction tests with a fake billable provider: retries must not repeat paid calls."""
from sqlalchemy import func, select

from backend import domain, providers
from backend import models as m
from tests.test_domain_integrity import ingest_text, make_project


def prepare_run(runtime):
    project_id = make_project(runtime)
    ingest_text(runtime, project_id, 'Policy evidence shows measurable effects.')
    with runtime.session_factory() as session:
        run, operation = domain.create_research_run(session, 'workspace-a', project_id, 'Policy evidence',
                                                    provider='openai', max_cost_usd=1.0)
        session.commit()
        return run.id, operation.id


def test_uncertain_external_call_is_not_repeated_on_operation_retry(runtime, monkeypatch):
    _, operation_id = prepare_run(runtime)
    calls = []

    class UncertainProvider:
        def research(self, **kwargs):
            calls.append(kwargs)
            raise providers.ProviderError('Remote acceptance unknown')

    monkeypatch.setattr(providers, 'get_provider', lambda name: UncertainProvider())
    first = domain.execute_operation(operation_id, runtime.session_factory)
    assert first['status'] == 'failed'
    retried = domain.execute_operation(operation_id, runtime.session_factory, force_resume=True)
    assert retried['status'] == 'failed'
    assert 'earlier external call' in retried['error']
    assert len(calls) == 1
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.ExternalCallReservation)) == 1
        assert session.scalar(select(func.count()).select_from(m.Proposal)) == 0


def test_successful_external_call_caches_and_stages_without_duplicate_calls(runtime, monkeypatch):
    run_id, operation_id = prepare_run(runtime)
    calls = []

    class SuccessfulProvider:
        def research(self, **kwargs):
            calls.append(kwargs)
            return providers.LocalExtractiveProvider().research(**kwargs)

    monkeypatch.setattr(providers, 'get_provider', lambda name: SuccessfulProvider())
    finished = domain.execute_operation(operation_id, runtime.session_factory)
    assert finished['status'] == 'succeeded', finished['error']
    repeated = domain.execute_operation(operation_id, runtime.session_factory, force_resume=True)
    assert repeated['result_json'] == finished['result_json']
    assert len(calls) == 1
    with runtime.session_factory() as session:
        reservation = session.scalar(select(m.ExternalCallReservation))
        assert reservation.status == 'cached' and reservation.output_json
        run = session.get(m.ResearchRun, run_id)
        assert run.status == 'succeeded'
        proposal = session.get(m.Proposal, run.result_json['proposal_id'])
        assert proposal.status == 'pending'
        assert proposal.published_document_id is None
        assert session.scalar(select(func.count()).select_from(m.Proposal)) == 1
