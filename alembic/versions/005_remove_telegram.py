"""Remove the retired Telegram integration.

Revision ID: 005
Revises: 004
"""

import sqlalchemy as sa
from alembic import op

revision = "006_remove_telegram"
down_revision = "005_calendar_oauth"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    tables = _tables()
    if "telegram_invites" in tables:
        op.drop_table("telegram_invites")
    if "processed_updates" in tables:
        op.drop_table("processed_updates")
    if "cleaners" in tables:
        columns = _columns("cleaners")
        removable = {name for name in ("telegram_username", "telegram_user_id", "telegram_chat_id", "telegram_status") if name in columns}
        if removable:
            if "ix_cleaners_telegram_user_id" in _indexes("cleaners"):
                op.drop_index("ix_cleaners_telegram_user_id", table_name="cleaners")
            with op.batch_alter_table("cleaners") as batch:
                for name in removable:
                    batch.drop_column(name)


def downgrade() -> None:
    raise RuntimeError("Telegram integration removal is irreversible")
