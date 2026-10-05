"""At-least-once webhook delivery with stable event IDs and bounded retries.

Receivers must deduplicate X-EvidenceHarbor-Event-ID. A network acceptance followed
by a lost DB acknowledgement cannot generally provide exactly-once delivery.
"""

import json
from datetime import timedelta

from sqlalchemy import or_, select

from .models import OutboxEvent, Subscription, utcnow
from .security import FetchError, UnsafeURLError, safe_fetch


def deliver_outbox_once(session_factory, max_attempts: int = 8) -> dict:
    delivered = failed = 0
    with session_factory() as session:
        candidates = select(OutboxEvent).where(
            OutboxEvent.status == "pending", OutboxEvent.attempts < max_attempts
        )
        if hasattr(OutboxEvent, "next_attempt_at"):
            candidates = candidates.where(
                or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= utcnow())
            )
        events = list(
            session.scalars(
                candidates.order_by(OutboxEvent.created_at).limit(20).with_for_update(skip_locked=True)
            )
        )
        for event in events:
            subscription = session.get(Subscription, event.subscription_id)
            if subscription is None or not subscription.active:
                event.status = "cancelled"
                session.commit()
                continue
            if not subscription.target_url:
                continue  # Pull-only subscriptions are intentionally not sent anywhere.
            event.attempts += 1
            payload = {
                "id": event.id,
                "type": event.event_type,
                "project_id": event.project_id,
                "created_at": event.created_at.isoformat(),
                "data": event.payload,
            }
            try:
                safe_fetch(
                    subscription.target_url,
                    method="POST",
                    max_redirects=0,
                    max_bytes=64 * 1024,
                    timeout=15,
                    body=json.dumps(payload, ensure_ascii=False).encode(),
                    headers={"Content-Type": "application/json", "X-EvidenceHarbor-Event-ID": event.id},
                )
            except (FetchError, UnsafeURLError, ValueError) as exc:
                # Error classification only: URLs may contain private query identifiers.
                event.last_error = type(exc).__name__
                event.status = "failed" if event.attempts >= max_attempts else "pending"
                if hasattr(event, "next_attempt_at"):
                    event.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 2**event.attempts * 5))
                failed += 1
            else:
                event.status = "delivered"
                event.delivered_at = utcnow()
                event.last_error = None
                delivered += 1
            session.commit()
    return {"delivered": delivered, "failed_attempts": failed}
