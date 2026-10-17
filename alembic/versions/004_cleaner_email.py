"""Cleaner email, task instructions, and rejection tokens.

Revision ID: 004
Revises: 003
Create Date: 2026-10-17

A fresh upgrade runs 001 against the current models, so each change is skipped
when the column or table is already present.
"""

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _tables() -> set[str]:
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "cleaners" in tables and "email" not in _columns("cleaners"):
        op.add_column("cleaners", sa.Column("email", sa.String(320), nullable=True))
    if "cleaning_tasks" in tables and "instructions" not in _columns("cleaning_tasks"):
        op.add_column("cleaning_tasks", sa.Column("instructions", sa.String(2000), nullable=True))
    if "rejection_tokens" not in tables:
        op.create_table(
            "rejection_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("host_id", sa.String(36), sa.ForeignKey("hosts.id"), nullable=False),
            sa.Column("assignment_id", sa.String(36), sa.ForeignKey("assignments.id"), nullable=False),
            sa.Column("cleaner_id", sa.String(36), sa.ForeignKey("cleaners.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_rejection_tokens_host_id", "rejection_tokens", ["host_id"])
        op.create_index("ix_rejection_tokens_assignment_id", "rejection_tokens", ["assignment_id"])
        op.create_index("ix_rejection_tokens_cleaner_id", "rejection_tokens", ["cleaner_id"])
        op.create_index("ix_rejection_tokens_token_hash", "rejection_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    if "rejection_tokens" in _tables():
        op.drop_table("rejection_tokens")
    if "cleaning_tasks" in _tables() and "instructions" in _columns("cleaning_tasks"):
        op.drop_column("cleaning_tasks", "instructions")
    if "cleaners" in _tables() and "email" in _columns("cleaners"):
        op.drop_column("cleaners", "email")
