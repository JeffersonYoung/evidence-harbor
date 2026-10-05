"""A database activity attempt is not authoritative Temporal completion."""

from types import SimpleNamespace

import pytest

from backend import execution_status
from backend import models as m
from tests.test_api_workflow import checked, project


@pytest.fixture
def durable_operation(client, runtime):
    proj = project(client)
    runtime.settings.inline_worker = False
    op = checked(client.post('/v1/ingestions', json={
        'project_id': proj['id'], 'type': 'text', 'text': 'Durable workflow status fixture.'}), 202)
    return op


def describe_as(monkeypatch, status):
    async def describe():
        return SimpleNamespace(status=SimpleNamespace(name=status))

    async def connect(*args, **kwargs):
        return SimpleNamespace(get_workflow_handle=lambda identifier: SimpleNamespace(describe=describe))

    monkeypatch.setattr(execution_status.Client, 'connect', connect)


def test_live_running_workflow_is_not_terminal_even_if_last_activity_failed(client, runtime, durable_operation, monkeypatch):
    op = durable_operation
    with runtime.session_factory() as session:
        item = session.get(m.Operation, op['id'])
        item.status, item.completed_at = 'failed', m.utcnow()
        session.commit()
    describe_as(monkeypatch, 'RUNNING')
    state = checked(client.get(f'/v1/operations/{op["id"]}/execution'))
    assert state['operation_status'] == 'failed'
    assert state['workflow_status'] == 'RUNNING' and state['terminal'] is False
    assert client.post(f'/v1/operations/{op["id"]}/retry').status_code == 409
    assert checked(client.get(f'/v1/operations/{op["id"]}'))['status_scope'] == 'activity_attempt'


def test_terminal_failure_allows_recovery_from_stale_running_database_status(client, runtime, durable_operation, monkeypatch):
    op = durable_operation
    with runtime.session_factory() as session:
        item = session.get(m.Operation, op['id'])
        item.status, item.completed_at = 'running', m.utcnow()
        session.commit()
    describe_as(monkeypatch, 'TIMED_OUT')
    requested = checked(client.post(f'/v1/operations/{op["id"]}/retry'), 202)
    assert requested['status'] == 'pending' and requested['completed_at'] is None
    state = checked(client.get(f'/v1/operations/{op["id"]}/execution'))
    assert state['generation'] == 1 and state['workflow_id'].endswith('-g1')


def test_unavailable_workflow_state_never_grants_retry(client, durable_operation, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError('fixture service unavailable')

    monkeypatch.setattr(execution_status.Client, 'connect', unavailable)
    op = durable_operation
    state = checked(client.get(f'/v1/operations/{op["id"]}/execution'))
    assert state['available'] is False and state['terminal'] is None
    assert client.post(f'/v1/operations/{op["id"]}/retry').status_code == 503


def test_execution_status_does_not_query_foreign_workspace(client, durable_operation, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('Unauthorized workflow lookup')

    monkeypatch.setattr(execution_status.Client, 'connect', forbidden)
    op = durable_operation
    assert client.get(f'/v1/operations/{op["id"]}/execution',
                      headers={'Authorization': 'Bearer owner-b'}).status_code == 404
