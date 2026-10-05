"""Mock external discovery/HTTP only; candidate parsing, storage, evidence and proposals are real."""
import pytest
from sqlalchemy import func, select

from backend import models as m
from backend import processing, providers
from backend.security import FetchError, FetchResult
from tests.helpers import digest
from tests.test_api_workflow import checked, project


@pytest.fixture
def external_sources(monkeypatch):
    searches, fetched = [], []
    candidates = [providers.SearchResult('Discovered source', 'https://sources.example.org/paper',
                                        'UNVERIFIED_SNIPPET must never be quoted as evidence.', 'fixture-search')]
    bodies = {'https://sources.example.org/paper': b'<article><p>Tree canopy evidence records a measurable heat reduction.</p></article>'}

    class Search:
        name = 'fixture-search'

        def search(self, query, limit):
            searches.append((query, limit))
            return candidates[:limit]

    def fetch(url, **kwargs):
        fetched.append(url)
        body = bodies[url]
        if isinstance(body, Exception):
            raise body
        return FetchResult(body, url, 'text/html', 200, {'content-type': 'text/html'})

    monkeypatch.setattr(providers, 'get_search_provider', lambda name: Search())
    monkeypatch.setattr(processing, 'safe_fetch', fetch)
    monkeypatch.setenv('SEARCH_PRICE_PER_REQUEST_USD', '0')
    return searches, fetched, candidates, bodies


def start_discovery(client, project_id, **options):
    requested = checked(client.post('/v1/research-runs', json={
        'project_id': project_id, 'question': 'tree canopy evidence', 'provider': 'local',
        'web_discovery': True, 'search_provider': 'brave',
        'max_searches': 2, 'max_documents': 2, 'max_tool_calls': 8,
        'max_duration_seconds': 180, **options}), 202)
    completed = checked(client.get(f'/v1/research-runs/{requested["id"]}'))
    return requested, completed


def test_web_research_ingests_saved_body_before_creating_cited_draft(client, runtime, external_sources):
    searches, fetched, _, bodies = external_sources
    proj = project(client)
    _, run = start_discovery(client, proj['id'])
    assert run['status'] == 'succeeded', run['error']
    assert len(searches) == len(fetched) == 1
    lead = run['result_json']['discovery']['leads'][0]
    assert lead['status'] == 'saved' and lead['evidence_eligible'] is True
    proposal = checked(client.get(f'/v1/proposals/{run["result_json"]["proposal_id"]}'))
    assert proposal['status'] == 'pending'
    assert 'UNVERIFIED_SNIPPET' not in proposal['content']
    assert 'Tree canopy evidence records a measurable heat reduction.' in proposal['content']
    with runtime.session_factory() as session:
        capture = session.get(m.Capture, lead['capture_id'])
        assert capture.content_hash == digest(bodies[fetched[0]])
        anchors = list(session.scalars(select(m.Evidence)))
        assert anchors and all(anchor.capture_id == capture.id for anchor in anchors)
        assert all('UNVERIFIED_SNIPPET' not in anchor.quote for anchor in anchors)
        ingest_dispatch = session.scalar(select(m.OperationDispatch).where(
            m.OperationDispatch.operation_id == lead['operation_id']))
        assert ingest_dispatch.status == 'delivered'
    assert run['usage_json']['external_searches'] == 1
    assert run['usage_json']['searches'] == 2
    assert run['usage_json']['ingestion_attempts'] == 1
    assert run['usage_json']['tool_calls'] <= 8
    assert run['usage_json']['documents'] <= 2


def test_failed_candidate_remains_unverified_while_saved_candidate_supports_research(client, external_sources):
    _, fetched, candidates, bodies = external_sources
    candidates.append(providers.SearchResult('Unavailable lead', 'https://sources.example.org/missing',
                                            'Unsaved evidence claim', 'fixture-search'))
    bodies['https://sources.example.org/missing'] = FetchError('Unavailable source')
    proj = project(client)
    _, run = start_discovery(client, proj['id'])
    assert run['status'] == 'succeeded', run['error']
    assert len(fetched) == 2
    leads = run['result_json']['discovery']['leads']
    assert [(lead['status'], lead['evidence_eligible']) for lead in leads] == [('saved', True), ('unverified', False)]
    failed_operation = checked(client.get(f'/v1/operations/{leads[1]["operation_id"]}'))
    assert failed_operation['status'] == 'failed'
    proposal = checked(client.get(f'/v1/proposals/{run["result_json"]["proposal_id"]}'))
    assert 'Unsaved evidence claim' not in proposal['content']
    assert all(item['capture_id'] == leads[0]['capture_id'] for item in proposal['evidence'])


def test_snippet_only_match_cannot_create_evidence_and_retry_does_not_repeat_discovery(client, runtime, external_sources):
    searches, fetched, candidates, bodies = external_sources
    candidates[0] = providers.SearchResult('Quantum batteries', candidates[0].url,
                                         'quantum batteries important claim', 'fixture-search')
    bodies[candidates[0].url] = b'<article><p>Archive source exclusively discusses urban trees.</p></article>'
    proj = project(client)
    requested, run = start_discovery(client, proj['id'], question='quantum batteries')
    assert run['status'] == 'failed'
    assert 'No local evidence' in run['error']
    lead = run['result_json']['discovery']['leads'][0]
    assert lead['status'] == 'saved' and lead['evidence_eligible'] is True
    assert lead['research_transaction_rolled_back'] is True
    assert {'capture_id', 'document_id', 'operation_id'} <= lead.keys()
    with runtime.session_factory() as session:
        # Successful acquisition is durable even when nothing answers the research question.
        for model in (m.Capture, m.Representation):
            assert session.scalar(select(func.count()).select_from(model)) == 1
        for model in (m.Evidence, m.Proposal):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    checked(client.post(f'/v1/operations/{requested["operation_id"]}/retry'), 202)
    blocked = checked(client.get(f'/v1/operations/{requested["operation_id"]}'))
    assert blocked['status'] == 'failed' and 'earlier external call' in blocked['error']
    assert len(searches) == len(fetched) == 1


@pytest.mark.parametrize('price,budget', [(None, 0), ('nan', 1), ('-1', 1), ('0.50', 0.10)])
def test_unpriced_invalid_or_over_budget_discovery_never_calls_search(client, external_sources, monkeypatch, price, budget):
    searches, fetched, _, _ = external_sources
    if price is None:
        monkeypatch.delenv('SEARCH_PRICE_PER_REQUEST_USD')
    else:
        monkeypatch.setenv('SEARCH_PRICE_PER_REQUEST_USD', price)
    proj = project(client)
    _, run = start_discovery(client, proj['id'], max_cost_usd=budget)
    assert run['status'] == 'failed'
    assert searches == [] and fetched == []
    assert checked(client.get(f'/v1/projects/{proj["id"]}'))['proposals'] == []


def test_discovery_document_budget_limits_external_fetches(client, external_sources):
    searches, fetched, candidates, bodies = external_sources
    second_url = 'https://sources.example.org/second'
    candidates.append(providers.SearchResult('Second lead', second_url, 'Second snippet', 'fixture-search'))
    bodies[second_url] = b'<p>Additional tree canopy evidence.</p>'
    proj = project(client)
    _, run = start_discovery(client, proj['id'], max_documents=1, max_tool_calls=5)
    assert run['status'] == 'succeeded', run['error']
    assert searches[0][1] == 1 and len(fetched) == 1
    assert run['usage_json']['documents'] == 1
    assert run['usage_json']['tool_calls'] <= 5
