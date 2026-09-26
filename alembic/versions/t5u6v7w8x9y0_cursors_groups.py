"""agent_cursors table; group + speaker + reply fields on conversations

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-09-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "t5u6v7w8x9y0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── agent_cursors ─────────────────────────────────────────────────────────
    op.create_table(
        "agent_cursors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("cursor", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_id", "connector_id", "scope", name="uq_agent_cursor"),
    )

    # ── conversations.is_group ────────────────────────────────────────────────
    op.add_column(
        "conversations",
        sa.Column("is_group", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # ── conversation_messages: speaker + reply threading ──────────────────────
    op.add_column(
        "conversation_messages",
        sa.Column("speaker_id", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("speaker_name", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("reply_to_external_id", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("reply_to_text", sa.String(), nullable=False, server_default=""),
    )

    # ── real dedup for inbound messages ───────────────────────────────────────
    # record_inbound's ON CONFLICT DO NOTHING on conversation_messages had nothing to
    # conflict against, so a redelivered webhook stored the message twice.  Outbound
    # rows have external_id = '' and are excluded from the index.
    op.execute("""
        CREATE UNIQUE INDEX uq_conv_message_external
        ON conversation_messages (conversation_id, external_id)
        WHERE external_id <> ''
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_conv_message_external")
    op.drop_column("conversation_messages", "reply_to_text")
    op.drop_column("conversation_messages", "reply_to_external_id")
    op.drop_column("conversation_messages", "speaker_name")
    op.drop_column("conversation_messages", "speaker_id")
    op.drop_column("conversations", "is_group")
    op.drop_table("agent_cursors")
