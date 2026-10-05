"""Real password/session endpoints: salted hashes, bounded login and revocation."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from backend.auth import UserAccount, UserSession, hash_password, verify_password
from backend.models import utcnow
from tests.helpers import digest
from tests.test_api_workflow import checked

PASSWORD = 'Long fixture password 123!'


def account(runtime, username='test-researcher', enabled=True, role='researcher'):
    with runtime.session_factory() as session:
        user = UserAccount(username=username, workspace_id='workspace-a', role=role,
                           password_hash=hash_password(PASSWORD), enabled=enabled)
        session.add(user)
        session.commit()
        return user.id


def test_password_hashes_are_salted_and_never_plaintext():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert first.startswith('scrypt$')
    assert verify_password(PASSWORD, first)
    assert not verify_password('wrong-password', first)
    assert not verify_password(PASSWORD, 'invalid-hash')
    for password in ('short', 'a' * 1025):
        with pytest.raises(ValueError):
            hash_password(password)


def test_login_session_is_hashed_scoped_and_revocable(client, runtime):
    account(runtime)
    login = checked(client.post('/v1/auth/login', json={'username': 'test-researcher', 'password': PASSWORD}))
    token = login['access_token']
    headers = {'Authorization': f'Bearer {token}'}
    identity = checked(client.get('/v1/auth/me', headers=headers))
    assert identity == {'username': 'test-researcher', 'role': 'researcher', 'workspace_id': 'workspace-a'}
    assert checked(client.get('/v1/me', headers=headers))['role'] == 'researcher'
    with runtime.session_factory() as session:
        saved = session.scalar(select(UserSession))
        assert saved.token_hash == digest(token)
        assert saved.token_hash != token
        assert len(saved.token_hash) == 64
    assert checked(client.post('/v1/auth/logout', headers=headers))['status'] == 'signed_out'
    assert client.get('/v1/auth/me', headers=headers).status_code == 401
    assert client.get('/v1/projects', headers=headers).status_code == 401


def test_disabled_or_expired_account_cannot_reuse_session(client, runtime):
    user_id = account(runtime)
    token = checked(client.post('/v1/auth/login', json={'username': 'test-researcher', 'password': PASSWORD}))['access_token']
    headers = {'Authorization': f'Bearer {token}'}
    with runtime.session_factory() as session:
        saved = session.scalar(select(UserSession))
        saved.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert client.get('/v1/projects', headers=headers).status_code == 401
    with runtime.session_factory() as session:
        session.get(UserAccount, user_id).enabled = False
        session.commit()
    response = client.post('/v1/auth/login', json={'username': 'test-researcher', 'password': PASSWORD})
    assert response.status_code == 401
    assert response.json()['detail'] == 'Invalid username or password'


def test_invalid_login_does_not_enumerate_accounts_or_create_sessions(client, runtime):
    account(runtime)
    responses = [client.post('/v1/auth/login', json={'username': username, 'password': 'Wrong password 123'})
                 for username in ('test-researcher', 'nonexistent-account')]
    assert all(response.status_code == 401 for response in responses)
    assert responses[0].json() == responses[1].json()
    with runtime.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UserSession)) == 0


def test_repeated_failed_login_is_rate_limited(client):
    for _ in range(8):
        assert client.post('/v1/auth/login', json={
            'username': 'unknown-account', 'password': 'wrong-password'}).status_code == 401
    limited = client.post('/v1/auth/login', json={'username': 'unknown-account', 'password': 'wrong-password'})
    assert limited.status_code == 429
    assert limited.headers['retry-after'] == '900'
