"""agent_status_paused

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-24 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE … ADD VALUE cannot run inside a transaction, so we use
    # execute() which Alembic sends with autocommit semantics when needed.
    op.execute("ALTER TYPE agentstatus ADD VALUE IF NOT EXISTS 'paused'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the type.
    # Safe to leave as-is; 'paused' agents will fail to load, not corrupt data.
    pass
