"""add conversations + conversation_messages tables; link sessions and inbound_events

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-09-26
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "s4t5u6v7w8x9"
down_revision = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New enums ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TYPE conversationstatus AS ENUM ('open', 'human', 'closed')
    """)
    op.execute("""
        CREATE TYPE messagedirection AS ENUM ('inbound', 'outbound')
    """)
    op.execute("""
        CREATE TYPE messageauthor AS ENUM ('peer', 'agent', 'human')
    """)
    op.execute("""
        CREATE TYPE messagekind AS ENUM (
            'text', 'image', 'audio', 'video', 'document',
            'location', 'sticker', 'contact', 'other'
        )
    """)

    # ── conversations ──────────────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("connectors.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("channel", sa.String(), nullable=False, index=True),
        sa.Column("peer_id", sa.String(), nullable=False),
        sa.Column("peer_name", sa.String(), nullable=False, server_default=""),
        sa.Column("thread_key", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.Enum("open", "human", "closed",
                                    name="conversationstatus"), nullable=False,
                  server_default="open"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_through_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("connector_id", "peer_id", "thread_key",
                            name="uq_conversation"),
    )
    op.create_index("ix_conversations_connector_last", "conversations",
                    ["connector_id", "last_inbound_at"])

    # ── conversation_messages ──────────────────────────────────────────────────
    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("direction", sa.Enum("inbound", "outbound",
                                       name="messagedirection"), nullable=False),
        sa.Column("author", sa.Enum("peer", "agent", "human",
                                    name="messageauthor"), nullable=False),
        sa.Column("kind", sa.Enum("text", "image", "audio", "video", "document",
                                  "location", "sticker", "contact", "other",
                                  name="messagekind"), nullable=False,
                  server_default="text"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("attachments", postgresql.JSONB(), nullable=False,
                  server_default="[]"),
        sa.Column("external_id", sa.String(), nullable=False, server_default=""),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_sessions.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conv_messages_conv_time", "conversation_messages",
                    ["conversation_id", "created_at"])

    # ── Add conversation_id to agent_sessions ──────────────────────────────────
    op.add_column("agent_sessions",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                  nullable=True))
    op.create_index("ix_agent_sessions_conversation", "agent_sessions",
                    ["conversation_id"])

    # ── Add conversation columns to inbound_events ─────────────────────────────
    op.add_column("inbound_events",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                  nullable=True))
    op.add_column("inbound_events",
        sa.Column("conversation_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversation_messages.id", ondelete="SET NULL"),
                  nullable=True))
    op.create_index("ix_inbound_events_conversation", "inbound_events",
                    ["conversation_id"])

    # ── Backfill: link existing inbound_events into conversations ──────────────
    # We create one Conversation per (connector_id, peer_id="backfill:{id}", thread_key="")
    # for each unlinked inbound_event using the sender field as peer_id.
    # A second pass tries to extract a better peer_id from the JSON payload.
    op.execute("""
        WITH distinct_peers AS (
            SELECT DISTINCT
                connector_id,
                org_id,
                COALESCE(NULLIF(sender, ''), 'unknown') AS peer_id,
                COALESCE(NULLIF(sender, ''), 'unknown') AS peer_name,
                ''::text AS thread_key,
                MIN(received_at) AS created_at,
                MAX(received_at) AS last_inbound_at
            FROM inbound_events
            WHERE conversation_id IS NULL
            GROUP BY connector_id, org_id, sender
        ),
        inserted AS (
            INSERT INTO conversations
                (id, org_id, connector_id, channel, peer_id, peer_name, thread_key,
                 status, summary, created_at, last_inbound_at)
            SELECT
                gen_random_uuid(),
                dp.org_id,
                dp.connector_id,
                c.type::text,
                dp.peer_id,
                dp.peer_name,
                dp.thread_key,
                'open',
                '',
                dp.created_at,
                dp.last_inbound_at
            FROM distinct_peers dp
            JOIN connectors c ON c.id = dp.connector_id
            ON CONFLICT (connector_id, peer_id, thread_key) DO NOTHING
            RETURNING id, connector_id, peer_id
        )
        UPDATE inbound_events ie
        SET conversation_id = ins.id
        FROM inserted ins
        WHERE ie.connector_id = ins.connector_id
          AND COALESCE(NULLIF(ie.sender, ''), 'unknown') = ins.peer_id
          AND ie.conversation_id IS NULL
    """)

    # Create a ConversationMessage for each existing inbound_event that was linked
    op.execute("""
        WITH inserted_msgs AS (
            INSERT INTO conversation_messages
                (id, conversation_id, org_id, direction, author, kind, text,
                 attachments, external_id, created_at)
            SELECT
                gen_random_uuid(),
                ie.conversation_id,
                ie.org_id,
                'inbound',
                'peer',
                'text',
                ie.text,
                '[]'::jsonb,
                ie.external_id,
                ie.received_at
            FROM inbound_events ie
            WHERE ie.conversation_id IS NOT NULL
              AND ie.conversation_message_id IS NULL
            RETURNING id, external_id
        )
        UPDATE inbound_events ie
        SET conversation_message_id = im.id
        FROM inserted_msgs im
        WHERE ie.external_id = im.external_id
          AND ie.conversation_message_id IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_inbound_events_conversation", "inbound_events")
    op.drop_column("inbound_events", "conversation_message_id")
    op.drop_column("inbound_events", "conversation_id")

    op.drop_index("ix_agent_sessions_conversation", "agent_sessions")
    op.drop_column("agent_sessions", "conversation_id")

    op.drop_index("ix_conv_messages_conv_time", "conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_connector_last", "conversations")
    op.drop_table("conversations")

    op.execute("DROP TYPE IF EXISTS messagekind")
    op.execute("DROP TYPE IF EXISTS messageauthor")
    op.execute("DROP TYPE IF EXISTS messagedirection")
    op.execute("DROP TYPE IF EXISTS conversationstatus")
