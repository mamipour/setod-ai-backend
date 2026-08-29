"""add mcp connector type

Revision ID: 8c2d4e6f1a90
Revises: 5f9a1a779ac1
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op

revision: str = "8c2d4e6f1a90"
down_revision: Union[str, Sequence[str], None] = "5f9a1a779ac1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'mcp'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum safely.
    pass
