"""processed_item_status

Revision ID: cfe5c0810802
Revises: 3aecfc5dd347
Create Date: 2026-08-26 22:42:52.632478

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.* for SQLModel string columns


# revision identifiers, used by Alembic.
revision: str = 'cfe5c0810802'
down_revision: Union[str, Sequence[str], None] = '3aecfc5dd347'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE TYPE processeditemstatus AS ENUM ('in_flight', 'permanent')")
    op.add_column(
        'agent_processed_items',
        sa.Column(
            'status',
            sa.Enum('in_flight', 'permanent', name='processeditemstatus', create_type=False),
            nullable=False,
            server_default='permanent',  # all existing rows are already confirmed
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agent_processed_items', 'status')
    op.execute("DROP TYPE IF EXISTS processeditemstatus")
