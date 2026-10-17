import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.integrations import google_calendar_api
from turnoverops.integrations.google_calendar_api import CalendarError
from turnoverops.models import Assignment, CalendarEventMapping, CleaningTask, IntegrationState, Property
from turnoverops.services.audit import audit

PROVIDER = "google_calendar"
TOKEN_KEY = "oauth"


def calendar_configured(settings: Settings) -> bool:
    return bool(settings.google_calendar_client_id and settings.google_calendar_client_secret)


def load_oauth(db: Session, host_id: str) -> IntegrationState | None:
    return db.scalar(
        select(IntegrationState).where(
            IntegrationState.host_id == host_id,
            IntegrationState.provider == PROVIDER,
            IntegrationState.key == TOKEN_KEY,
        )
    )


def save_oauth(db: Session, host_id: str, value: dict) -> IntegrationState:
    row = load_oauth(db, host_id)
    if row is None:
        row = IntegrationState(host_id=host_id, provider=PROVIDER, key=TOKEN_KEY, value=value)
        db.add(row)
    else:
        row.value = value
    row.updated_at = row.updated_at
    from turnoverops.models.base import utcnow

    row.updated_at = utcnow()
    return row


def disconnect(db: Session, host_id: str) -> None:
    row = load_oauth(db, host_id)
    if row is not None:
        db.delete(row)


def _access_token(db: Session, host_id: str, settings: Settings) -> tuple[IntegrationState, str] | None:
    row = load_oauth(db, host_id)
    if row is None or not row.value or not row.value.get("access_token"):
        return None
    value = dict(row.value)
    expires_at = float(value.get("expires_at") or 0)
    if expires_at <= time.time() + 30:
        refresh_token = value.get("refresh_token")
        if not refresh_token:
            raise CalendarError("Google Calendar authorization expired")
        refreshed = google_calendar_api.refresh_access_token(settings, refresh_token)
        value["access_token"] = refreshed["access_token"]
        value["expires_at"] = time.time() + int(refreshed.get("expires_in") or 3600)
        if refreshed.get("refresh_token"):
            value["refresh_token"] = refreshed["refresh_token"]
        row.value = value
    return row, value["access_token"]


def _mapping(db: Session, task: CleaningTask) -> CalendarEventMapping:
    row = db.scalar(select(CalendarEventMapping).where(CalendarEventMapping.task_id == task.id))
    if row is None:
        row = CalendarEventMapping(host_id=task.host_id, task_id=task.id, calendar_id="primary")
        db.add(row)
        db.flush()
    return row


def publish_task(db: Session, task: CleaningTask, prop: Property, settings: Settings) -> None:
    if not calendar_configured(settings):
        return
    if load_oauth(db, task.host_id) is None:
        return
    mapping = _mapping(db, task)
    try:
        token = _access_token(db, task.host_id, settings)
        if token is None:
            return
        _row, access_token = token
        assignment = db.get(Assignment, task.current_assignment_id) if task.current_assignment_id else None
        if assignment is None:
            return
        event_id = google_calendar_api.upsert_event(
            access_token,
            mapping.calendar_id,
            mapping.google_event_id,
            {
                "summary": f"{prop.name} cleaning",
                "description": f"Task {task.id}. Status {task.status}.",
                "start": {"dateTime": assignment.scheduled_start.isoformat()},
                "end": {"dateTime": assignment.scheduled_end.isoformat()},
            },
        )
    except CalendarError as exc:
        mapping.last_error = str(exc)[:500]
        audit(
            db,
            host_id=task.host_id,
            event_type="calendar_sync_failed",
            entity_type="cleaning_task",
            entity_id=task.id,
            reason=str(exc)[:500],
        )
        return
    mapping.google_event_id = event_id
    mapping.last_error = None
    from turnoverops.models.base import utcnow

    mapping.updated_at = utcnow()
