from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from turnoverops.scheduling.windows import as_utc, parse_aware

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
from turnoverops.services.host_policy import load_policy
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
                    guest_name=snapshot.guest_name,
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
        row.guest_name = snapshot.guest_name
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
            guest_name=row.guest_name,
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


def _same_instant(left: datetime, right: datetime) -> bool:
    return as_utc(left) == as_utc(right)


def _apply_feed_window(task: CleaningTask, spec) -> bool:
    changed = (
        not _same_instant(task.window_start, spec.window_start)
        or not _same_instant(task.window_end, spec.window_end)
        or task.duration_minutes != spec.duration_minutes
        or task.window_overridden
        or task.status == TaskStatus.blocked
    )
    task.window_start = spec.window_start
    task.window_end = spec.window_end
    task.duration_minutes = spec.duration_minutes
    task.feed_window_start = spec.window_start
    task.feed_window_end = spec.window_end
    task.feed_duration_minutes = spec.duration_minutes
    if changed:
        task.window_overridden = False
        task.host_resolved_at = None
    return changed


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
                feed_window_start=spec.window_start,
                feed_window_end=spec.window_end,
                feed_duration_minutes=spec.duration_minutes,
                window_overridden=False,
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
        feed_changed = (
            not _same_instant(task.feed_window_start, spec.window_start)
            or not _same_instant(task.feed_window_end, spec.window_end)
            or task.feed_duration_minutes != spec.duration_minutes
        )
        task.checkout_reservation_id = checkout.id if checkout else None
        task.checkin_reservation_id = checkin.id if checkin else None
        if task.window_overridden and not feed_changed and task.status != TaskStatus.blocked:
            continue
        changed = _apply_feed_window(task, spec) or feed_changed
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
        task.host_resolved_at = None
        task.conflict_details = {
            "summary": "The reservation changed while cleaning was in progress",
            "brief": "The reservation changed while cleaning was in progress, so the current plan no longer fits.",
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
        from turnoverops.services.cleaner_mail import revoke_rejection_tokens

        revoke_rejection_tokens(db, current.id, now)
    task.assigned_cleaner_id = None
    task.current_assignment_id = None
    task.status = TaskStatus.blocked
    task.host_resolved_at = None
    task.conflict_details = {
        "summary": "The turnover was removed by a reservation change",
        "brief": "The reservation changed, so this cleaning was removed from the schedule.",
    }
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
    policy = load_policy(db, prop.host_id)
    for task in affected:
        try:
            reconcile_task(db, task, prop, now, settings, policy=policy)
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
        raise AppError(409, "feed_missing", "This property has no calendar address.")
    try:
        text = ical_feed.fetch_feed(prop.ical_url, timeout=settings.feed_timeout_seconds, max_bytes=settings.feed_max_bytes)
        snapshots = parse_ical(
            text,
            zone=ZoneInfo(prop.timezone),
            default_checkin=prop.default_checkin_time,
            default_checkout=prop.default_checkout_time,
        )
    except FeedError as exc:
        message = _host_sync_error(exc)
        prop.sync_status = SyncStatus.error
        prop.last_sync_error = message
        audit(
            db,
            host_id=prop.host_id,
            event_type="integration_sync_failed",
            entity_type="property",
            entity_id=prop.id,
            reason=message,
            details={"error": str(exc)[:500]},
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
    raise AppError(409, "feed_missing", "This property has no calendar feed.")


def shift_demo_booking(db: Session, task: CleaningTask, prop: Property, days: int, now: datetime, settings: Settings) -> dict:
    if task.started_at is not None or task.status in {TaskStatus.in_progress, TaskStatus.completed}:
        raise AppError(409, "task_locked", "This task can no longer be shifted")
    if prop.feed_kind == FeedKind.ical:
        raise AppError(409, "feed_owned", "These times come from the calendar, so they cannot be edited here.")
    if prop.feed_kind != FeedKind.demo or not prop.demo_payload:
        raise AppError(409, "feed_missing", "Only a sample calendar can be shifted this way.")
    checkout = db.get(Reservation, task.checkout_reservation_id) if task.checkout_reservation_id else None
    checkin = db.get(Reservation, task.checkin_reservation_id) if task.checkin_reservation_id else None
    uids = {row.external_uid for row in (checkout, checkin) if row is not None}
    if not uids:
        raise AppError(409, "feed_missing", "This cleaning is not linked to a sample reservation.")
    payload = {"reservations": []}
    delta = timedelta(days=days)
    for item in prop.demo_payload.get("reservations") or []:
        copied = dict(item)
        if copied.get("external_uid") in uids:
            copied["start"] = (parse_aware(copied["start"]) + delta).isoformat()
            copied["end"] = (parse_aware(copied["end"]) + delta).isoformat()
        payload["reservations"].append(copied)
    return sync_demo(db, prop, payload, now, settings)


def sync_property_feed(db: Session, prop: Property, settings: Settings, now: datetime) -> dict:
    """Fetch and apply a feed without assigning cleaners. A failed fetch leaves reservations in place."""
    from turnoverops.models import CalendarSyncRun
    from turnoverops.services.planning.changes import classify_reservation_changes, record_change_events

    if prop.feed_kind == FeedKind.none:
        return {"ok": True, "synced": False, "error": None, "events": []}
    run = CalendarSyncRun(host_id=prop.host_id, property_id=prop.id, status="running", started_at=now)
    db.add(run)
    db.flush()
    try:
        if prop.feed_kind == FeedKind.demo:
            if not prop.demo_payload:
                run.status = "ok"
                run.finished_at = now
                run.details = {"skipped": "no_demo_payload"}
                return {"ok": True, "synced": False, "error": None, "events": []}
            snapshots = parse_demo_feed(prop.demo_payload)
        elif prop.feed_kind == FeedKind.ical:
            if not prop.ical_url:
                raise FeedError("No calendar URL is stored")
            text = ical_feed.fetch_feed(prop.ical_url, timeout=settings.feed_timeout_seconds, max_bytes=settings.feed_max_bytes)
            snapshots = parse_ical(
                text,
                zone=ZoneInfo(prop.timezone),
                default_checkin=prop.default_checkin_time,
                default_checkout=prop.default_checkout_time,
            )
        else:
            run.status = "ok"
            run.finished_at = now
            return {"ok": True, "synced": False, "error": None, "events": []}
    except FeedError as exc:
        message = _host_sync_error(exc)
        run.status = "failed"
        run.error = message
        run.finished_at = now
        prop.sync_status = SyncStatus.error
        prop.last_sync_error = message
        audit(
            db,
            host_id=prop.host_id,
            event_type="integration_sync_failed",
            entity_type="property",
            entity_id=prop.id,
            reason=message,
            details={"error": str(exc)[:500]},
        )
        return {"ok": False, "synced": False, "error": message, "events": []}
    existing = {
        row.external_uid: row
        for row in db.scalars(select(Reservation).where(Reservation.property_id == prop.id)).all()
    }
    changes = classify_reservation_changes(existing, snapshots)
    apply_snapshots(db, prop, snapshots, now)
    events = record_change_events(db, prop, changes)
    prop.sync_status = SyncStatus.ok
    prop.last_synced_at = now
    prop.last_sync_error = None
    run.status = "ok"
    run.finished_at = now
    run.details = {"events": len(events)}
    return {"ok": True, "synced": True, "error": None, "events": events}


def sync_due_icals(db: Session, settings: Settings, now: datetime | None = None) -> dict[str, list[str]]:
    from turnoverops.services.planning.changes import link_event_tasks

    moment = now or datetime.now(UTC)
    properties = db.scalars(select(Property).where(Property.feed_kind == FeedKind.ical, Property.active.is_(True))).all()
    policies: dict[str, int] = {}
    pending: dict[str, list[str]] = {}
    for prop in properties:
        if prop.host_id not in policies:
            policies[prop.host_id] = load_policy(db, prop.host_id).sync_interval_minutes
        if prop.last_synced_at is not None:
            elapsed = moment - as_utc(prop.last_synced_at)
            if elapsed < timedelta(minutes=policies[prop.host_id]):
                continue
        try:
            outcome = sync_property_feed(db, prop, settings, moment)
        except Exception as exc:
            message = _host_sync_error(FeedError("Unexpected sync failure"))
            prop.sync_status = SyncStatus.error
            prop.last_sync_error = message
            audit(
                db,
                host_id=prop.host_id,
                event_type="integration_sync_failed",
                entity_type="property",
                entity_id=prop.id,
                reason=message,
                details={"error": str(exc)[:500]},
            )
            continue
        if not outcome["ok"] or not outcome["events"]:
            continue
        affected = rebuild_tasks(db, prop, moment)
        task_ids = set(link_event_tasks(db, outcome["events"]))
        task_ids.update(task.id for task in affected)
        if task_ids:
            pending.setdefault(prop.host_id, []).extend(task_ids)
    return pending


def _host_sync_error(exc: Exception) -> str:
    from turnoverops.services.host_copy import for_host

    return for_host(str(exc))
