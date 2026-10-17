from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Datetime must include a timezone")
    return parsed.astimezone(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime must include a timezone")
    return value.astimezone(UTC)


def combine_local(day: date, clock: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, clock).replace(tzinfo=zone)


def open_ended_deadline(
    checkout: datetime,
    zone: ZoneInfo,
    checkin_time: time,
    duration: timedelta,
) -> datetime:
    local = checkout.astimezone(zone)
    same_day = combine_local(local.date(), checkin_time, zone)
    candidate = same_day if same_day > checkout else combine_local(local.date() + timedelta(days=1), checkin_time, zone)
    return max(candidate, checkout + duration)


def earliest_start(
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    zone: ZoneInfo,
    working_days: set[int],
    work_start: time,
    work_end: time,
) -> datetime | None:
    if work_end <= work_start or window_end - window_start < duration:
        return None
    latest_start = window_end - duration
    day = window_start.astimezone(zone).date()
    last_day = latest_start.astimezone(zone).date()
    while day <= last_day:
        if day.weekday() in working_days:
            day_start = combine_local(day, work_start, zone)
            day_end = combine_local(day, work_end, zone)
            start = max(window_start, day_start)
            if start + duration <= min(window_end, day_end) and start <= latest_start:
                return as_utc(start)
        day += timedelta(days=1)
    return None
