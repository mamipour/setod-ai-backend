"""Add model_slug to agent_sessions

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "j4k5l6m7n8o9"
down_revision = "i3j4k5l6m7n8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("model_slug", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "model_slug")
