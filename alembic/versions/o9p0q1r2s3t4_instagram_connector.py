"""add instagram connector type

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2026-09-25
"""
from alembic import op

revision = "o9p0q1r2s3t4"
down_revision = "n8o9p0q1r2s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres lets you add a value to an existing enum without rebuilding the table.
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'instagram'")


def downgrade() -> None:
    # Enum values cannot be removed in Postgres without recreating the type.
    # Downgrade is intentionally a no-op — removing a used enum value would break
    # any connector rows of that type.
    pass
