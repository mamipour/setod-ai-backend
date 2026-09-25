"""agent_scenarios table

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "h2i3j4k5l6m7"
down_revision = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_scenarios",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("expected_tools", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("last_ran_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("agent_scenarios")
