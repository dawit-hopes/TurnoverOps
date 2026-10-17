from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.context import get_correlation_id
from turnoverops.models import Notification
from turnoverops.services.audit import audit


def notify_host(
    db: Session,
    host_id: str,
    message_type: str,
    body: str,
    *,
    task_id: str | None = None,
    dedupe_key: str | None = None,
) -> Notification:
    if dedupe_key:
        existing = db.scalar(
            select(Notification).where(Notification.host_id == host_id, Notification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing
    note = Notification(
        host_id=host_id,
        recipient_type="host",
        recipient_id=host_id,
        task_id=task_id,
        channel="dashboard",
        message_type=message_type,
        body=body[:2000],
        status="sent",
        provider_response={"delivered": "dashboard"},
        correlation_id=get_correlation_id(),
        dedupe_key=dedupe_key,
    )
    db.add(note)
    db.flush()
    audit(
        db,
        host_id=host_id,
        event_type="notification_sent",
        entity_type="notification",
        entity_id=note.id,
        reason=body[:500],
        details={"channel": "dashboard", "message_type": message_type},
    )
    return note

