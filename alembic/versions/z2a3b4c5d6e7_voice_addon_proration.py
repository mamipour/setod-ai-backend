"""voice add-on proration, period-end scheduling, metered overage

Revision ID: z2a3b4c5d6e7
Revises: y1z2a3b4c5d6
Create Date: 2026-10-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'z2a3b4c5d6e7'
down_revision = 'y1z2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── org_addons: proration + scheduling fields ───────────────────────────
    op.add_column('org_addons', sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('org_addons', sa.Column('included_snapshot', sa.Float(), nullable=True))
    op.add_column('org_addons', sa.Column('snapshot_period_end', sa.DateTime(timezone=True), nullable=True))
    op.add_column('org_addons', sa.Column('cancel_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('org_addons', sa.Column('pending_addon_code', sa.String(), nullable=True))
    op.add_column('org_addons', sa.Column('overage_reported', sa.Float(), server_default='0', nullable=False))
    op.add_column('org_addons', sa.Column('overage_period_start', sa.DateTime(timezone=True), nullable=True))

    # ── org_subscriptions: store pending plan code so get_plan doesn't need Stripe ──
    op.add_column('org_subscriptions', sa.Column('pending_plan_code', sa.String(), nullable=True))

    # ── org_billing_settings: allow voice overage ───────────────────────────
    op.add_column('org_billing_settings', sa.Column('allow_voice_overage', sa.Boolean(), server_default='true', nullable=False))


def downgrade() -> None:
    op.drop_column('org_billing_settings', 'allow_voice_overage')
    op.drop_column('org_subscriptions', 'pending_plan_code')
    op.drop_column('org_addons', 'overage_period_start')
    op.drop_column('org_addons', 'overage_reported')
    op.drop_column('org_addons', 'pending_addon_code')
    op.drop_column('org_addons', 'cancel_at')
    op.drop_column('org_addons', 'snapshot_period_end')
    op.drop_column('org_addons', 'included_snapshot')
    op.drop_column('org_addons', 'activated_at')
