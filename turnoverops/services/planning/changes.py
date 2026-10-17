"""Classify reservation diffs. Unchanged polls and duplicate keys do not start planning."""

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from turnoverops.integrations.snapshots import ReservationSnapshot
from turnoverops.models import CalendarChangeEvent, CleaningTask, Property, Reservation
from turnoverops.scheduling.windows import as_utc


def _changed(left, right) -> bool:
    return as_utc(left) != as_utc(right)


def classify_reservation_changes(
    existing: dict[str, Reservation],
    snapshots: list[ReservationSnapshot],
) -> list[tuple[str, str, str, dict]]:
    found: set[str] = set()
    changes: list[tuple[str, str, str, dict]] = []
    for snapshot in snapshots:
        found.add(snapshot.external_uid)
        digest = snapshot.digest()
        row = existing.get(snapshot.external_uid)
        if row is None:
            kind = "cancelled" if snapshot.cancelled else "new"
            changes.append((snapshot.external_uid, kind, digest, {"source": snapshot.source}))
            continue
        if row.observed_hash == digest:
            continue
        if snapshot.cancelled and not row.cancelled:
            changes.append((snapshot.external_uid, "cancelled", digest, {"source": snapshot.source}))
            continue
        end_earlier = as_utc(snapshot.end_at) < as_utc(row.end_at)
        end_later = as_utc(snapshot.end_at) > as_utc(row.end_at)
        start_changed = _changed(snapshot.start_at, row.start_at)
        if end_earlier:
            kind = "earlier_checkout"
        elif end_later:
            kind = "later_checkout"
        elif start_changed:
            kind = "checkin_changed"
        else:
            kind = "modified"
        changes.append((snapshot.external_uid, kind, digest, {"source": snapshot.source}))
    for uid, row in existing.items():
        if uid in found or row.cancelled:
            continue
        removed = ReservationSnapshot(
            external_uid=row.external_uid,
            start_at=row.start_at,
            end_at=row.end_at,
            cancelled=True,
            summary=row.summary,
            guest_name=row.guest_name,
            source=row.source,
        )
        changes.append((uid, "removed", removed.digest(), {"source": row.source}))
    return changes


def record_change_events(db: Session, prop: Property, changes: list[tuple[str, str, str, dict]]) -> list[CalendarChangeEvent]:
    created: list[CalendarChangeEvent] = []
    for uid, kind, digest, details in changes:
        event = CalendarChangeEvent(
            host_id=prop.host_id,
            property_id=prop.id,
            external_uid=uid,
            observed_hash=digest,
            change_type=kind,
            status="recorded",
            details=details,
        )
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
        except IntegrityError:
            continue
        created.append(event)
    return created


def link_event_tasks(db: Session, events: list[CalendarChangeEvent]) -> list[str]:
    task_ids: list[str] = []
    for event in events:
        reservation = db.scalar(
            select(Reservation).where(
                Reservation.property_id == event.property_id,
                Reservation.external_uid == event.external_uid,
            )
        )
        if reservation is None:
            continue
        rows = db.scalars(
            select(CleaningTask).where(
                CleaningTask.property_id == event.property_id,
                or_(
                    CleaningTask.checkout_reservation_id == reservation.id,
                    CleaningTask.checkin_reservation_id == reservation.id,
                ),
            )
        ).all()
        linked = [row.id for row in rows]
        event.details = {**(event.details or {}), "task_ids": linked}
        task_ids.extend(linked)
    return task_ids
