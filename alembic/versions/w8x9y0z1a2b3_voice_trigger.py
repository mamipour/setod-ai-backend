"""voice_trigger — add TriggerType.phone enum value

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from alembic import op

revision = "w8x9y0z1a2b3"
down_revision = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD VALUE is not transactional in PostgreSQL; run outside transaction.
    op.execute("ALTER TYPE triggertype ADD VALUE IF NOT EXISTS 'phone'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the type.
    # Acceptable for a migration intended to be permanent.
    pass
