from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from turnoverops.scheduling.windows import as_utc, earliest_start


@dataclass(frozen=True)
class CleanerInput:
    id: str
    name: str
    active: bool
    timezone: str
    working_days: frozenset[int]
    work_start: time
    work_end: time
    max_jobs_per_day: int | None
    eligible_property_ids: frozenset[str] | None
    preferred_property_ids: frozenset[str]
    unavailable_dates: frozenset[date] = field(default_factory=frozenset)
    calendar_status: str = "not_connected"


@dataclass(frozen=True)
class BusyInterval:
    cleaner_id: str
    task_id: str
    start: datetime
    end: datetime
    counts_toward_daily_limit: bool = True


@dataclass(frozen=True)
class JobWindow:
    property_id: str
    property_timezone: str
    window_start: datetime
    window_end: datetime
    duration_minutes: int


@dataclass(frozen=True)
class Evaluation:
    cleaner_id: str
    cleaner_name: str
    feasible: bool
    reasons: tuple[str, ...]
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    jobs_that_day: int
    preferred: bool
    slack_seconds: int
    external_availability_verified: bool = False

    @property
    def score(self) -> tuple:
        return (self.jobs_that_day, 0 if self.preferred else 1, -self.slack_seconds, self.cleaner_id)


@dataclass(frozen=True)
class Decision:
    ranked_feasible: tuple[Evaluation, ...]
    rejected: tuple[Evaluation, ...]

    @property
    def selected(self) -> Evaluation | None:
        return self.ranked_feasible[0] if self.ranked_feasible else None


def describe(evaluation: Evaluation) -> str:
    if not evaluation.feasible:
        return f"{evaluation.cleaner_name} cannot take this job: {', '.join(evaluation.reasons)}"
    start = evaluation.scheduled_start.isoformat() if evaluation.scheduled_start else "unscheduled"
    summary = (
        f"{evaluation.cleaner_name} can start at {start} with {evaluation.jobs_that_day} other jobs that day, "
        f"preferred={str(evaluation.preferred).lower()}, slack={evaluation.slack_seconds // 60} minutes"
    )
    if not evaluation.external_availability_verified:
        summary += "; external calendar availability unverified"
    return summary


def _overlaps(start: datetime, end: datetime, busy: BusyInterval) -> bool:
    return start < busy.end and busy.start < end


def evaluate_cleaner(cleaner: CleanerInput, window: JobWindow, busy: list[BusyInterval]) -> Evaluation:
    reasons: list[str] = []
    if not cleaner.active:
        reasons.append("inactive")
    if cleaner.calendar_status == "access_error":
        reasons.append("calendar_unavailable")
    property_zone = ZoneInfo(window.property_timezone)
    job_day = as_utc(window.window_start).astimezone(property_zone).date()
    if job_day in cleaner.unavailable_dates:
        reasons.append("unavailable")
    eligible = cleaner.eligible_property_ids
    if eligible is not None and window.property_id not in eligible:
        reasons.append("not_eligible")
    duration = timedelta(minutes=window.duration_minutes)
    if window.window_end - window.window_start < duration:
        reasons.append("insufficient_window")

    start = None
    saw_overlap = False
    if not reasons:
        zone = ZoneInfo(cleaner.timezone)
        cursor = as_utc(window.window_start)
        deadline = as_utc(window.window_end)
        while cursor < deadline:
            candidate = earliest_start(
                cursor,
                deadline,
                duration,
                zone,
                set(cleaner.working_days),
                cleaner.work_start,
                cleaner.work_end,
            )
            if candidate is None:
                break
            overlapping = [
                item for item in busy
                if item.cleaner_id == cleaner.id and _overlaps(candidate, candidate + duration, item)
            ]
            if not overlapping:
                start = candidate
                break
            saw_overlap = True
            cursor = max(item.end for item in overlapping)
        if start is None:
            reasons.append("overlap" if saw_overlap else "outside_working_hours")

    end = start + duration if start is not None else None
    jobs_that_day = 0
    if start is not None and end is not None:
        day = start.astimezone(property_zone).date()
        for item in busy:
            if item.cleaner_id != cleaner.id:
                continue
            if _overlaps(start, end, item):
                reasons.append("overlap")
                break
            if item.counts_toward_daily_limit and item.start.astimezone(property_zone).date() == day:
                jobs_that_day += 1
        if cleaner.max_jobs_per_day is not None and jobs_that_day >= cleaner.max_jobs_per_day:
            reasons.append("daily_job_limit")

    feasible = not reasons
    slack = int((as_utc(window.window_end) - end).total_seconds()) if end is not None and feasible else 0
    preferred = window.property_id in cleaner.preferred_property_ids
    return Evaluation(
        cleaner_id=cleaner.id,
        cleaner_name=cleaner.name,
        feasible=feasible,
        reasons=tuple(dict.fromkeys(reasons)),
        scheduled_start=start if feasible else None,
        scheduled_end=end if feasible else None,
        jobs_that_day=jobs_that_day,
        preferred=preferred,
        slack_seconds=slack,
        external_availability_verified=cleaner.calendar_status == "connected",
    )


def decide(cleaners: list[CleanerInput], window: JobWindow, busy: list[BusyInterval]) -> Decision:
    evaluations = [evaluate_cleaner(cleaner, window, busy) for cleaner in cleaners]
    feasible = tuple(sorted((item for item in evaluations if item.feasible), key=lambda item: item.score))
    rejected = tuple(sorted((item for item in evaluations if not item.feasible), key=lambda item: item.cleaner_id))
    return Decision(ranked_feasible=feasible, rejected=rejected)
