from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.domain import BLOCKING_TASK_STATUSES, LIVE_ASSIGNMENT_STATES
from turnoverops.models import Assignment, Cleaner, CleanerProperty, CleaningTask
from turnoverops.scheduling.engine import BusyInterval, CleanerInput, Decision, JobWindow, decide
from turnoverops.services.cleaner_calendar import calendar_busy_intervals


def cleaner_inputs(db: Session, host_id: str) -> list[CleanerInput]:
    cleaners = db.scalars(select(Cleaner).where(Cleaner.host_id == host_id)).all()
    links = db.scalars(select(CleanerProperty).where(CleanerProperty.host_id == host_id)).all()
    grouped: dict[str, list[CleanerProperty]] = defaultdict(list)
    for link in links:
        grouped[link.cleaner_id].append(link)
    inputs: list[CleanerInput] = []
    for cleaner in cleaners:
        rows = grouped.get(cleaner.id, [])
        restricting = [row.property_id for row in rows if row.restricts]
        eligible = frozenset(restricting) if restricting else None
        preferred = frozenset(row.property_id for row in rows if row.is_preferred)
        unavailable = frozenset(date.fromisoformat(item) for item in (cleaner.unavailable_dates or []) if item)
        inputs.append(
            CleanerInput(
                id=cleaner.id,
                name=cleaner.name,
                active=cleaner.active,
                timezone=cleaner.timezone,
                working_days=frozenset(cleaner.working_days or []),
                work_start=cleaner.work_start,
                work_end=cleaner.work_end,
                max_jobs_per_day=cleaner.max_jobs_per_day,
                eligible_property_ids=eligible,
                preferred_property_ids=preferred,
                unavailable_dates=unavailable,
                calendar_status=cleaner.calendar_status or "not_connected",
            )
        )
    return inputs


def busy_intervals(db: Session, host_id: str, *, exclude_task_id: str | None = None) -> list[BusyInterval]:
    stmt = (
        select(Assignment, CleaningTask)
        .join(CleaningTask, CleaningTask.id == Assignment.task_id)
        .where(
            CleaningTask.host_id == host_id,
            CleaningTask.status.in_(tuple(BLOCKING_TASK_STATUSES)),
            Assignment.state.in_(tuple(LIVE_ASSIGNMENT_STATES)),
            Assignment.id == CleaningTask.current_assignment_id,
        )
    )
    if exclude_task_id:
        stmt = stmt.where(CleaningTask.id != exclude_task_id)
    intervals: list[BusyInterval] = []
    for assignment, task in db.execute(stmt).all():
        intervals.append(
            BusyInterval(
                cleaner_id=assignment.cleaner_id,
                task_id=task.id,
                start=assignment.scheduled_start,
                end=assignment.scheduled_end,
            )
        )
    return intervals


def evaluate_window(
    db: Session,
    host_id: str,
    window: JobWindow,
    *,
    exclude_task_id: str | None = None,
    settings: Settings | None = None,
) -> Decision:
    external = calendar_busy_intervals(db, host_id, settings, window.window_start, window.window_end)
    busy = busy_intervals(db, host_id, exclude_task_id=exclude_task_id) + external
    return decide(cleaner_inputs(db, host_id), window, busy)


def task_window(task: CleaningTask, timezone_name: str) -> JobWindow:
    return JobWindow(
        property_id=task.property_id,
        property_timezone=timezone_name,
        window_start=task.window_start,
        window_end=task.window_end,
        duration_minutes=task.duration_minutes,
    )
