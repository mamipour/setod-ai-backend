"""agent_calls_agent

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-29 00:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new TriggerType value — safe: value not used within this migration.
    op.execute("ALTER TYPE triggertype ADD VALUE IF NOT EXISTS 'agent'")

    # agent_links: grants a caller agent the ability to invoke a target agent as a tool.
    op.create_table(
        'agent_links',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('target_agent_id', sa.UUID(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_agent_id'], ['agents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'target_agent_id', name='uq_agent_link'),
    )
    op.create_index(op.f('ix_agent_links_agent_id'), 'agent_links', ['agent_id'], unique=False)

    # triggered_by_session_id on agent_sessions: links a child run back to the caller's session.
    op.add_column(
        'agent_sessions',
        sa.Column('triggered_by_session_id', sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        'fk_agent_sessions_triggered_by',
        'agent_sessions', 'agent_sessions',
        ['triggered_by_session_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_agent_sessions_triggered_by_session_id',
        'agent_sessions', ['triggered_by_session_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_agent_sessions_triggered_by_session_id', table_name='agent_sessions')
    op.drop_constraint('fk_agent_sessions_triggered_by', 'agent_sessions', type_='foreignkey')
    op.drop_column('agent_sessions', 'triggered_by_session_id')
    op.drop_index(op.f('ix_agent_links_agent_id'), table_name='agent_links')
    op.drop_table('agent_links')
    # Note: Postgres does not support removing enum values; TriggerType.agent stays in the DB.
