"""agent_memory_entries — pgvector episodic memory

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "l6m7n8o9p0q1"
down_revision = "k5l6m7n8o9p0"
branch_labels = None
depends_on = None

EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "agent_memory_entries",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column(
            "session_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # IVFFlat index for fast approximate cosine search (nlist=100 is standard for <1M rows)
    op.execute(
        "CREATE INDEX ix_agent_memory_entries_embedding "
        "ON agent_memory_entries USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.drop_table("agent_memory_entries")
