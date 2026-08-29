"""inbound webhook event queue

Revision ID: 737aab89b9a3
Revises: 30ffac9ea954
Create Date: 2026-08-26 13:11:38.767627

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.* for SQLModel string columns
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '737aab89b9a3'
down_revision: Union[str, Sequence[str], None] = '30ffac9ea954'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Queue table for webhook deliveries, drained by the scheduler worker."""
    op.create_table('inbound_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('connector_id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('external_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('text', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('sender', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('pending', 'processed', 'failed', 'ignored', name='inboundeventstatus'), nullable=False),
    sa.Column('error', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['connector_id'], ['connectors.id'], ),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('connector_id', 'external_id', name='uq_inbound_event')
    )
    op.create_index(op.f('ix_inbound_events_connector_id'), 'inbound_events', ['connector_id'], unique=False)
    op.create_index(op.f('ix_inbound_events_external_id'), 'inbound_events', ['external_id'], unique=False)
    op.create_index(op.f('ix_inbound_events_org_id'), 'inbound_events', ['org_id'], unique=False)
    # Partial: the worker only reads pending rows, and processed ones are kept indefinitely
    # for debugging. A full index would grow forever to serve a working set of near zero.
    op.create_index(
        'ix_inbound_events_queue', 'inbound_events', ['received_at'],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index('ix_inbound_events_queue', table_name='inbound_events')
    op.drop_index(op.f('ix_inbound_events_org_id'), table_name='inbound_events')
    op.drop_index(op.f('ix_inbound_events_external_id'), table_name='inbound_events')
    op.drop_index(op.f('ix_inbound_events_connector_id'), table_name='inbound_events')
    op.drop_table('inbound_events')
    # Enums outlive their table in Postgres; leaving it behind makes a re-upgrade fail.
    sa.Enum(name='inboundeventstatus').drop(op.get_bind())
