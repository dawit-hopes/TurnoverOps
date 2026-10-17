from datetime import UTC, datetime, time
from typing import Generic, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


def _zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown timezone") from exc
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must include a timezone")
    return value.astimezone(UTC)


def _days(value: list[int]) -> list[int]:
    if len(set(value)) != len(value) or any(day not in range(7) for day in value):
        raise ValueError("Working days must be unique integers from 0 (Monday) through 6 (Sunday)")
    return value


class GoogleAuthIn(APIModel):
    id_token: str = Field(min_length=1)


class HostOut(APIModel):
    id: str
    email: str
    name: str
    picture: str | None
    onboarding_completed_at: datetime | None


class AuthOut(APIModel):
    access_token: str
    token_type: str = "bearer"
    host: HostOut


class OnboardingOut(APIModel):
    has_property: bool
    has_cleaner: bool
    completed: bool


class PropertyIn(APIModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None
    timezone: str = "UTC"
    default_checkout_time: time = time(10, 0)
    default_checkin_time: time = time(15, 0)
    cleaning_duration_minutes: int = Field(default=120, ge=15, le=24 * 60)
    feed_kind: str = "none"
    ical_url: str | None = None
    demo_payload: dict | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        return _zone(value)

    @field_validator("feed_kind")
    @classmethod
    def known_feed(cls, value: str) -> str:
        if value not in {"none", "demo", "ical"}:
            raise ValueError("feed_kind must be none, demo, or ical")
        return value


class PropertyUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = None
    timezone: str | None = None
    default_checkout_time: time | None = None
    default_checkin_time: time | None = None
    cleaning_duration_minutes: int | None = Field(default=None, ge=15, le=24 * 60)
    feed_kind: str | None = None
    ical_url: str | None = None
    demo_payload: dict | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str | None) -> str | None:
        return None if value is None else _zone(value)

    @field_validator("feed_kind")
    @classmethod
    def known_feed(cls, value: str | None) -> str | None:
        if value is not None and value not in {"none", "demo", "ical"}:
            raise ValueError("feed_kind must be none, demo, or ical")
        return value


class PropertyOut(APIModel):
    id: str
    name: str
    address: str | None
    timezone: str
    default_checkout_time: time
    default_checkin_time: time
    cleaning_duration_minutes: int
    feed_kind: str
    ical_url: str | None
    demo_payload: dict | None
    sync_status: str
    last_synced_at: datetime | None
    last_sync_error: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class DemoReservationIn(APIModel):
    external_uid: str = Field(min_length=1, max_length=255)
    start: datetime
    end: datetime
    cancelled: bool = False
    summary: str | None = None

    @field_validator("start", "end")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        return _aware(value)


class SyncIn(APIModel):
    reservations: list[DemoReservationIn] | None = None


class SyncOut(APIModel):
    feed_kind: str
    sync_status: str
    last_synced_at: datetime | None
    last_sync_error: str | None
    ok: bool
    error: str | None = None
    created: int
    updated: int
    cancelled: int
    tasks_affected: int


class CleanerIn(APIModel):
    name: str = Field(min_length=1, max_length=200)
    telegram_username: str | None = None
    timezone: str = "UTC"
    working_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    work_start: time = time(9, 0)
    work_end: time = time(17, 0)
    max_jobs_per_day: int | None = Field(default=None, ge=1, le=20)
    property_ids: list[str] = Field(default_factory=list)
    preferred_property_ids: list[str] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        return _zone(value)

    @field_validator("working_days")
    @classmethod
    def days(cls, value: list[int]) -> list[int]:
        return _days(value)

    @model_validator(mode="after")
    def hours(self):
        if self.work_end <= self.work_start:
            raise ValueError("work_end must be after work_start")
        return self


class CleanerUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    telegram_username: str | None = None
    timezone: str | None = None
    working_days: list[int] | None = None
    work_start: time | None = None
    work_end: time | None = None
    max_jobs_per_day: int | None = Field(default=None, ge=1, le=20)
    property_ids: list[str] | None = None
    preferred_property_ids: list[str] | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str | None) -> str | None:
        return None if value is None else _zone(value)

    @field_validator("working_days")
    @classmethod
    def days(cls, value: list[int] | None) -> list[int] | None:
        return None if value is None else _days(value)


class CleanerOut(APIModel):
    id: str
    name: str
    telegram_username: str | None
    telegram_user_id: str | None
    timezone: str
    working_days: list[int]
    work_start: time
    work_end: time
    max_jobs_per_day: int | None
    active: bool
    telegram_status: str
    property_ids: list[str]
    preferred_property_ids: list[str]
    created_at: datetime
    updated_at: datetime


class InviteOut(APIModel):
    invite_url: str
    expires_at: datetime


class ReservationOut(APIModel):
    id: str
    property_id: str
    external_uid: str
    start_at: datetime
    end_at: datetime
    source: str
    summary: str | None
    cancelled: bool
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class AssignmentOut(APIModel):
    id: str
    cleaner_id: str
    cleaner_name: str | None = None
    state: str
    reason: str
    attempt_number: int
    scheduled_start: datetime
    scheduled_end: datetime
    deadline_at: datetime
    responded_at: datetime | None


class TaskOut(APIModel):
    id: str
    property_id: str
    turnover_key: str
    window_start: datetime
    window_end: datetime
    duration_minutes: int
    status: str
    priority: str
    assigned_cleaner_id: str | None
    conflict_details: dict | None
    started_at: datetime | None
    assignment: AssignmentOut | None = None
    created_at: datetime
    updated_at: datetime


class TaskPatch(APIModel):
    priority: str | None = None

    @field_validator("priority")
    @classmethod
    def known_priority(cls, value: str | None) -> str | None:
        if value is not None and value not in {"normal", "high"}:
            raise ValueError("priority must be normal or high")
        return value


class CandidateOut(APIModel):
    cleaner_id: str
    cleaner_name: str
    feasible: bool
    reasons: list[str]
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    jobs_that_day: int
    preferred: bool
    slack_minutes: int
    summary: str


class DecisionOut(APIModel):
    selected_cleaner_id: str | None
    candidates: list[CandidateOut]


class PreviewIn(APIModel):
    property_id: str
    window_start: datetime
    window_end: datetime
    duration_minutes: int = Field(ge=15, le=24 * 60)

    @field_validator("window_start", "window_end")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        return _aware(value)


class TaskIdIn(APIModel):
    task_id: str


class PropertyIdIn(APIModel):
    property_id: str


class RunIn(APIModel):
    property_id: str | None = None


class ProposeIn(APIModel):
    cleaner_id: str | None = None


class ReasonIn(APIModel):
    reason: str | None = None


class AuditOut(APIModel):
    id: str
    event_type: str
    entity_type: str
    entity_id: str
    reason: str
    details: dict | None
    correlation_id: str | None
    created_at: datetime


class NotificationOut(APIModel):
    id: str
    recipient_type: str
    recipient_id: str
    channel: str
    message_type: str
    body: str
    status: str
    retry_count: int
    next_retry_at: datetime | None
    provider_response: dict | None
    created_at: datetime


class MetricsOut(APIModel):
    properties: int
    cleaners: int
    tasks_today: int
    awaiting_confirmation: int
    at_risk: int
    blocked: int
    unassigned: int


class DashboardOut(APIModel):
    metrics: MetricsOut
    today: list[TaskOut]
    upcoming: list[TaskOut]
    needs_attention: list[TaskOut]


class CalendarConnectOut(APIModel):
    authorization_url: str


class CalendarCallbackIn(APIModel):
    code: str = Field(min_length=1)
    state: str = Field(min_length=1)


class IntegrationOut(APIModel):
    google_login: dict
    telegram: dict
    gemini: dict
    google_calendar: dict
