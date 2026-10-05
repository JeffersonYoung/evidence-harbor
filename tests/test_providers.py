"""Provider contract/security tests; external HTTP is intentionally mocked, never billed."""
import json

import pytest

from backend import providers
from backend.security import FetchResult

EVIDENCE = [{'id': 'saved-evidence-1', 'quote': 'Policy evidence supports a measurable reduction.',
             'classification': 'public', 'title': 'Private source title', 'source_uri': 'https://example.org/private?token=do-not-share'}]


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('ENABLE_EXTERNAL_PROVIDERS', 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-credential-no-real-account')
    monkeypatch.setenv('OPENAI_MODEL', 'configured-test-model')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://provider.example.org/v1')
    monkeypatch.setenv('EXTERNAL_EVIDENCE_POLICY', 'allow-reviewed')
    monkeypatch.delenv('ALLOW_SENSITIVE_EXTERNAL_DATA', raising=False)
    monkeypatch.delenv('EXTERNAL_ALLOWED_CLASSIFICATIONS', raising=False)
    monkeypatch.delenv('MODEL_PRICING_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_TOKEN_LIMIT_FIELD', raising=False)
    return providers.OpenAICompatibleProvider()


def model_response(output=None):
    if output is None:
        output = {'title': 'Evidence review', 'claims': [
            {'text': 'A measurable reduction was recorded.', 'evidence_ids': ['saved-evidence-1']}]}
    envelope = {'model': 'configured-test-model', 'choices': [{'message': {'content': json.dumps(output)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
    return FetchResult(json.dumps(envelope).encode(), 'https://provider.example.org/v1/chat/completions',
                       'application/json', 200, {})


def test_local_provider_uses_exact_saved_quotes_and_zero_paid_usage(monkeypatch):
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Local provider attempted HTTP'))
    result = providers.get_provider('local').research('What does the evidence say?', EVIDENCE)
    assert result['generated'] is False
    assert result['claims'] == [{'text': EVIDENCE[0]['quote'], 'evidence_ids': [EVIDENCE[0]['id']]}]
    assert result['usage']['billed_cost_usd'] == 0
    assert EVIDENCE[0]['quote'] in result['content']


def test_external_provider_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv('ENABLE_EXTERNAL_PROVIDERS', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'configured-but-not-approved')
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Disabled provider attempted HTTP'))
    with pytest.raises(providers.ProviderNotConfigured, match='disabled'):
        providers.get_provider('openai')


def test_external_data_sharing_gate_denies_before_network(provider, monkeypatch):
    monkeypatch.delenv('EXTERNAL_EVIDENCE_POLICY', raising=False)
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Unreviewed evidence was transmitted'))
    with pytest.raises(providers.ProviderNotConfigured, match='EXTERNAL_EVIDENCE_POLICY'):
        provider.research('Question', EVIDENCE)


def test_declared_sensitive_evidence_is_not_transmitted(provider, monkeypatch):
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Sensitive evidence was transmitted'))
    with pytest.raises(providers.ProviderNotConfigured, match='sensitive'):
        provider.research('Question', [{**EVIDENCE[0], 'sensitive': True}])


def test_provider_payload_minimizes_data_and_carries_explicit_limits(provider, monkeypatch):
    requests = []

    def fetch(url, **kwargs):
        requests.append((url, kwargs))
        return model_response()

    monkeypatch.setattr(providers, 'safe_fetch', fetch)
    result = provider.research('What is supported?', EVIDENCE, reasoning_effort='low',
                               max_output_tokens=512, timeout=30)
    assert len(requests) == 1
    url, request = requests[0]
    assert url == 'https://provider.example.org/v1/chat/completions'
    assert request['method'] == 'POST' and request['max_redirects'] == 0
    assert request['timeout'] == 30
    body = json.loads(request['body'])
    assert body['model'] == 'configured-test-model'
    assert body['reasoning_effort'] == 'low' and body['max_completion_tokens'] == 512
    transmitted = json.loads(body['messages'][1]['content'])['evidence']
    assert transmitted == [{'id': EVIDENCE[0]['id'], 'quote': EVIDENCE[0]['quote']}]
    assert b'do-not-share' not in request['body'] and b'Private source title' not in request['body']
    assert result['claims'][0]['evidence_ids'] == [EVIDENCE[0]['id']]
    assert result['usage']['input_tokens'] == 100
    assert result['usage']['billed_cost_usd'] is None
    assert result['usage']['estimated_cost_usd'] is None


def test_contact_redaction_happens_before_external_http(provider, monkeypatch):
    monkeypatch.setenv('EXTERNAL_EVIDENCE_POLICY', 'redact-contact-data')
    bodies = []

    def fetch(url, **kwargs):
        bodies.append(kwargs['body'])
        return model_response()

    monkeypatch.setattr(providers, 'safe_fetch', fetch)
    original = 'Contact researcher@example.org or +1 415 555 0100 for evidence.'
    provider.research('Question', [{**EVIDENCE[0], 'quote': original}])
    assert b'researcher@example.org' not in bodies[0]
    assert b'415 555 0100' not in bodies[0]
    assert b'EMAIL_REDACTED' in bodies[0] and b'PHONE_REDACTED' in bodies[0]
    assert original == 'Contact researcher@example.org or +1 415 555 0100 for evidence.'


@pytest.mark.parametrize('output', [
    {'title': 'Unknown cite', 'claims': [{'text': 'Claim', 'evidence_ids': ['fabricated-id']}]},
    {'title': 'No citations', 'claims': [{'text': 'Claim', 'evidence_ids': []}]},
    {'title': 'No claims', 'claims': []},
    {'title': 42, 'claims': [{'text': 'Claim', 'evidence_ids': ['saved-evidence-1']}]},
    [],
])
def test_invalid_model_drafts_are_rejected_instead_of_staged(provider, monkeypatch, output):
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: model_response(output))
    with pytest.raises(providers.ProviderError):
        provider.research('Question', EVIDENCE)


def test_monetary_budget_requires_exact_model_pricing(provider, monkeypatch):
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Unpriced request attempted HTTP'))
    with pytest.raises(providers.ProviderNotConfigured, match='prices'):
        provider.research('Question', EVIDENCE, max_cost_usd=1.0)


def test_insufficient_cost_reservation_blocks_http(provider, monkeypatch):
    monkeypatch.setenv('MODEL_PRICING_MODEL', 'configured-test-model')
    monkeypatch.setenv('MODEL_INPUT_PRICE_PER_MILLION', '1')
    monkeypatch.setenv('MODEL_OUTPUT_PRICE_PER_MILLION', '2')
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Over-budget request attempted HTTP'))
    with pytest.raises(providers.ProviderError, match='budget'):
        provider.research('Question', EVIDENCE, max_cost_usd=0.0000001)


def test_priced_usage_is_an_estimate_and_not_claimed_as_a_bill(provider, monkeypatch):
    monkeypatch.setenv('MODEL_PRICING_MODEL', 'configured-test-model')
    monkeypatch.setenv('MODEL_INPUT_PRICE_PER_MILLION', '1')
    monkeypatch.setenv('MODEL_OUTPUT_PRICE_PER_MILLION', '2')
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: model_response())
    result = provider.research('Question', EVIDENCE, max_cost_usd=1.0)
    assert result['usage']['estimated_cost_usd'] == pytest.approx(0.00014)
    assert 0 < result['usage']['reserved_cost_estimate_usd'] < 1.0
    assert result['usage']['billed_cost_usd'] is None
    assert result['usage']['cost_basis'] == 'operator_configured_prices'


@pytest.mark.parametrize('classification', ['internal', 'sensitive'])
def test_unapproved_data_classification_is_not_sent(provider, monkeypatch, classification):
    monkeypatch.setattr(providers, 'safe_fetch', lambda *a, **kw: pytest.fail('Unapproved classification transmitted'))
    with pytest.raises(providers.ProviderNotConfigured, match='classification'):
        provider.research('Question', [{**EVIDENCE[0], 'classification': classification}])
