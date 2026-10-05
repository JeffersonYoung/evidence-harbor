"""Source deltas are review signals; historical anchors and publication authority stay intact."""
from sqlalchemy import func, select

from backend import continuous, domain, providers
from backend import models as m
from backend.scheduling import SourceWatch
from tests.helpers import digest
from tests.test_domain_integrity import make_project


def save_version(runtime, project_id, raw, text, *, parser='fixture-parser-v1', config_hash='config-one', watch_id=None):
    with runtime.session_factory() as session:
        operation = domain.create_operation(session, 'workspace-a', project_id,
            {'type': 'text', 'text': text, 'raw_hash': digest(raw), 'config_hash': config_hash,
             **({'watch_id': watch_id} if watch_id else {})})
        result = domain.store_capture(session, 'workspace-a', project_id, 'https://example.org/source', 'url',
                                      'Watched source', raw, 'text/html', text,
                                      parser=parser, metadata={'config_hash': config_hash})
        change = continuous.after_ingestion(session, operation, result)
        domain.complete_operation(session, operation, result)
        session.commit()
        return operation.id, result, change


def test_initial_and_html_byte_noise_are_not_claimed_as_body_changes(runtime):
    project_id = make_project(runtime)
    _, first, initial = save_version(runtime, project_id, b'<p>Policy evidence</p>', 'Policy evidence')
    _, second, change = save_version(runtime, project_id, b'<p class="new">Policy evidence</p>', 'Policy evidence')
    assert initial['kind'] == 'initial' and initial['body_changed'] is None
    assert change['kind'] == 'bytes_only'
    assert change['bytes_changed'] is True and change['body_changed'] is False
    assert change['processing_changed'] is False
    assert change['question_id'] is None and change['research_run_id'] is None
    assert first['capture_id'] != second['capture_id']


def test_changed_body_identifies_affected_claim_without_rewriting_old_evidence(runtime):
    project_id = make_project(runtime)
    _, first, _ = save_version(runtime, project_id, b'<p>Original evidence.</p>', 'Original evidence.')
    with runtime.session_factory() as session:
        evidence = domain.create_evidence(session, 'workspace-a', project_id, first['block_ids'][0], 'Original evidence.')
        proposal = domain.create_proposal(session, 'workspace-a', project_id, 'Prior conclusion', 'Original finding.',
            [{'text': 'Original finding.', 'evidence_ids': [evidence.id]}])
        session.commit()
        old_anchor = domain.serialize(evidence)
        claim_id = session.scalar(select(m.ClaimRecord.id).where(m.ClaimRecord.proposal_id == proposal.id))
    _, _, change = save_version(runtime, project_id, b'<p>Revised evidence.</p>', 'Revised evidence.')
    assert change['kind'] == 'body_changed' and change['body_changed'] is True
    impact = change['impact']
    assert impact['affected_claim_ids'] == [claim_id]
    assert [item['evidence_id'] for item in impact['affected_evidence']] == [old_anchor['id']]
    assert impact['semantic_entailment_assessed'] is False
    assert impact['historical_anchors_unchanged'] is True
    with runtime.session_factory() as session:
        persisted = domain.serialize(session.get(m.Evidence, old_anchor['id']))
        for field in ('quote', 'content_hash', 'block_id', 'capture_id', 'start_offset', 'end_offset'):
            assert persisted[field] == old_anchor[field]
        question = session.get(m.Question, change['question_id'])
        assert question.priority == 'high' and question.status == 'open'
        assert question.evidence_ids == [old_anchor['id']]
        assert question.evidence_gaps


def test_processing_change_is_explicitly_incomparable(runtime):
    project_id = make_project(runtime)
    save_version(runtime, project_id, b'<p>Policy evidence.</p>', 'Policy evidence.')
    _, _, change = save_version(runtime, project_id, b'<p>Policy evidence.</p>', 'Policy evidence.',
                                 parser='fixture-parser-v2', config_hash='config-two')
    assert change['kind'] == 'processing_changed'
    assert change['processing_changed'] is True
    assert change['bytes_changed'] is False
    assert change['body_changed'] is None
    assert change['impact']['comparable_processing'] is False
    assert change['research_run_id'] is None


def test_watch_change_queues_one_bounded_review_only_run_and_no_repeat_for_unchanged(runtime):
    project_id = make_project(runtime)
    with runtime.session_factory() as session:
        watch = SourceWatch(workspace_id='workspace-a', project_id=project_id, locator='https://example.org/source',
                            research_on_change=True, research_question='Review changed policy evidence',
                            research_config={'provider': 'local', 'max_searches': 10, 'max_documents': 30,
                                             'max_tool_calls': 100, 'max_duration_seconds': 900, 'max_cost_usd': 100})
        session.add(watch)
        session.commit()
        watch_id = watch.id
    save_version(runtime, project_id, b'Original evidence.', 'Original evidence.', watch_id=watch_id)
    operation_id, result, changed = save_version(runtime, project_id, b'Revised evidence.', 'Revised evidence.', watch_id=watch_id)
    assert changed['research_run_id'] and changed['research_operation_id']
    with runtime.session_factory() as session:
        run = session.get(m.ResearchRun, changed['research_run_id'])
        assert run.status == 'pending'
        assert run.config_json['max_searches'] == 3
        assert run.config_json['max_documents'] == 10
        assert run.config_json['max_tool_calls'] == 20
        assert run.config_json['max_duration_seconds'] == 180
        assert run.config_json['max_cost_usd'] == 5.0
        repeated = continuous.after_ingestion(session, session.get(m.Operation, operation_id), result)
        session.commit()
        assert repeated['research_run_id'] == run.id
        assert session.scalar(select(func.count()).select_from(m.ResearchRun)) == 1
        assert session.scalar(select(func.count()).select_from(m.Proposal)) == 0
    _, _, unchanged = save_version(runtime, project_id, b'Revised evidence.', 'Revised evidence.', watch_id=watch_id)
    assert unchanged['kind'] == 'unchanged'
    assert unchanged['research_run_id'] is None
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.ResearchRun)) == 1


def test_discovery_disabled_or_exhausted_never_calls_external_provider(monkeypatch):
    monkeypatch.setattr(providers, 'get_search_provider', lambda name: (_ for _ in ()).throw(AssertionError('Unexpected provider')))
    assert continuous.discover_sources('Question', {})['enabled'] is False
    assert continuous.discover_sources('Question', {'web_discovery': 'true'})['enabled'] is False
    exhausted = continuous.discover_sources('Question', {'web_discovery': True, 'max_searches': 1}, used_searches=1)
    assert exhausted['budget_exhausted'] is True and exhausted['searches_used'] == 0


def test_discovery_bounds_deduplicates_candidates_and_never_calls_them_evidence(monkeypatch):
    calls = []

    class Search:
        name = 'fixture-discovery'

        def search(self, query, limit):
            calls.append((query, limit))
            urls = ['https://example.org/shared', f'https://example.org/{len(calls)}']
            return [providers.SearchResult('Lead', url, 'Unverified snippet', self.name) for url in urls[:limit]]

    monkeypatch.setattr(providers, 'get_search_provider', lambda name: Search())
    result = continuous.discover_sources('Research question', {
        'web_discovery': True, 'search_provider': 'fixture', 'max_searches': 2, 'max_documents': 4,
        'max_duration_seconds': 60, 'discovery_queries': ['first explicit query', 'second explicit query', 'ignored']})
    assert [query for query, _ in calls] == ['first explicit query', 'second explicit query']
    urls = [item['url'] for item in result['candidates']]
    assert len(urls) == len(set(urls)) == 3
    assert result['searches_used'] == 2
    assert result['evidence_ready'] is False
    assert result['budget_exhausted'] is True
    assert all('evidence_id' not in item for item in result['candidates'])
