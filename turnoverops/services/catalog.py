from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from turnoverops.core.errors import AppError
from turnoverops.domain import FeedKind
from turnoverops.integrations.demo_feed import parse_demo_feed
from turnoverops.integrations.ical_feed import validate_public_http_url
from turnoverops.integrations.snapshots import FeedError
from turnoverops.models import Cleaner, CleanerProperty, Host, Property
from turnoverops.schemas import CleanerIn, CleanerUpdate, PropertyIn, PropertyUpdate


def require_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise AppError(422, "invalid_timezone", "Unknown timezone") from exc
    return name


def _check_feed(feed_kind: str, ical_url: str | None, demo_payload: dict | None) -> None:
    if ical_url:
        try:
            validate_public_http_url(ical_url)
        except FeedError as exc:
            raise AppError(422, "invalid_feed_url", str(exc)) from exc
    if demo_payload is not None:
        try:
            parse_demo_feed(demo_payload)
        except FeedError as exc:
            raise AppError(422, "invalid_feed", str(exc)) from exc
    if feed_kind == FeedKind.ical and not ical_url:
        raise AppError(422, "feed_missing", "An iCal feed needs a URL")


def create_property(db: Session, host: Host, body: PropertyIn) -> Property:
    require_zone(body.timezone)
    _check_feed(body.feed_kind, body.ical_url, body.demo_payload)
    prop = Property(host_id=host.id, **body.model_dump())
    db.add(prop)
    db.flush()
    return prop


def update_property(db: Session, prop: Property, body: PropertyUpdate) -> Property:
    data = body.model_dump(exclude_unset=True)
    if "timezone" in data and data["timezone"] is not None:
        require_zone(data["timezone"])
    merged_kind = data.get("feed_kind", prop.feed_kind)
    merged_url = data.get("ical_url", prop.ical_url)
    merged_payload = data.get("demo_payload", prop.demo_payload)
    _check_feed(merged_kind, merged_url, merged_payload if "demo_payload" in data else None)
    for key, value in data.items():
        setattr(prop, key, value)
    return prop


def _owned_properties(db: Session, host_id: str, property_ids: list[str]) -> None:
    if not property_ids:
        return
    found = set(
        db.scalars(select(Property.id).where(Property.host_id == host_id, Property.id.in_(property_ids))).all()
    )
    if found != set(property_ids):
        raise AppError(422, "invalid_property", "One or more properties were not found")


def _replace_links(db: Session, cleaner: Cleaner, property_ids: list[str], preferred_ids: list[str]) -> None:
    if set(preferred_ids) - set(property_ids) and property_ids:
        raise AppError(422, "invalid_preferences", "Preferred properties must be eligible properties")
    _owned_properties(db, cleaner.host_id, list(set(property_ids) | set(preferred_ids)))
    db.execute(delete(CleanerProperty).where(CleanerProperty.cleaner_id == cleaner.id))
    preferred = set(preferred_ids)
    if property_ids:
        for property_id in dict.fromkeys(property_ids):
            db.add(
                CleanerProperty(
                    host_id=cleaner.host_id,
                    cleaner_id=cleaner.id,
                    property_id=property_id,
                    restricts=True,
                    is_preferred=property_id in preferred,
                )
            )
        return
    for property_id in dict.fromkeys(preferred_ids):
        db.add(
            CleanerProperty(
                host_id=cleaner.host_id,
                cleaner_id=cleaner.id,
                property_id=property_id,
                restricts=False,
                is_preferred=True,
            )
        )


def create_cleaner(db: Session, host: Host, body: CleanerIn) -> Cleaner:
    require_zone(body.timezone)
    cleaner = Cleaner(
        host_id=host.id,
        name=body.name,
        telegram_username=body.telegram_username,
        timezone=body.timezone,
        working_days=body.working_days,
        work_start=body.work_start,
        work_end=body.work_end,
        max_jobs_per_day=body.max_jobs_per_day,
    )
    db.add(cleaner)
    db.flush()
    _replace_links(db, cleaner, body.property_ids, body.preferred_property_ids)
    return cleaner


def update_cleaner(db: Session, cleaner: Cleaner, body: CleanerUpdate) -> Cleaner:
    data = body.model_dump(exclude_unset=True)
    if "timezone" in data and data["timezone"] is not None:
        require_zone(data["timezone"])
    property_ids = data.pop("property_ids", None)
    preferred_ids = data.pop("preferred_property_ids", None)
    for key, value in data.items():
        setattr(cleaner, key, value)
    if property_ids is not None or preferred_ids is not None:
        current = db.scalars(select(CleanerProperty).where(CleanerProperty.cleaner_id == cleaner.id)).all()
        if property_ids is None:
            property_ids = [row.property_id for row in current if row.restricts]
        if preferred_ids is None:
            preferred_ids = [row.property_id for row in current if row.is_preferred]
        _replace_links(db, cleaner, property_ids, preferred_ids)
    return cleaner


def onboarding_status(db: Session, host: Host) -> dict:
    has_property = db.scalar(select(Property.id).where(Property.host_id == host.id, Property.active.is_(True))) is not None
    has_cleaner = db.scalar(select(Cleaner.id).where(Cleaner.host_id == host.id, Cleaner.active.is_(True))) is not None
    return {
        "has_property": has_property,
        "has_cleaner": has_cleaner,
        "completed": host.onboarding_completed_at is not None,
    }


def complete_onboarding(db: Session, host: Host) -> dict:
    status = onboarding_status(db, host)
    if not status["has_property"]:
        raise AppError(409, "onboarding_incomplete", "Add a property before completing onboarding")
    host.onboarding_completed_at = datetime.now(UTC)
    status["completed"] = True
    return status
