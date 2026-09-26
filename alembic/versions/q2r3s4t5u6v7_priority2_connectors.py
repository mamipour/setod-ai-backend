"""add notion, airtable, shopify, google_business_profile connector types

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-09-26
"""
from alembic import op

revision = "q2r3s4t5u6v7"
down_revision = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'notion'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'airtable'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'shopify'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'google_business_profile'")


def downgrade() -> None:
    pass
