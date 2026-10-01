"""org_tables — agent-native business data layer

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-09-30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ConnectorType enum ────────────────────────────────────────────────────
    # ALTER TYPE … ADD VALUE is not transactional in PostgreSQL, so we must run
    # it outside a transaction block.  Alembic does that automatically when the
    # statement is in execute() rather than inside op.batch_alter_table/etc.
    op.execute("ALTER TYPE connectortype ADD VALUE IF NOT EXISTS 'tables'")

    # ── org_tables ────────────────────────────────────────────────────────────
    op.create_table(
        "org_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("columns", postgresql.JSONB(), nullable=False, server_default="'[]'"),
        sa.Column("unique_on", postgresql.JSONB(), nullable=False, server_default="'[]'"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_org_table_slug", "org_tables", ["org_id", "slug"], unique=True,
                    postgresql_where=sa.text("deleted_at IS NULL"))

    # ── org_table_rows ────────────────────────────────────────────────────────
    op.create_table(
        "org_table_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "table_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("org_tables.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="'{}'"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_org_table_rows_table_deleted", "org_table_rows", ["table_id", "deleted_at"])
    # GIN index for JSONB data queries
    op.execute("CREATE INDEX ix_org_table_rows_data ON org_table_rows USING gin(data)")

    # ── org_table_events ──────────────────────────────────────────────────────
    op.create_table(
        "org_table_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "table_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("org_tables.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("row_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("org_table_events")
    op.execute("DROP INDEX IF EXISTS ix_org_table_rows_data")
    op.drop_index("ix_org_table_rows_table_deleted", table_name="org_table_rows")
    op.drop_table("org_table_rows")
    op.drop_index("ix_org_table_slug", table_name="org_tables")
    op.drop_table("org_tables")
    # NOTE: PostgreSQL does not support removing enum values; the 'tables' value
    # stays in the connectortype enum after downgrade.
