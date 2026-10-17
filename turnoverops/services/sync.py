from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.domain import FeedKind, SyncStatus, TaskStatus
from turnoverops.integrations import ical_feed
from turnoverops.integrations.demo_feed import parse_demo_feed
from turnoverops.integrations.ical_feed import parse_ical
from turnoverops.integrations.snapshots import FeedError, ReservationSnapshot
from turnoverops.models import CleaningTask, Property, Reservation
from turnoverops.scheduling.turns import Stay, build_turnovers
from turnoverops.services.audit import audit
from turnoverops.services.workflow import reconcile_open, reconcile_task


def apply_snapshots(db: Session, prop: Property, snapshots: list[ReservationSnapshot], now: datetime) -> dict[str, int]:
    existing = {
        row.external_uid: row
        for row in db.scalars(select(Reservation).where(Reservation.property_id == prop.id)).all()
    }
    counts = {"created": 0, "updated": 0, "cancelled": 0}
    seen: set[str] = set()
    for snapshot in snapshots:
        seen.add(snapshot.external_uid)
        row = existing.get(snapshot.external_uid)
        digest = snapshot.digest()
        if row is None:
            db.add(
                Reservation(
                    host_id=prop.host_id,
                    property_id=prop.id,
                    external_uid=snapshot.external_uid,
                    start_at=snapshot.start_at,
                    end_at=snapshot.end_at,
                    source=snapshot.source,
                    summary=snapshot.summary,
                    cancelled=snapshot.cancelled,
                    last_seen_at=now,
                    observed_hash=digest,
                )
            )
            counts["created"] += 1
            if snapshot.cancelled:
                counts["cancelled"] += 1
            audit(
                db,
                host_id=prop.host_id,
                event_type="reservation_cancelled" if snapshot.cancelled else "reservation_imported",
                entity_type="property",
                entity_id=prop.id,
                reason=f"Reservation {snapshot.external_uid} imported",
                details={"external_uid": snapshot.external_uid, "source": snapshot.source},
            )
            continue
        row.last_seen_at = now
        if row.observed_hash == digest:
            continue
        became_cancelled = snapshot.cancelled and not row.cancelled
        row.start_at = snapshot.start_at
        row.end_at = snapshot.end_at
        row.source = snapshot.source
        row.summary = snapshot.summary
        row.cancelled = snapshot.cancelled
        row.observed_hash = digest
        if became_cancelled or snapshot.cancelled:
            counts["cancelled"] += 1
            event_type = "reservation_cancelled"
        else:
            counts["updated"] += 1
            event_type = "reservation_changed"
        audit(
            db,
            host_id=prop.host_id,
            event_type=event_type,
            entity_type="reservation",
            entity_id=row.id,
            reason=f"Reservation {snapshot.external_uid} changed",
            details={"external_uid": snapshot.external_uid},
        )
    for uid, row in existing.items():
        if uid in seen or row.cancelled:
            continue
        row.cancelled = True
        row.last_seen_at = now
        row.observed_hash = ReservationSnapshot(
            external_uid=row.external_uid,
            start_at=row.start_at,
            end_at=row.end_at,
            cancelled=True,
            summary=row.summary,
            source=row.source,
        ).digest()
        counts["cancelled"] += 1
        audit(
            db,
            host_id=prop.host_id,
            event_type="reservation_cancelled",
            entity_type="reservation",
            entity_id=row.id,
            reason=f"Reservation {uid} disappeared from the feed",
            details={"external_uid": uid},
        )
    db.flush()
    return counts


def _priority(window_start: datetime, window_end: datetime, duration_minutes: int, now: datetime) -> str:
    if window_end - window_start <= timedelta(minutes=duration_minutes):
        return "high"
    if window_start - now <= timedelta(hours=24):
        return "high"
    return "normal"


def rebuild_tasks(db: Session, prop: Property, now: datetime) -> list[CleaningTask]:
    reservations = db.scalars(select(Reservation).where(Reservation.property_id == prop.id)).all()
    specs = build_turnovers(
        [Stay(row.external_uid, row.start_at, row.end_at, row.cancelled) for row in reservations],
        zone=ZoneInfo(prop.timezone),
        default_checkin=prop.default_checkin_time,
        duration_minutes=prop.cleaning_duration_minutes,
    )
    existing = db.scalars(select(CleaningTask).where(CleaningTask.property_id == prop.id)).all()
    by_key = {task.turnover_key: task for task in existing}
    by_uid = {row.external_uid: row for row in reservations}
    seen: set[str] = set()
    affected: list[CleaningTask] = []
    for spec in specs:
        seen.add(spec.turnover_key)
        checkout = by_uid.get(spec.checkout_uid)
        checkin = by_uid.get(spec.checkin_uid) if spec.checkin_uid else None
        task = by_key.get(spec.turnover_key)
        if task is None:
            task = CleaningTask(
                host_id=prop.host_id,
                property_id=prop.id,
                turnover_key=spec.turnover_key,
                checkout_reservation_id=checkout.id if checkout else None,
                checkin_reservation_id=checkin.id if checkin else None,
                window_start=spec.window_start,
                window_end=spec.window_end,
                duration_minutes=spec.duration_minutes,
                status=TaskStatus.unassigned,
                priority=_priority(spec.window_start, spec.window_end, spec.duration_minutes, now),
            )
            db.add(task)
            db.flush()
            audit(
                db,
                host_id=prop.host_id,
                event_type="cleaning_task_generated",
                entity_type="cleaning_task",
                entity_id=task.id,
                reason=f"Turnover {spec.turnover_key} created",
                details={"turnover_key": spec.turnover_key},
            )
            affected.append(task)
            continue
        if task.status == TaskStatus.completed:
            continue
        changed = (
            task.window_start != spec.window_start
            or task.window_end != spec.window_end
            or task.duration_minutes != spec.duration_minutes
            or task.status == TaskStatus.blocked
        )
        task.window_start = spec.window_start
        task.window_end = spec.window_end
        task.duration_minutes = spec.duration_minutes
        task.checkout_reservation_id = checkout.id if checkout else None
        task.checkin_reservation_id = checkin.id if checkin else None
        if task.status == TaskStatus.blocked:
            task.status = TaskStatus.unassigned
            task.assigned_cleaner_id = None
            task.current_assignment_id = None
        if changed:
            audit(
                db,
                host_id=prop.host_id,
                event_type="cleaning_task_updated",
                entity_type="cleaning_task",
                entity_id=task.id,
                reason=f"Turnover {spec.turnover_key} changed",
                details={"turnover_key": spec.turnover_key},
            )
            affected.append(task)
    for task in existing:
        if task.turnover_key in seen or task.status in {TaskStatus.completed, TaskStatus.blocked}:
            continue
        _retire(db, task, prop, now)
        affected.append(task)
    return affected


def _retire(db: Session, task: CleaningTask, prop: Property, now: datetime) -> None:
    from turnoverops.domain import LIVE_ASSIGNMENT_STATES, AssignmentState
    from turnoverops.services.workflow import bump, current_assignment

    if task.started_at is not None or task.status == TaskStatus.in_progress:
        bump(db, task)
        task.status = TaskStatus.at_risk
        task.conflict_details = {
            "summary": "The reservation changed while cleaning was in progress",
            "signature": "retired-in-progress",
        }
        audit(
            db,
            host_id=prop.host_id,
            event_type="host_intervention_required",
            entity_type="cleaning_task",
            entity_id=task.id,
            reason="The reservation changed while cleaning was in progress",
        )
        return
    current = current_assignment(db, task)
    if current is not None and current.state in LIVE_ASSIGNMENT_STATES:
        current.state = AssignmentState.cancelled
        current.responded_at = now
    task.assigned_cleaner_id = None
    task.current_assignment_id = None
    task.status = TaskStatus.blocked
    task.conflict_details = {"summary": "The turnover was removed by a reservation change"}
    audit(
        db,
        host_id=prop.host_id,
        event_type="cleaning_task_updated",
        entity_type="cleaning_task",
        entity_id=task.id,
        reason="The turnover was removed by a reservation change",
    )


def sync_snapshots(db: Session, prop: Property, snapshots: list[ReservationSnapshot], now: datetime, settings: Settings) -> dict:
    counts = apply_snapshots(db, prop, snapshots, now)
    affected = rebuild_tasks(db, prop, now)
    for task in affected:
        try:
            reconcile_task(db, task, prop, now, settings)
        except AppError as exc:
            if exc.code != "conflict":
                raise
    prop.sync_status = SyncStatus.ok
    prop.last_synced_at = now
    prop.last_sync_error = None
    counts["tasks_affected"] = len(affected)
    return counts


def generate_tasks(db: Session, prop: Property, now: datetime, settings: Settings) -> list[CleaningTask]:
    rebuild_tasks(db, prop, now)
    return reconcile_open(db, prop, now, settings)


def sync_ical(db: Session, prop: Property, settings: Settings, now: datetime) -> dict:
    if not prop.ical_url:
        raise AppError(409, "feed_missing", "No calendar URL is stored")
    try:
        text = ical_feed.fetch_feed(prop.ical_url, timeout=settings.feed_timeout_seconds, max_bytes=settings.feed_max_bytes)
        snapshots = parse_ical(
            text,
            zone=ZoneInfo(prop.timezone),
            default_checkin=prop.default_checkin_time,
            default_checkout=prop.default_checkout_time,
        )
    except FeedError as exc:
        prop.sync_status = SyncStatus.error
        prop.last_sync_error = str(exc)[:500]
        audit(
            db,
            host_id=prop.host_id,
            event_type="integration_sync_failed",
            entity_type="property",
            entity_id=prop.id,
            reason=str(exc)[:500],
        )
        return {"ok": False, "error": str(exc), "created": 0, "updated": 0, "cancelled": 0, "tasks_affected": 0}
    counts = sync_snapshots(db, prop, snapshots, now, settings)
    counts["ok"] = True
    counts["error"] = None
    return counts


def sync_demo(db: Session, prop: Property, payload: dict, now: datetime, settings: Settings) -> dict:
    snapshots = parse_demo_feed(payload)
    prop.feed_kind = FeedKind.demo
    prop.demo_payload = payload
    prop.ical_url = None
    counts = sync_snapshots(db, prop, snapshots, now, settings)
    counts["ok"] = True
    counts["error"] = None
    return counts


def sync_stored(db: Session, prop: Property, now: datetime, settings: Settings) -> dict:
    if prop.feed_kind == FeedKind.demo:
        if not prop.demo_payload:
            raise AppError(409, "feed_missing", "No demo feed is stored")
        return sync_demo(db, prop, prop.demo_payload, now, settings)
    if prop.feed_kind == FeedKind.ical:
        return sync_ical(db, prop, settings, now)
    raise AppError(409, "feed_missing", "Property has no reservation feed")


def sync_due_icals(db: Session, settings: Settings, now: datetime | None = None) -> None:
    moment = now or datetime.now(UTC)
    properties = db.scalars(select(Property).where(Property.feed_kind == FeedKind.ical, Property.active.is_(True))).all()
    for prop in properties:
        try:
            sync_ical(db, prop, settings, moment)
        except Exception as exc:
            prop.sync_status = SyncStatus.error
            prop.last_sync_error = "Unexpected sync failure"
            audit(
                db,
                host_id=prop.host_id,
                event_type="integration_sync_failed",
                entity_type="property",
                entity_id=prop.id,
                reason=str(exc)[:500],
            )
