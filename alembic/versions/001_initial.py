"""Initial schema.

Revision ID: 001
Revises:
Create Date: 2026-10-17
"""

from alembic import op

from turnoverops.models import Base

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
