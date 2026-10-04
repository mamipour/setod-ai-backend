"""billing v1: USD prices, credit_grants, org_billing_settings, stripe_customer_id on orgs

Revision ID: y1z2a3b4c5d6
Revises: x9y0z1a2b3c4
Create Date: 2026-10-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'y1z2a3b4c5d6'
down_revision = 'x9y0z1a2b3c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── organizations: add stripe_customer_id ────────────────────────────────
    op.add_column('organizations', sa.Column('stripe_customer_id', sa.String(), nullable=True))
    op.create_index('ix_organizations_stripe_customer_id', 'organizations', ['stripe_customer_id'])

    # Migrate existing stripe_customer_id values from org_subscriptions to organizations
    op.execute("""
        UPDATE organizations o
        SET stripe_customer_id = s.stripe_customer_id
        FROM org_subscriptions s
        WHERE s.org_id = o.id
          AND s.stripe_customer_id IS NOT NULL
    """)

    # ── plans: rename price_cad_* → price_usd_*, add monthly_credit_cents ───
    op.add_column('plans', sa.Column('price_usd_monthly', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('plans', sa.Column('price_usd_annual', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('plans', sa.Column('monthly_credit_cents', sa.Integer(), nullable=False, server_default='0'))

    # Copy values
    op.execute("UPDATE plans SET price_usd_monthly = price_cad_monthly, price_usd_annual = price_cad_annual")

    op.drop_column('plans', 'price_cad_monthly')
    op.drop_column('plans', 'price_cad_annual')

    # ── addons: rename price_cad_monthly → price_usd_monthly, add sort_order, stripe_overage_price_id ──
    op.add_column('addons', sa.Column('price_usd_monthly', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('addons', sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('addons', sa.Column('stripe_overage_price_id', sa.String(), nullable=True))

    op.execute("UPDATE addons SET price_usd_monthly = price_cad_monthly")
    op.drop_column('addons', 'price_cad_monthly')

    # ── Update plan catalog to new values ────────────────────────────────────
    # Free: 5 agents, 5000 rows, no members limit, no voice, no managed
    op.execute("""
        UPDATE plans SET
            price_usd_monthly = 0,
            price_usd_annual = 0,
            monthly_credit_cents = 0,
            features = '{"managed_models": false, "voice": false}'::jsonb,
            limits = '{"agents": 5, "rows": 5000}'::jsonb,
            included = '{}'::jsonb
        WHERE code = 'free'
    """)
    # Pro: 20 agents, 50k rows, $25 credit, voice via add-on
    op.execute("""
        UPDATE plans SET
            price_usd_monthly = 4900,
            price_usd_annual = 49000,
            monthly_credit_cents = 2500,
            features = '{"managed_models": true, "voice": false}'::jsonb,
            limits = '{"agents": 20, "rows": 50000}'::jsonb,
            included = '{}'::jsonb
        WHERE code = 'pro'
    """)
    # Business: unlimited, $120 credit, voice via add-on
    op.execute("""
        UPDATE plans SET
            price_usd_monthly = 14900,
            price_usd_annual = 149000,
            monthly_credit_cents = 12000,
            features = '{"managed_models": true, "voice": false}'::jsonb,
            limits = '{"agents": -1, "rows": -1}'::jsonb,
            included = '{}'::jsonb
        WHERE code = 'business'
    """)

    # ── Update addon catalog: USD prices, remove voice flag from features ────
    op.execute("""
        UPDATE addons SET
            price_usd_monthly = 14900,
            features = '{"voice": true}'::jsonb,
            sort_order = 1
        WHERE code = 'voice_lite'
    """)
    op.execute("""
        UPDATE addons SET
            price_usd_monthly = 29900,
            features = '{"voice": true}'::jsonb,
            sort_order = 2
        WHERE code = 'voice_standard'
    """)

    # ── credit_grants ─────────────────────────────────────────────────────────
    op.create_table(
        'credit_grants',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('remaining_cents', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=False, server_default='plan'),
        sa.Column('stripe_ref', sa.String(), nullable=True),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    # Indexes on separate statements (table create does not include inline indexes in alembic)
    op.execute('CREATE INDEX IF NOT EXISTS ix_credit_grants_org_id ON credit_grants (org_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_credit_grants_expires_at ON credit_grants (expires_at)')

    # ── org_billing_settings ──────────────────────────────────────────────────
    op.create_table(
        'org_billing_settings',
        sa.Column('org_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('auto_recharge_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('threshold_cents', sa.Integer(), nullable=False, server_default='500'),
        sa.Column('recharge_amount_cents', sa.Integer(), nullable=False, server_default='2500'),
        sa.Column('monthly_cap_cents', sa.Integer(), nullable=False, server_default='20000'),
        sa.Column('auto_recharged_this_month_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('notified_50pct_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notified_80pct_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notified_low_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notified_zero_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('auto_recharge_failed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('org_id'),
    )


def downgrade() -> None:
    op.drop_table('org_billing_settings')
    op.drop_table('credit_grants')
    op.drop_index('ix_organizations_stripe_customer_id', 'organizations')
    op.drop_column('organizations', 'stripe_customer_id')
    op.add_column('plans', sa.Column('price_cad_monthly', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('plans', sa.Column('price_cad_annual', sa.Integer(), nullable=False, server_default='0'))
    op.execute("UPDATE plans SET price_cad_monthly = price_usd_monthly, price_cad_annual = price_usd_annual")
    op.drop_column('plans', 'price_usd_monthly')
    op.drop_column('plans', 'price_usd_annual')
    op.drop_column('plans', 'monthly_credit_cents')
    op.add_column('addons', sa.Column('price_cad_monthly', sa.Integer(), nullable=False, server_default='0'))
    op.execute("UPDATE addons SET price_cad_monthly = price_usd_monthly")
    op.drop_column('addons', 'price_usd_monthly')
    op.drop_column('addons', 'sort_order')
    op.drop_column('addons', 'stripe_overage_price_id')
