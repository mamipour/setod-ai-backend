"""assist_message_token_counts

Revision ID: d1e2f3a4b5c6
Revises: c3d4e5f6a7b8
Create Date: 2026-08-31 22:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_assist_messages', sa.Column('prompt_tokens', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('agent_assist_messages', sa.Column('completion_tokens', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('agent_assist_messages', 'completion_tokens')
    op.drop_column('agent_assist_messages', 'prompt_tokens')
