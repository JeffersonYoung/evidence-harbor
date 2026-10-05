"""Authentication is opt-in in development and never bypassed by default."""
import pytest

from backend.config import Settings


def test_default_settings_do_not_enable_demo_auth(monkeypatch):
    monkeypatch.delenv('DEMO_MODE', raising=False)
    monkeypatch.delenv('API_TOKENS_JSON', raising=False)
    monkeypatch.delenv('API_TOKEN', raising=False)
    config = Settings()
    assert config.demo_mode is False
    assert config.api_tokens == {}


@pytest.mark.parametrize('tokens', [[], 'token', {'': 'workspace'}, {'token': ''}, {'token': 123}])
def test_token_mapping_rejects_invalid_identity_config(tokens):
    with pytest.raises(ValueError, match='bearer tokens'):
        Settings(api_tokens=tokens)
