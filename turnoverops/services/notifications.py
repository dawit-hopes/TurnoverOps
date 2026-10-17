from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.context import get_correlation_id
from turnoverops.integrations import telegram_api
from turnoverops.integrations.telegram_api import TelegramError
from turnoverops.models import Cleaner, Notification
from turnoverops.services.audit import audit


def notify_host(db: Session, host_id: str, message_type: str, body: str) -> Notification:
    note = Notification(
        host_id=host_id,
        recipient_type="host",
        recipient_id=host_id,
        channel="dashboard",
        message_type=message_type,
        body=body[:2000],
        status="sent",
        provider_response={"delivered": "dashboard"},
        correlation_id=get_correlation_id(),
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


def notify_cleaner(
    db: Session,
    cleaner: Cleaner,
    message_type: str,
    body: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> Notification:
    moment = now or datetime.now(UTC)
    note = Notification(
        host_id=cleaner.host_id,
        recipient_type="cleaner",
        recipient_id=cleaner.id,
        channel="telegram",
        message_type=message_type,
        body=body[:2000],
        status="pending",
        correlation_id=get_correlation_id(),
    )
    db.add(note)
    db.flush()
    _deliver(db, note, cleaner, settings, moment)
    return note


def _deliver(db: Session, note: Notification, cleaner: Cleaner, settings: Settings, now: datetime) -> None:
    can_send = bool(settings.telegram_bot_token and cleaner.telegram_chat_id)
    if not can_send:
        if settings.dev_mode:
            note.channel = "simulated"
            note.status = "simulated"
            note.provider_response = {"simulated": True}
            audit(
                db,
                host_id=note.host_id,
                event_type="notification_simulated",
                entity_type="notification",
                entity_id=note.id,
                reason="Telegram is not connected, so the message was simulated",
                details={"message_type": note.message_type, "simulated": True},
            )
            return
        note.status = "failed"
        note.retry_count = settings.max_notification_retries
        note.provider_response = {"error": "cleaner_not_connected"}
        audit(
            db,
            host_id=note.host_id,
            event_type="notification_failed",
            entity_type="notification",
            entity_id=note.id,
            reason="Cleaner has no Telegram connection",
            details={"message_type": note.message_type},
        )
        return
    try:
        payload = telegram_api.send_message(settings.telegram_bot_token, cleaner.telegram_chat_id, note.body)
    except TelegramError as exc:
        note.status = "failed"
        note.retry_count += 1
        note.next_retry_at = now + timedelta(seconds=settings.notification_retry_seconds * note.retry_count)
        if note.retry_count >= settings.max_notification_retries:
            note.next_retry_at = None
        note.provider_response = {"error": str(exc)[:300]}
        audit(
            db,
            host_id=note.host_id,
            event_type="notification_failed",
            entity_type="notification",
            entity_id=note.id,
            reason=str(exc)[:500],
            details={"message_type": note.message_type, "retry_count": note.retry_count},
        )
        return
    message_id = (payload.get("result") or {}).get("message_id")
    note.status = "sent"
    note.provider_response = {"ok": True, "message_id": message_id}
    audit(
        db,
        host_id=note.host_id,
        event_type="notification_sent",
        entity_type="notification",
        entity_id=note.id,
        reason="Telegram accepted the message",
        details={"message_type": note.message_type, "message_id": message_id},
    )


def retry_notifications(db: Session, settings: Settings, *, now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    rows = db.scalars(
        select(Notification).where(
            Notification.channel == "telegram",
            Notification.status == "failed",
            Notification.retry_count < settings.max_notification_retries,
            Notification.next_retry_at.is_not(None),
            Notification.next_retry_at <= moment,
        )
    ).all()
    delivered = 0
    for note in rows:
        cleaner = db.get(Cleaner, note.recipient_id)
        if cleaner is None:
            note.next_retry_at = None
            continue
        before = note.retry_count
        _deliver(db, note, cleaner, settings, moment)
        if note.status == "sent":
            delivered += 1
        elif note.retry_count == before:
            note.retry_count += 1
    return delivered
