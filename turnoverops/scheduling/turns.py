from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from turnoverops.scheduling.windows import open_ended_deadline


@dataclass(frozen=True)
class Stay:
    external_uid: str
    start_at: datetime
    end_at: datetime
    cancelled: bool = False


@dataclass(frozen=True)
class TurnoverSpec:
    turnover_key: str
    checkout_uid: str
    checkin_uid: str | None
    window_start: datetime
    window_end: datetime
    duration_minutes: int


def build_turnovers(
    stays: list[Stay],
    *,
    zone: ZoneInfo,
    default_checkin: time,
    duration_minutes: int,
) -> list[TurnoverSpec]:
    active = sorted(
        (stay for stay in stays if not stay.cancelled),
        key=lambda stay: (stay.end_at, stay.start_at, stay.external_uid),
    )
    duration = timedelta(minutes=duration_minutes)
    specs: list[TurnoverSpec] = []
    for index, stay in enumerate(active):
        nxt = next((item for item in active[index + 1 :] if item.start_at >= stay.end_at), None)
        if nxt is None:
            window_end = open_ended_deadline(stay.end_at, zone, default_checkin, duration)
            key = f"{stay.external_uid}->open"
            checkin_uid = None
        else:
            window_end = nxt.start_at
            key = f"{stay.external_uid}->{nxt.external_uid}"
            checkin_uid = nxt.external_uid
        if window_end <= stay.end_at:
            continue
        specs.append(
            TurnoverSpec(
                turnover_key=key,
                checkout_uid=stay.external_uid,
                checkin_uid=checkin_uid,
                window_start=stay.end_at,
                window_end=window_end,
                duration_minutes=duration_minutes,
            )
        )
    return specs
