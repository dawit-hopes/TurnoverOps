"""Planning runs, calendar changes, email deliveries, and dispatch policy.

Revision ID: 003
Revises: 002
Create Date: 2026-10-17

A fresh upgrade runs 001 against the current models, so each change is skipped
when the column, table, or index is already present.
"""

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _tables() -> set[str]:
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def _indexes(table: str) -> set[str]:
    bind = op.get_bind()
    return {index["name"] for index in sa.inspect(bind).get_indexes(table) if index.get("name")}


def _unique_names(table: str) -> set[str]:
    bind = op.get_bind()
    return {item["name"] for item in sa.inspect(bind).get_unique_constraints(table) if item.get("name")}


def upgrade() -> None:
    settings_columns = _columns("host_settings") if "host_settings" in _tables() else set()
    if "configuration_confirmed_at" not in settings_columns:
        op.add_column("host_settings", sa.Column("configuration_confirmed_at", sa.DateTime(), nullable=True))
    if "plan_approved_at" not in settings_columns:
        op.add_column("host_settings", sa.Column("plan_approved_at", sa.DateTime(), nullable=True))
    if "daily_planning_enabled" not in settings_columns:
        op.add_column(
            "host_settings",
            sa.Column("daily_planning_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "dispatch_mode" not in settings_columns:
        op.add_column(
            "host_settings",
            sa.Column("dispatch_mode", sa.String(32), nullable=False, server_default="review_only"),
        )
    if "daily_plan_time" not in settings_columns:
        op.add_column("host_settings", sa.Column("daily_plan_time", sa.Time(), nullable=False, server_default="06:00:00"))
    if "allow_fast_path" not in settings_columns:
        op.add_column(
            "host_settings",
            sa.Column("allow_fast_path", sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    assignment_columns = _columns("assignments")
    if "source" not in assignment_columns:
        op.add_column("assignments", sa.Column("source", sa.String(32), nullable=False, server_default="manual"))
    if "dispatched_at" not in assignment_columns:
        op.add_column("assignments", sa.Column("dispatched_at", sa.DateTime(), nullable=True))

    notification_columns = _columns("notifications")
    if "dedupe_key" not in notification_columns:
        op.add_column("notifications", sa.Column("dedupe_key", sa.String(255), nullable=True))
    if "uq_notifications_host_dedupe" not in _unique_names("notifications") and "uq_notifications_host_dedupe" not in _indexes(
        "notifications"
    ):
        op.create_index("uq_notifications_host_dedupe", "notifications", ["host_id", "dedupe_key"], unique=True)

    if "daily_planning_runs" not in _tables():
        op.create_table(
            "daily_planning_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("trigger", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("stage", sa.String(64), nullable=True),
            sa.Column("operational_date", sa.String(10), nullable=False),
            sa.Column("timezone", sa.String(64), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("tasks_considered", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("assignments_created", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("assignments_preserved", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("assignments_changed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unassigned_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("conflicts", sa.JSON(), nullable=True),
            sa.Column("warnings", sa.JSON(), nullable=True),
            sa.Column("reasoning_summary", sa.String(500), nullable=True),
            sa.Column("requires_human_review", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("llm_skipped_reason", sa.String(64), nullable=True),
            sa.Column("llm_provider", sa.String(32), nullable=True),
            sa.Column("llm_model", sa.String(64), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("repair_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("validation_outcome", sa.String(32), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("estimated_cost", sa.Float(), nullable=True),
            sa.Column("scope_fingerprint", sa.String(64), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
        )
        op.create_index("ix_daily_planning_runs_host_id", "daily_planning_runs", ["host_id"])
        op.create_index("ix_daily_planning_runs_status", "daily_planning_runs", ["status"])
    if "uq_daily_planning_runs_host_running" not in _indexes("daily_planning_runs"):
        op.create_index(
            "uq_daily_planning_runs_host_running",
            "daily_planning_runs",
            ["host_id"],
            unique=True,
            sqlite_where=sa.text("status = 'running'"),
            postgresql_where=sa.text("status = 'running'"),
        )

    if "calendar_sync_runs" not in _tables():
        op.create_table(
            "calendar_sync_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("property_id", sa.String(36), sa.ForeignKey("properties.id"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
        )
        op.create_index("ix_calendar_sync_runs_host_id", "calendar_sync_runs", ["host_id"])
        op.create_index("ix_calendar_sync_runs_property_id", "calendar_sync_runs", ["property_id"])
        op.create_index("ix_calendar_sync_runs_status", "calendar_sync_runs", ["status"])

    if "calendar_change_events" not in _tables():
        op.create_table(
            "calendar_change_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("property_id", sa.String(36), sa.ForeignKey("properties.id"), nullable=False),
            sa.Column("external_uid", sa.String(255), nullable=False),
            sa.Column("observed_hash", sa.String(64), nullable=False),
            sa.Column("change_type", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="recorded"),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "property_id",
                "external_uid",
                "observed_hash",
                "change_type",
                name="uq_calendar_change_event",
            ),
        )
        op.create_index("ix_calendar_change_events_host_id", "calendar_change_events", ["host_id"])
        op.create_index("ix_calendar_change_events_property_id", "calendar_change_events", ["property_id"])
        op.create_index("ix_calendar_change_events_status", "calendar_change_events", ["status"])

    if "notification_deliveries" not in _tables():
        op.create_table(
            "notification_deliveries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("notification_id", sa.String(36), sa.ForeignKey("notifications.id"), nullable=True),
            sa.Column("task_id", sa.String(36), sa.ForeignKey("cleaning_tasks.id"), nullable=True),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("dedupe_key", sa.String(255), nullable=True),
            sa.Column("recipient", sa.String(320), nullable=True),
            sa.Column("subject", sa.String(200), nullable=True),
            sa.Column("body", sa.String(2000), nullable=False),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("provider_response", sa.JSON(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_notification_deliveries_host_id", "notification_deliveries", ["host_id"])
        op.create_index("ix_notification_deliveries_task_id", "notification_deliveries", ["task_id"])
        op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])
        op.create_index("ix_notification_deliveries_dedupe_key", "notification_deliveries", ["dedupe_key"])

    if "scheduled_job_runs" not in _tables():
        op.create_table(
            "scheduled_job_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("job_name", sa.String(64), nullable=False),
            sa.Column("operational_date", sa.String(10), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
        )
        op.create_index("ix_scheduled_job_runs_host_id", "scheduled_job_runs", ["host_id"])
        op.create_index("ix_scheduled_job_runs_status", "scheduled_job_runs", ["status"])


def downgrade() -> None:
    if "scheduled_job_runs" in _tables():
        op.drop_table("scheduled_job_runs")
    if "notification_deliveries" in _tables():
        op.drop_table("notification_deliveries")
    if "calendar_change_events" in _tables():
        op.drop_table("calendar_change_events")
    if "calendar_sync_runs" in _tables():
        op.drop_table("calendar_sync_runs")
    if "daily_planning_runs" in _tables():
        op.drop_table("daily_planning_runs")
    if "uq_notifications_host_dedupe" in _unique_names("notifications") or "uq_notifications_host_dedupe" in _indexes(
        "notifications"
    ):
        op.drop_index("uq_notifications_host_dedupe", table_name="notifications")
    if "dedupe_key" in _columns("notifications"):
        op.drop_column("notifications", "dedupe_key")
    if "dispatched_at" in _columns("assignments"):
        op.drop_column("assignments", "dispatched_at")
    if "source" in _columns("assignments"):
        op.drop_column("assignments", "source")
    for name in (
        "allow_fast_path",
        "daily_plan_time",
        "dispatch_mode",
        "daily_planning_enabled",
        "plan_approved_at",
        "configuration_confirmed_at",
    ):
        if name in _columns("host_settings"):
            op.drop_column("host_settings", name)
