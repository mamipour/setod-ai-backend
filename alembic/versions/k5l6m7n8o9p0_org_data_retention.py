"""Add data retention policy to organizations

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "k5l6m7n8o9p0"
down_revision = "j4k5l6m7n8o9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("data_retention_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("scrub_content_only", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("organizations", "scrub_content_only")
    op.drop_column("organizations", "data_retention_days")
