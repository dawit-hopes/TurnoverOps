from datetime import datetime, time

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from turnoverops.domain import AssignmentState, FeedKind, SyncStatus, TaskStatus
from turnoverops.models.base import Base, Timestamped, UtcDateTime, utcnow


def _uuid() -> str:
    from uuid import uuid4

    return str(uuid4())


class Host(Timestamped, Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(200))
    picture: Mapped[str | None] = mapped_column(String(500))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class HostSettings(Timestamped, Base):
    __tablename__ = "host_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Addis_Ababa")
    default_checkout_time: Mapped[time] = mapped_column(Time, default=time(10, 0))
    default_checkin_time: Mapped[time] = mapped_column(Time, default=time(15, 0))
    default_cleaning_duration_minutes: Mapped[int] = mapped_column(Integer, default=120)
    auto_reassignment: Mapped[bool] = mapped_column(Boolean, default=True)
    trusted_cleaner_policy: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_cleaners: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_host_on_escalation: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    configuration_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    plan_approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    daily_planning_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_mode: Mapped[str] = mapped_column(String(32), default="review_only")
    daily_plan_time: Mapped[time] = mapped_column(Time, default=time(6, 0))
    allow_fast_path: Mapped[bool] = mapped_column(Boolean, default=True)


class Property(Timestamped, Base):
    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    default_checkout_time: Mapped[time] = mapped_column(Time)
    default_checkin_time: Mapped[time] = mapped_column(Time)
    cleaning_duration_minutes: Mapped[int] = mapped_column(Integer)
    feed_kind: Mapped[str] = mapped_column(String(16), default=FeedKind.none)
    ical_url: Mapped[str | None] = mapped_column(String(1000))
    demo_payload: Mapped[dict | None] = mapped_column(JSON)
    sync_status: Mapped[str] = mapped_column(String(16), default=SyncStatus.never)
    last_synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_sync_error: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Cleaner(Timestamped, Base):
    __tablename__ = "cleaners"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    telegram_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64))
    working_days: Mapped[list] = mapped_column(JSON)
    work_start: Mapped[time] = mapped_column(Time)
    work_end: Mapped[time] = mapped_column(Time)
    max_jobs_per_day: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_status: Mapped[str] = mapped_column(String(32), default="disconnected")
    unavailable_dates: Mapped[list] = mapped_column(JSON, default=list)
    calendar_status: Mapped[str] = mapped_column(String(24), default="not_connected")
    calendar_connect_token_hash: Mapped[str | None] = mapped_column(String(64))
    calendar_connect_token_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    calendar_oauth_state_hash: Mapped[str | None] = mapped_column(String(64))
    calendar_oauth_state_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    calendar_tokens_encrypted: Mapped[str | None] = mapped_column(Text)


class CleanerProperty(Base):
    __tablename__ = "cleaner_properties"
    __table_args__ = (UniqueConstraint("cleaner_id", "property_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    cleaner_id: Mapped[str] = mapped_column(ForeignKey("cleaners.id"), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    restricts: Mapped[bool] = mapped_column(Boolean, default=False)


class Reservation(Timestamped, Base):
    __tablename__ = "reservations"
    __table_args__ = (UniqueConstraint("property_id", "external_uid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    external_uid: Mapped[str] = mapped_column(String(255))
    start_at: Mapped[datetime] = mapped_column(UtcDateTime)
    end_at: Mapped[datetime] = mapped_column(UtcDateTime)
    source: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(String(300))
    guest_name: Mapped[str | None] = mapped_column(String(200))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    observed_hash: Mapped[str] = mapped_column(String(64))


class CleaningTask(Timestamped, Base):
    __tablename__ = "cleaning_tasks"
    __table_args__ = (UniqueConstraint("property_id", "turnover_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    turnover_key: Mapped[str] = mapped_column(String(520))
    checkout_reservation_id: Mapped[str | None] = mapped_column(ForeignKey("reservations.id"))
    checkin_reservation_id: Mapped[str | None] = mapped_column(ForeignKey("reservations.id"))
    window_start: Mapped[datetime] = mapped_column(UtcDateTime)
    window_end: Mapped[datetime] = mapped_column(UtcDateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    feed_window_start: Mapped[datetime] = mapped_column(UtcDateTime)
    feed_window_end: Mapped[datetime] = mapped_column(UtcDateTime)
    feed_duration_minutes: Mapped[int] = mapped_column(Integer)
    window_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    host_resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    assigned_cleaner_id: Mapped[str | None] = mapped_column(ForeignKey("cleaners.id"))
    current_assignment_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.unassigned, index=True)
    instructions: Mapped[str | None] = mapped_column(String(2000))
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    assignment_version: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    conflict_details: Mapped[dict | None] = mapped_column(JSON)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("cleaning_tasks.id"), index=True)
    cleaner_id: Mapped[str] = mapped_column(ForeignKey("cleaners.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default=AssignmentState.awaiting_confirmation, index=True)
    reason: Mapped[str] = mapped_column(String(500))
    explanation: Mapped[dict | None] = mapped_column(JSON)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    window_start: Mapped[datetime] = mapped_column(UtcDateTime)
    window_end: Mapped[datetime] = mapped_column(UtcDateTime)
    scheduled_start: Mapped[datetime] = mapped_column(UtcDateTime)
    scheduled_end: Mapped[datetime] = mapped_column(UtcDateTime)
    proposed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    deadline_at: Mapped[datetime] = mapped_column(UtcDateTime)
    responded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    dispatched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class RejectionToken(Base):
    __tablename__ = "rejection_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id"), index=True)
    cleaner_id: Mapped[str] = mapped_column(ForeignKey("cleaners.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Notification(Timestamped, Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("host_id", "dedupe_key", name="uq_notifications_host_dedupe"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    recipient_type: Mapped[str] = mapped_column(String(16))
    recipient_id: Mapped[str] = mapped_column(String(36))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("cleaning_tasks.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    message_type: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(16), index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    provider_response: Mapped[dict | None] = mapped_column(JSON)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str | None] = mapped_column(String(255))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    reason: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class IntegrationState(Base):
    __tablename__ = "integration_states"
    __table_args__ = (UniqueConstraint("host_id", "provider", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class TelegramInvite(Base):
    __tablename__ = "telegram_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    cleaner_id: Mapped[str] = mapped_column(ForeignKey("cleaners.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    __table_args__ = (UniqueConstraint("provider", "update_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32))
    update_id: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class CalendarEventMapping(Base):
    __tablename__ = "calendar_event_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("cleaning_tasks.id"), unique=True)
    calendar_id: Mapped[str] = mapped_column(String(200), default="primary")
    google_event_id: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class DailyPlanningRun(Base):
    __tablename__ = "daily_planning_runs"
    __table_args__ = (
        Index(
            "uq_daily_planning_runs_host_running",
            "host_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str | None] = mapped_column(String(64))
    operational_date: Mapped[str] = mapped_column(String(10))
    timezone: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(String(500))
    tasks_considered: Mapped[int] = mapped_column(Integer, default=0)
    assignments_created: Mapped[int] = mapped_column(Integer, default=0)
    assignments_preserved: Mapped[int] = mapped_column(Integer, default=0)
    assignments_changed: Mapped[int] = mapped_column(Integer, default=0)
    unassigned_count: Mapped[int] = mapped_column(Integer, default=0)
    conflicts: Mapped[list | None] = mapped_column(JSON)
    warnings: Mapped[list | None] = mapped_column(JSON)
    reasoning_summary: Mapped[str | None] = mapped_column(String(500))
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_skipped_reason: Mapped[str | None] = mapped_column(String(64))
    llm_provider: Mapped[str | None] = mapped_column(String(32))
    llm_model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    repair_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_outcome: Mapped[str | None] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    scope_fingerprint: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON)
    details: Mapped[dict | None] = mapped_column(JSON)


class CalendarSyncRun(Base):
    __tablename__ = "calendar_sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)


class CalendarChangeEvent(Base):
    __tablename__ = "calendar_change_events"
    __table_args__ = (
        UniqueConstraint(
            "property_id",
            "external_uid",
            "observed_hash",
            "change_type",
            name="uq_calendar_change_event",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    external_uid: Mapped[str] = mapped_column(String(255))
    observed_hash: Mapped[str] = mapped_column(String(64))
    change_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="recorded", index=True)
    error: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    notification_id: Mapped[str | None] = mapped_column(ForeignKey("notifications.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("cleaning_tasks.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), index=True)
    recipient: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(2000))
    error: Mapped[str | None] = mapped_column(String(500))
    provider_response: Mapped[dict | None] = mapped_column(JSON)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ScheduledJobRun(Base):
    __tablename__ = "scheduled_job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"), index=True)
    job_name: Mapped[str] = mapped_column(String(64))
    operational_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict | None] = mapped_column(JSON)
