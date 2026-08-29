"""add_owner_notes

Revision ID: a1b2c3d4e5f6
Revises: 8c2d4e6f1a90
Create Date: 2026-08-28 23:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '8c2d4e6f1a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'owner_notes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('agent_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('agent_resolvable', sa.Boolean(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.Uuid(), nullable=True),
        sa.Column('resolution', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_owner_notes_org_id'), 'owner_notes', ['org_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_owner_notes_org_id'), table_name='owner_notes')
    op.drop_table('owner_notes')
