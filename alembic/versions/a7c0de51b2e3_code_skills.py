"""code skills: user Python functions deployed as Lambda tools

Revision ID: a7c0de51b2e3
Revises: 90f309e31e4d
Create Date: 2026-10-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a7c0de51b2e3"
down_revision = "90f309e31e4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("tagline", sa.String(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_description", sa.String(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("network_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("secrets_enc", sa.Text(), nullable=True),
        sa.Column("deploy_status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("deployed_sha256", sa.String(), nullable=True),
        sa.Column("deployed_network_access", sa.Boolean(), nullable=True),
        sa.Column("deployed_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("lambda_function_name", sa.String(), nullable=True),
        sa.Column("lambda_arn", sa.String(), nullable=True),
        sa.Column("last_deploy_error", sa.Text(), nullable=True),
        sa.Column("last_deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invocation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_invoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "tool_name", name="uq_code_skills_org_tool_name"),
    )
    op.create_index("ix_code_skills_org_id", "code_skills", ["org_id"])

    op.create_table(
        "agent_code_skill_links",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("code_skill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("code_skills.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "code_skill_deploys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code_skill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("code_skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=False),
        sa.Column("network_access", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_code_skill_deploys_code_skill_id", "code_skill_deploys", ["code_skill_id"])
    op.create_index("ix_code_skill_deploys_org_id", "code_skill_deploys", ["org_id"])
    op.create_index("ix_code_skill_deploys_org_started", "code_skill_deploys", ["org_id", "started_at"])

    # Merge into existing plan JSON. Do not replace features/limits/included —
    # those already carry managed_models, voice, and agent limits.
    op.execute("""
        UPDATE plans SET
            features = COALESCE(features, '{}'::jsonb) || '{"code_skills": false}'::jsonb,
            limits = COALESCE(limits, '{}'::jsonb) || '{"code_skills": 0}'::jsonb,
            included = COALESCE(included, '{}'::jsonb) || '{"code_invocations": 0}'::jsonb
        WHERE code = 'free'
    """)
    op.execute("""
        UPDATE plans SET
            features = COALESCE(features, '{}'::jsonb) || '{"code_skills": true}'::jsonb,
            limits = COALESCE(limits, '{}'::jsonb) || '{"code_skills": 5}'::jsonb,
            included = COALESCE(included, '{}'::jsonb) || '{"code_invocations": 2000}'::jsonb
        WHERE code = 'pro'
    """)
    op.execute("""
        UPDATE plans SET
            features = COALESCE(features, '{}'::jsonb) || '{"code_skills": true}'::jsonb,
            limits = COALESCE(limits, '{}'::jsonb) || '{"code_skills": 25}'::jsonb,
            included = COALESCE(included, '{}'::jsonb) || '{"code_invocations": 20000}'::jsonb
        WHERE code = 'business'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE plans SET
            features = features - 'code_skills',
            limits = limits - 'code_skills',
            included = included - 'code_invocations'
    """)
    op.drop_index("ix_code_skill_deploys_org_started", table_name="code_skill_deploys")
    op.drop_index("ix_code_skill_deploys_org_id", table_name="code_skill_deploys")
    op.drop_index("ix_code_skill_deploys_code_skill_id", table_name="code_skill_deploys")
    op.drop_table("code_skill_deploys")
    op.drop_table("agent_code_skill_links")
    op.drop_index("ix_code_skills_org_id", table_name="code_skills")
    op.drop_table("code_skills")
