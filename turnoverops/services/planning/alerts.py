"""Host dashboard alerts and optional cleaner dispatch for a validated plan."""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.domain import LIVE_ASSIGNMENT_STATES
from turnoverops.integrations.email_api import EmailError, EmailNotConfigured, send_email
from turnoverops.models import Assignment, CleaningTask, Host, Notification, NotificationDelivery, Property
from turnoverops.services.audit import audit
from turnoverops.services.host_copy import spoken_time
from turnoverops.services.notifications import notify_host
from turnoverops.services.planning.policy import PLANNING_SOURCES, TaskView


def issue_key(task: TaskView, cleaner_ids: list[str], reason: str) -> str:
    digest = hashlib.sha256(
        f"{task.window_end.isoformat()}|{','.join(sorted(cleaner_ids))}|{reason}".encode()
    ).hexdigest()[:20]
    return f"{task.id}:unassigned:{digest}"


def manager_alert_state(db: Session, task_id: str) -> tuple[bool, str | None]:
    note = db.scalar(
        select(Notification).where(
            Notification.task_id == task_id,
            Notification.recipient_type == "host",
            Notification.message_type == "task_unassigned",
            Notification.status == "sent",
        )
    )
    delivery = db.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.task_id == task_id, NotificationDelivery.channel == "email")
        .order_by(NotificationDelivery.created_at.desc())
    )
    return note is not None, delivery.status if delivery is not None else None


def alert_unassigned(
    db: Session,
    host: Host,
    task: TaskView | CleaningTask,
    prop: Property,
    settings: Settings,
    *,
    reason: str,
    cleaner_ids: list[str],
    email_client=None,
) -> NotificationDelivery | None:
    key = issue_key(task, cleaner_ids, reason)
    existing = db.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.host_id == host.id,
            NotificationDelivery.dedupe_key == key,
        )
    )
    if existing is not None:
        return None
    deadline = spoken_time(task.window_end, prop.timezone)
    body = (
        f"Cleaning assignment requires attention. No available cleaner can complete the cleaning at {prop.name} "
        f"before {deadline}. The task remains unassigned. Review cleaner availability, adjust the schedule if possible, "
        "or arrange additional coverage."
    )
    note = notify_host(db, host.id, "task_unassigned", body, task_id=task.id, dedupe_key=key)
    delivery = NotificationDelivery(
        host_id=host.id,
        notification_id=note.id,
        task_id=task.id,
        channel="email",
        status="pending",
        dedupe_key=key,
        recipient=host.email,
        subject="Cleaning assignment requires attention",
        body=body,
    )
    db.add(delivery)
    db.flush()
    try:
        if callable(email_client):
            payload = email_client(to=host.email, subject=delivery.subject, body=body)
        else:
            payload = send_email(
                settings,
                to=host.email,
                subject=delivery.subject or "Cleaning assignment requires attention",
                body=body,
                client=email_client,
            )
    except EmailNotConfigured:
        delivery.status = "not_configured"
        delivery.error = "Email provider is not configured"
        delivery.provider_response = {"status": "not_configured"}
    except EmailError as exc:
        delivery.status = "failed"
        delivery.error = str(exc)[:500]
        delivery.provider_response = {"status_code": exc.status_code}
    else:
        delivery.status = "delivered"
        delivery.provider_response = payload if isinstance(payload, dict) else {"ok": True}
    delivery.updated_at = datetime.now(UTC)
    audit(
        db,
        host_id=host.id,
        event_type="notification_sent" if delivery.status == "delivered" else "notification_failed",
        entity_type="notification_delivery",
        entity_id=delivery.id,
        reason=body[:500],
        details={"channel": "email", "status": delivery.status, "task_id": task.id},
    )
    return delivery


def release_assignment(
    db: Session,
    assignment: Assignment,
    settings: Settings,
    now: datetime,
    *,
    notify: bool,
) -> None:
    if assignment.dispatched_at is not None:
        return
    assignment.dispatched_at = now
    if not notify:
        return
    from turnoverops.services.cleaner_mail import send_assignment_email

    send_assignment_email(db, assignment, settings, now)
    task = db.get(CleaningTask, assignment.task_id)
    task_prop = db.get(Property, task.property_id) if task is not None else None
    if task is not None and task_prop is not None:
        from turnoverops.services.calendar_sync import publish_task

        publish_task(db, task, task_prop, settings)


def dispatch_pending(db: Session, host_id: str, settings: Settings, now: datetime, *, notify: bool) -> int:
    rows = db.scalars(
        select(Assignment).where(
            Assignment.host_id == host_id,
            Assignment.dispatched_at.is_(None),
            Assignment.source.in_(tuple(PLANNING_SOURCES)),
            Assignment.state.in_(tuple(LIVE_ASSIGNMENT_STATES)),
        )
    ).all()
    released = 0
    for assignment in rows:
        task = db.get(CleaningTask, assignment.task_id)
        if task is None or task.current_assignment_id != assignment.id:
            continue
        release_assignment(db, assignment, settings, now, notify=notify)
        released += 1
    return released
