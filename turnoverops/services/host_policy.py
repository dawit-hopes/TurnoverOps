from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.models import HostSettings

DEFAULT_TIMEZONE = "Africa/Addis_Ababa"


@dataclass(frozen=True)
class HostPolicy:
    timezone: str
    default_checkout_time: time
    default_checkin_time: time
    default_cleaning_duration_minutes: int
    auto_reassignment: bool
    trusted_cleaner_policy: bool
    notify_cleaners: bool
    notify_host_on_escalation: bool
    sync_interval_minutes: int
    configuration_confirmed_at: datetime | None = None
    plan_approved_at: datetime | None = None
    daily_planning_enabled: bool = False
    dispatch_mode: str = "review_only"
    daily_plan_time: time = time(6, 0)
    allow_fast_path: bool = True


def ensure_settings(db: Session, host_id: str) -> HostSettings:
    row = db.scalar(select(HostSettings).where(HostSettings.host_id == host_id))
    if row is not None:
        return row
    row = HostSettings(
        host_id=host_id,
        timezone=DEFAULT_TIMEZONE,
        default_checkout_time=time(10, 0),
        default_checkin_time=time(15, 0),
        default_cleaning_duration_minutes=120,
        auto_reassignment=True,
        trusted_cleaner_policy=True,
        notify_cleaners=True,
        notify_host_on_escalation=True,
        sync_interval_minutes=5,
        daily_planning_enabled=False,
        dispatch_mode="review_only",
        daily_plan_time=time(6, 0),
        allow_fast_path=True,
    )
    db.add(row)
    db.flush()
    return row


def load_policy(db: Session, host_id: str) -> HostPolicy:
    row = ensure_settings(db, host_id)
    return HostPolicy(
        timezone=row.timezone,
        default_checkout_time=row.default_checkout_time,
        default_checkin_time=row.default_checkin_time,
        default_cleaning_duration_minutes=row.default_cleaning_duration_minutes,
        auto_reassignment=row.auto_reassignment,
        trusted_cleaner_policy=row.trusted_cleaner_policy,
        notify_cleaners=row.notify_cleaners,
        notify_host_on_escalation=row.notify_host_on_escalation,
        sync_interval_minutes=row.sync_interval_minutes,
        configuration_confirmed_at=row.configuration_confirmed_at,
        plan_approved_at=row.plan_approved_at,
        daily_planning_enabled=row.daily_planning_enabled,
        dispatch_mode=row.dispatch_mode,
        daily_plan_time=row.daily_plan_time,
        allow_fast_path=row.allow_fast_path,
    )
