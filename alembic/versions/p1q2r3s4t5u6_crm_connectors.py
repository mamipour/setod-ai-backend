"""add hubspot and pipedrive connector types

Revision ID: p1q2r3s4t5u6
Revises: o9p0q1r2s3t4
Create Date: 2026-09-26
"""
from alembic import op

revision = "p1q2r3s4t5u6"
down_revision = "o9p0q1r2s3t4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'hubspot'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'pipedrive'")


def downgrade() -> None:
    # Enum values cannot be removed in Postgres without recreating the type.
    pass
