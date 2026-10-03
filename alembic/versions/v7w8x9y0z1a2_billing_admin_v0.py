"""billing_admin_v0 — is_staff, admin_audit_log, billing tables

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-10-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text as _t
from sqlalchemy.dialects import postgresql

revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users.is_staff ────────────────────────────────────────────────────────
    op.add_column("users", sa.Column("is_staff", sa.Boolean(), nullable=False, server_default="false"))

    # Seed existing Farhad accounts as staff
    op.execute("""
        UPDATE users SET is_staff = true
        WHERE email IN ('mamipour.acc@gmail.com', 'mamipour@gmail.com')
    """)

    # ── admin_audit_log ───────────────────────────────────────────────────────
    op.create_table(
        "admin_audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("staff_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── plans ─────────────────────────────────────────────────────────────────
    op.create_table(
        "plans",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("price_cad_monthly", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_cad_annual", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features", postgresql.JSONB(), nullable=False, server_default=_t("'{}'::jsonb")),
        sa.Column("limits", postgresql.JSONB(), nullable=False, server_default=_t("'{}'::jsonb")),
        sa.Column("included", postgresql.JSONB(), nullable=False, server_default=_t("'{}'::jsonb")),
        sa.Column("stripe_monthly_price_id", sa.Text(), nullable=True),
        sa.Column("stripe_annual_price_id", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── addons ────────────────────────────────────────────────────────────────
    op.create_table(
        "addons",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("price_cad_monthly", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features", postgresql.JSONB(), nullable=False, server_default=_t("'{}'::jsonb")),
        sa.Column("included", postgresql.JSONB(), nullable=False, server_default=_t("'{}'::jsonb")),
        sa.Column("meter", sa.Text(), nullable=True),
        sa.Column("stripe_price_id", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── model_prices ──────────────────────────────────────────────────────────
    op.create_table(
        "model_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_slug", sa.Text(), nullable=False),
        sa.Column("input_per_m", sa.Float(), nullable=False, server_default="0"),
        sa.Column("output_per_m", sa.Float(), nullable=False, server_default="0"),
        sa.Column("audio_in_per_m", sa.Float(), nullable=False, server_default="0"),
        sa.Column("audio_out_per_m", sa.Float(), nullable=False, server_default="0"),
        sa.Column("managed_markup", sa.Float(), nullable=False, server_default="1.5"),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("provider", "model_slug", "active_from", name="uq_model_prices_slot"),
    )

    # ── org_subscriptions ─────────────────────────────────────────────────────
    op.create_table(
        "org_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        sa.Column("plan_code", sa.Text(), sa.ForeignKey("plans.code"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="'active'"),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True, index=True),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True, unique=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_org_subscriptions_org_id", "org_subscriptions", ["org_id"])

    # ── org_addons ────────────────────────────────────────────────────────────
    op.create_table(
        "org_addons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("addon_code", sa.Text(), sa.ForeignKey("addons.code"), nullable=False),
        sa.Column("stripe_subscription_item_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "addon_code", name="uq_org_addons_slot"),
    )

    # ── org_overrides ─────────────────────────────────────────────────────────
    op.create_table(
        "org_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default="''"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── org_caps ──────────────────────────────────────────────────────────────
    op.create_table(
        "org_caps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("meter", sa.Text(), nullable=False),
        sa.Column("hard_cap", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "meter", name="uq_org_caps_meter"),
    )

    # ── usage_events ──────────────────────────────────────────────────────────
    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("meter", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("billable", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── usage_periods ─────────────────────────────────────────────────────────
    op.create_table(
        "usage_periods",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meter", sa.Text(), nullable=False),
        sa.Column("included", sa.Float(), nullable=False, server_default="0"),
        sa.Column("used", sa.Float(), nullable=False, server_default="0"),
        sa.Column("overage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("pushed_to_stripe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "period_start", "meter", name="uq_usage_periods_slot"),
    )

    # ── stripe_events ─────────────────────────────────────────────────────────
    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── Seed catalog ──────────────────────────────────────────────────────────
    import json
    from uuid import uuid4

    # Use text() with bind params to avoid quoting issues with JSONB values
    conn = op.get_bind()

    plans = [
        ("free",     "Free",     0,     0,     {"managed_models": False, "voice": False}, {"agents": 2, "members": 2, "rows": 5000, "model_credits": 0}, {"model_credits": 0},     0),
        ("pro",      "Pro",      4900,  49000, {"managed_models": True,  "voice": False}, {"agents": 10, "members": 10, "rows": 100000, "model_credits": 2000}, {"model_credits": 2000},  1),
        ("business", "Business", 14900, 149000,{"managed_models": True,  "voice": True},  {"agents": -1, "members": -1, "rows": -1, "model_credits": 10000}, {"model_credits": 10000}, 2),
    ]
    for code, name, monthly, annual, features, limits, included, sort in plans:
        conn.execute(
            sa.text(
                "INSERT INTO plans (code, display_name, price_cad_monthly, price_cad_annual, features, limits, included, sort_order)"
                " VALUES (:code, :name, :monthly, :annual, cast(:features as jsonb), cast(:limits as jsonb), cast(:included as jsonb), :sort)"
                " ON CONFLICT DO NOTHING"
            ),
            {"code": code, "name": name, "monthly": monthly, "annual": annual,
             "features": json.dumps(features), "limits": json.dumps(limits),
             "included": json.dumps(included), "sort": sort},
        )

    addons = [
        ("voice_lite",     "Voice Lite (400 min/mo)",        14900, {"voice": True}, {"voice_minutes": 400},  "voice_minutes"),
        ("voice_standard", "Voice Standard (1,000 min/mo)",  29900, {"voice": True}, {"voice_minutes": 1000}, "voice_minutes"),
        ("rows_100k",      "100k Extra Rows",                 1900,  {},              {"rows": 100000},         None),
    ]
    for code, name, price, features, included, meter in addons:
        conn.execute(
            sa.text(
                "INSERT INTO addons (code, display_name, price_cad_monthly, features, included, meter)"
                " VALUES (:code, :name, :price, cast(:features as jsonb), cast(:included as jsonb), :meter)"
                " ON CONFLICT DO NOTHING"
            ),
            {"code": code, "name": name, "price": price,
             "features": json.dumps(features), "included": json.dumps(included), "meter": meter},
        )

    # Seed model prices (USD per million tokens, Oct 2026 pricing)
    prices = [
        ("openai",    "gpt-4.1",           2.0,   8.0,   0.0,  0.0),
        ("openai",    "gpt-4.1-mini",       0.4,   1.6,   0.0,  0.0),
        ("openai",    "gpt-4.1-nano",       0.1,   0.4,   0.0,  0.0),
        ("openai",    "gpt-4o",             2.5,  10.0,   0.0,  0.0),
        ("openai",    "gpt-4o-mini",        0.15,  0.6,   0.0,  0.0),
        ("openai",    "o3",                10.0,  40.0,   0.0,  0.0),
        ("openai",    "o4-mini",            1.1,   4.4,   0.0,  0.0),
        ("openai",    "gpt-realtime-2.1",  32.0,  64.0, 100.0, 200.0),
        ("openai",    "gpt-realtime-mini", 10.0,  20.0,  40.0,  80.0),
        ("anthropic", "claude-sonnet-4-5",  3.0,  15.0,   0.0,  0.0),
        ("anthropic", "claude-opus-4",     15.0,  75.0,   0.0,  0.0),
        ("anthropic", "claude-haiku-4",     0.8,   4.0,   0.0,  0.0),
    ]
    for provider, slug, inp, out, ain, aout in prices:
        uid = str(uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO model_prices (id, provider, model_slug, input_per_m, output_per_m, audio_in_per_m, audio_out_per_m, managed_markup)"
                " VALUES (:id, :provider, :slug, :inp, :out, :ain, :aout, 1.5)"
                " ON CONFLICT ON CONSTRAINT uq_model_prices_slot DO NOTHING"
            ),
            {"id": uid, "provider": provider, "slug": slug, "inp": inp, "out": out, "ain": ain, "aout": aout},
        )

    # ── Create read-only Postgres role for Metabase ───────────────────────────
    # Wrapped in a DO block so it is idempotent.
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'setod_ro') THEN
                CREATE ROLE setod_ro LOGIN PASSWORD 'changeme_setod_ro';
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            GRANT USAGE ON SCHEMA public TO setod_ro;
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO setod_ro;
        EXCEPTION WHEN others THEN
            NULL;  -- role may not exist yet if the DO block above failed
        END $$;
    """)


def downgrade() -> None:
    op.drop_table("stripe_events")
    op.drop_table("usage_periods")
    op.drop_table("usage_events")
    op.drop_table("org_caps")
    op.drop_table("org_overrides")
    op.drop_table("org_addons")
    op.drop_table("org_subscriptions")
    op.drop_table("model_prices")
    op.drop_table("addons")
    op.drop_table("plans")
    op.drop_table("admin_audit_log")
    op.drop_column("users", "is_staff")
