"""agent_kv — key-value memory for agents (agent-private or workspace-shared)

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "n8o9p0q1r2s3"
down_revision = "m7n8o9p0q1r2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_kv",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("key", sa.String, nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("updated_by_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # NULLS NOT DISTINCT (Postgres 15+): shared rows (agent_id NULL) must also be unique
        # per key, otherwise the upsert's ON CONFLICT never matches them.
        sa.UniqueConstraint(
            "org_id", "agent_id", "key", name="uq_agent_kv_scope_key", postgresql_nulls_not_distinct=True
        ),
    )
    op.create_index("ix_agent_kv_org_agent", "agent_kv", ["org_id", "agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_kv_org_agent", table_name="agent_kv")
    op.drop_table("agent_kv")
