from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turnoverops.domain import TaskStatus
from turnoverops.models import Cleaner, CleaningTask, Host, Property
from zoneinfo import ZoneInfo

from turnoverops.services.calendar_sync import calendar_configured, load_oauth
from turnoverops.core.config import Settings


def snapshot(db: Session, host: Host, now: datetime | None = None) -> dict:
    moment = now or datetime.now(UTC)
    properties = db.scalars(select(Property).where(Property.host_id == host.id)).all()
    zones = {prop.id: ZoneInfo(prop.timezone) for prop in properties}
    tasks = db.scalars(select(CleaningTask).where(CleaningTask.host_id == host.id)).all()
    today = []
    for task in tasks:
        zone = zones.get(task.property_id)
        if zone is None or task.status == TaskStatus.blocked:
            continue
        if task.window_start.astimezone(zone).date() == moment.astimezone(zone).date():
            today.append(task)
    upcoming = sorted(
        (task for task in tasks if task.window_start >= moment and task.status not in {TaskStatus.completed, TaskStatus.blocked}),
        key=lambda task: task.window_start,
    )[:20]
    attention = [
        task
        for task in tasks
        if task.host_resolved_at is None and task.status in {TaskStatus.at_risk, TaskStatus.blocked, TaskStatus.unassigned}
    ]
    return {
        "metrics": {
            "properties": sum(1 for prop in properties if prop.active),
            "cleaners": db.scalar(select(func.count()).select_from(Cleaner).where(Cleaner.host_id == host.id, Cleaner.active.is_(True))) or 0,
            "tasks_today": len(today),
            "awaiting_confirmation": sum(1 for task in tasks if task.status == TaskStatus.awaiting_confirmation),
            "at_risk": sum(1 for task in tasks if task.status == TaskStatus.at_risk),
            "blocked": sum(1 for task in tasks if task.status == TaskStatus.blocked),
            "unassigned": sum(1 for task in tasks if task.status == TaskStatus.unassigned),
        },
        "today": sorted(today, key=lambda task: task.window_start),
        "upcoming": upcoming,
        "needs_attention": attention,
    }


def integration_status(db: Session, host: Host, settings: Settings) -> dict:
    return {
        "google_login": {"configured": bool(settings.google_client_id)},
        "telegram": {"configured": bool(settings.telegram_bot_token)},
        "gemini": {"configured": bool(settings.gemini_api_key)},
        "google_calendar": {
            "configured": calendar_configured(settings),
            "connected": load_oauth(db, host.id) is not None,
        },
    }
