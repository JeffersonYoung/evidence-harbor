"""Webhook replay keeps event identity stable, honors backoff and stops bounded failures."""
import json
from datetime import timedelta

from sqlalchemy import select

from backend import domain, outbox
from backend import models as m
from backend.security import FetchError, FetchResult
from tests.test_domain_integrity import make_project


def seed_event(runtime, *, target='https://93.184.216.34/events', active=True):
    project_id = make_project(runtime)
    with runtime.session_factory() as session:
        subscription = m.Subscription(workspace_id='workspace-a', project_id=project_id,
                                      target_url=target, active=active)
        session.add(subscription)
        session.flush()
        event = m.OutboxEvent(workspace_id='workspace-a', project_id=project_id,
                             subscription_id=subscription.id, event_type='capture.created',
                             payload={'capture_id': 'saved-capture-id'})
        session.add(event)
        session.commit()
        return event.id


def due_again(runtime, event_id):
    with runtime.session_factory() as session:
        event = session.get(m.OutboxEvent, event_id)
        event.next_attempt_at = m.utcnow() - timedelta(seconds=1)
        session.commit()


def test_webhook_retry_uses_same_event_id_and_preserves_payload(runtime, monkeypatch):
    event_id = seed_event(runtime)
    sent = []

    def fetch(url, **kwargs):
        sent.append((url, kwargs))
        if len(sent) == 1:
            raise FetchError('Private response detail must not be saved')
        return FetchResult(b'ok', url, 'text/plain', 200, {})

    monkeypatch.setattr(outbox, 'safe_fetch', fetch)
    assert outbox.deliver_outbox_once(runtime.session_factory) == {'delivered': 0, 'failed_attempts': 1}
    with runtime.session_factory() as session:
        event = session.get(m.OutboxEvent, event_id)
        assert event.status == 'pending' and event.attempts == 1
        assert event.last_error == 'FetchError'
        assert event.next_attempt_at is not None
    assert outbox.deliver_outbox_once(runtime.session_factory) == {'delivered': 0, 'failed_attempts': 0}
    assert len(sent) == 1
    due_again(runtime, event_id)
    assert outbox.deliver_outbox_once(runtime.session_factory) == {'delivered': 1, 'failed_attempts': 0}
    assert len(sent) == 2
    for url, request in sent:
        assert url == 'https://93.184.216.34/events'
        assert request['method'] == 'POST' and request['max_redirects'] == 0
        assert request['headers']['X-EvidenceHarbor-Event-ID'] == event_id
        payload = json.loads(request['body'])
        assert payload['id'] == event_id and payload['type'] == 'capture.created'
        assert payload['data'] == {'capture_id': 'saved-capture-id'}
    assert sent[0][1]['body'] == sent[1][1]['body']
    with runtime.session_factory() as session:
        event = session.get(m.OutboxEvent, event_id)
        assert event.status == 'delivered' and event.attempts == 2
        assert event.delivered_at is not None and event.last_error is None
    assert outbox.deliver_outbox_once(runtime.session_factory)['delivered'] == 0


def test_webhook_failure_exhaustion_is_terminal(runtime, monkeypatch):
    event_id = seed_event(runtime)
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise FetchError('Unavailable')

    monkeypatch.setattr(outbox, 'safe_fetch', fail)
    outbox.deliver_outbox_once(runtime.session_factory, max_attempts=2)
    due_again(runtime, event_id)
    outbox.deliver_outbox_once(runtime.session_factory, max_attempts=2)
    with runtime.session_factory() as session:
        event = session.get(m.OutboxEvent, event_id)
        assert event.status == 'failed' and event.attempts == 2
    outbox.deliver_outbox_once(runtime.session_factory, max_attempts=2)
    assert len(calls) == 2


def test_disabled_and_pull_only_subscriptions_do_not_transmit(runtime, monkeypatch):
    disabled_id = seed_event(runtime, active=False)
    pull_id = seed_event(runtime, target=None)
    monkeypatch.setattr(outbox, 'safe_fetch', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unexpected HTTP')))
    assert outbox.deliver_outbox_once(runtime.session_factory) == {'delivered': 0, 'failed_attempts': 0}
    with runtime.session_factory() as session:
        assert session.get(m.OutboxEvent, disabled_id).status == 'cancelled'
        pull = session.get(m.OutboxEvent, pull_id)
        assert pull.status == 'pending' and pull.attempts == 0


def test_outbox_subscription_filters_match_only_requested_events(runtime):
    project_id = make_project(runtime)
    with runtime.session_factory() as session:
        sub = m.Subscription(workspace_id='workspace-a', project_id=project_id,
                             event_types=['capture.created'], active=True)
        session.add(sub)
        session.flush()
        domain.emit_event(session, 'workspace-a', project_id, 'proposal.published', {'document_id': 'report'})
        domain.emit_event(session, 'workspace-a', project_id, 'capture.created', {'capture_id': 'capture'})
        session.commit()
        events = list(session.scalars(select(m.OutboxEvent)))
        assert len(events) == 1 and events[0].event_type == 'capture.created'
