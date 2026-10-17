"""Host settings, unavailable dates, guest names, manual windows, and task links.

Revision ID: 002
Revises: 001
Create Date: 2026-10-17

Existing databases were created by 001 via create_all. This revision alters
those tables. A fresh upgrade also runs 001 against the current models, so
each change is skipped when the column or table is already present.
"""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _tables() -> set[str]:
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    if "host_settings" not in _tables():
        op.create_table(
            "host_settings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("timezone", sa.String(64), nullable=False),
            sa.Column("default_checkout_time", sa.Time(), nullable=False),
            sa.Column("default_checkin_time", sa.Time(), nullable=False),
            sa.Column("default_cleaning_duration_minutes", sa.Integer(), nullable=False),
            sa.Column("auto_reassignment", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("trusted_cleaner_policy", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("notify_cleaners", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notify_host_on_escalation", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_host_settings_host_id", "host_settings", ["host_id"], unique=True)

    cleaner_columns = _columns("cleaners")
    if "unavailable_dates" not in cleaner_columns:
        op.add_column(
            "cleaners",
            sa.Column("unavailable_dates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )

    reservation_columns = _columns("reservations")
    if "guest_name" not in reservation_columns:
        op.add_column("reservations", sa.Column("guest_name", sa.String(200), nullable=True))

    task_columns = _columns("cleaning_tasks")
    if "feed_window_start" not in task_columns:
        op.add_column("cleaning_tasks", sa.Column("feed_window_start", sa.DateTime(), nullable=True))
    if "feed_window_end" not in task_columns:
        op.add_column("cleaning_tasks", sa.Column("feed_window_end", sa.DateTime(), nullable=True))
    if "feed_duration_minutes" not in task_columns:
        op.add_column("cleaning_tasks", sa.Column("feed_duration_minutes", sa.Integer(), nullable=True))
    if "window_overridden" not in task_columns:
        op.add_column(
            "cleaning_tasks",
            sa.Column("window_overridden", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "host_resolved_at" not in task_columns:
        op.add_column("cleaning_tasks", sa.Column("host_resolved_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE cleaning_tasks SET feed_window_start = window_start "
        "WHERE feed_window_start IS NULL"
    )
    op.execute(
        "UPDATE cleaning_tasks SET feed_window_end = window_end "
        "WHERE feed_window_end IS NULL"
    )
    op.execute(
        "UPDATE cleaning_tasks SET feed_duration_minutes = duration_minutes "
        "WHERE feed_duration_minutes IS NULL"
    )

    notification_columns = _columns("notifications")
    if "task_id" not in notification_columns:
        op.add_column("notifications", sa.Column("task_id", sa.String(36), nullable=True))
        op.create_index("ix_notifications_task_id", "notifications", ["task_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_id" in _columns("notifications"):
        indexes = inspector.get_indexes("notifications")
        for index in indexes:
            if index.get("column_names") == ["task_id"] and index.get("name"):
                op.drop_index(index["name"], table_name="notifications")
        op.drop_column("notifications", "task_id")
    for name in (
        "host_resolved_at",
        "window_overridden",
        "feed_duration_minutes",
        "feed_window_end",
        "feed_window_start",
    ):
        if name in _columns("cleaning_tasks"):
            op.drop_column("cleaning_tasks", name)
    if "guest_name" in _columns("reservations"):
        op.drop_column("reservations", "guest_name")
    if "unavailable_dates" in _columns("cleaners"):
        op.drop_column("cleaners", "unavailable_dates")
    if "host_settings" in _tables():
        op.drop_index("ix_host_settings_host_id", table_name="host_settings")
        op.drop_table("host_settings")
