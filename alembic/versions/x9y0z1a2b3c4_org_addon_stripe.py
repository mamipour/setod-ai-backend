"""org_addons: add stripe_subscription_id and status columns

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
Create Date: 2026-10-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "x9y0z1a2b3c4"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_addons", sa.Column("stripe_subscription_id", sa.String(), nullable=True))
    op.add_column("org_addons", sa.Column("status", sa.String(), nullable=False, server_default="active"))


def downgrade() -> None:
    op.drop_column("org_addons", "stripe_subscription_id")
    op.drop_column("org_addons", "status")
