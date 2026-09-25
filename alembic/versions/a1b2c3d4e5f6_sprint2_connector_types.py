"""Sprint 2 connector types: slack_webhook, google_sheets, whatsapp

Revision ID: a1b2c3d4e5f6
Revises: f3a4b5c6d7e8
Create Date: 2026-09-24

"""
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'slack_webhook'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'google_sheets'")
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'whatsapp'")


def downgrade() -> None:
    pass  # PostgreSQL does not support removing enum values
