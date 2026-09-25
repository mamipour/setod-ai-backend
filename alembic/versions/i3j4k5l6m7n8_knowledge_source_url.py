"""Add source_url to agent_knowledge_files

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "i3j4k5l6m7n8"
down_revision = "h2i3j4k5l6m7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_knowledge_files",
        sa.Column("source_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_knowledge_files", "source_url")
