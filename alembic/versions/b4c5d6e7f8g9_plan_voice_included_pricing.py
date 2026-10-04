"""plan voice included pricing

Revision ID: b4c5d6e7f8g9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-04
"""
from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8g9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "org_subscriptions",
        sa.Column("voice_overage_reported", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "org_subscriptions",
        sa.Column("voice_overage_period_start", sa.DateTime(timezone=True), nullable=True),
    )

    # Voice is now included in paid plans. Users pay Twilio directly, so Setod only
    # charges for orchestration/LLM usage and low overage, not a second telecom bill.
    op.execute("""
        UPDATE plans SET
            monthly_credit_cents = 1500,
            features = '{"managed_models": true, "voice": true}'::jsonb,
            included = '{"voice_minutes": 200}'::jsonb
        WHERE code = 'pro'
    """)
    op.execute("""
        UPDATE plans SET
            monthly_credit_cents = 6000,
            features = '{"managed_models": true, "voice": true}'::jsonb,
            included = '{"voice_minutes": 1000}'::jsonb
        WHERE code = 'business'
    """)

    # Keep legacy rows for reconciliation, but hide paid voice packs from catalog.
    op.execute("""
        UPDATE addons SET
            active = false,
            features = jsonb_set(COALESCE(features, '{}'::jsonb), '{overage_price_usd}', '0.04'::jsonb, true)
        WHERE code IN ('voice_lite', 'voice_standard')
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE plans SET
            monthly_credit_cents = 2500,
            features = '{"managed_models": true, "voice": false}'::jsonb,
            included = '{}'::jsonb
        WHERE code = 'pro'
    """)
    op.execute("""
        UPDATE plans SET
            monthly_credit_cents = 12000,
            features = '{"managed_models": true, "voice": false}'::jsonb,
            included = '{}'::jsonb
        WHERE code = 'business'
    """)
    op.execute("""
        UPDATE addons SET active = true
        WHERE code IN ('voice_lite', 'voice_standard')
    """)

    op.drop_column("org_subscriptions", "voice_overage_period_start")
    op.drop_column("org_subscriptions", "voice_overage_reported")
