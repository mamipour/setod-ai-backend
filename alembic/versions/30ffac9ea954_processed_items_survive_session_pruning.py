"""processed items survive session pruning

Revision ID: 30ffac9ea954
Revises: 7a62fcc8ce68
Create Date: 2026-08-26 12:58:17.780089

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # autogenerate emits sqlmodel.sql.sqltypes.* for SQLModel string columns


# revision identifiers, used by Alembic.
revision: str = '30ffac9ea954'
down_revision: Union[str, Sequence[str], None] = '7a62fcc8ce68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FK = "agent_processed_items_session_id_fkey"


def upgrade() -> None:
    """Let session pruning null out the reference instead of blocking on it.

    The idempotency ledger has to outlive the sessions it points at — a deleted row here
    means the agent re-replies to an email it already answered.
    """
    op.drop_constraint(FK, "agent_processed_items", type_="foreignkey")
    op.create_foreign_key(
        FK, "agent_processed_items", "agent_sessions",
        ["session_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(FK, "agent_processed_items", type_="foreignkey")
    op.create_foreign_key(
        FK, "agent_processed_items", "agent_sessions", ["session_id"], ["id"]
    )
