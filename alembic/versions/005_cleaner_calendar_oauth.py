"""Cleaner Google Calendar authorization and credentials.

Revision ID: 005
Revises: 004
"""

import sqlalchemy as sa
from alembic import op

revision = "005_calendar_oauth"
down_revision = "004"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("cleaners")}


def upgrade() -> None:
    columns = _columns()
    if "calendar_status" not in columns:
        op.add_column("cleaners", sa.Column("calendar_status", sa.String(24), nullable=False, server_default="not_connected"))
    if "calendar_connect_token_hash" not in columns:
        op.add_column("cleaners", sa.Column("calendar_connect_token_hash", sa.String(64), nullable=True))
    if "calendar_connect_token_expires_at" not in columns:
        op.add_column("cleaners", sa.Column("calendar_connect_token_expires_at", sa.DateTime(), nullable=True))
    if "calendar_oauth_state_hash" not in columns:
        op.add_column("cleaners", sa.Column("calendar_oauth_state_hash", sa.String(64), nullable=True))
    if "calendar_oauth_state_expires_at" not in columns:
        op.add_column("cleaners", sa.Column("calendar_oauth_state_expires_at", sa.DateTime(), nullable=True))
    if "calendar_tokens_encrypted" not in columns:
        op.add_column("cleaners", sa.Column("calendar_tokens_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = _columns()
    for name in (
        "calendar_tokens_encrypted",
        "calendar_oauth_state_expires_at",
        "calendar_oauth_state_hash",
        "calendar_connect_token_expires_at",
        "calendar_connect_token_hash",
        "calendar_status",
    ):
        if name in columns:
            op.drop_column("cleaners", name)
