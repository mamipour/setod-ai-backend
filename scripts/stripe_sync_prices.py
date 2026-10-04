"""Idempotent script: create USD Stripe prices for all plans and add-ons, archive old CAD
prices, and write the new price IDs back to the DB.

Also creates a Stripe Billing Meter for voice overage and a meter-backed metered price.

Usage:
    cd platform
    python scripts/stripe_sync_prices.py

Requires STRIPE_SECRET_KEY and DATABASE_URL in environment (or .env).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

import asyncio
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

from app.db.models import Plan, Addon

# ── Constants ─────────────────────────────────────────────────────────────────

# This event name is shared with app/core/billing/voice.py — keep in sync.
VOICE_OVERAGE_EVENT_NAME = "voice_overage_minutes"

# Metered overage price for voice minutes (per minute, over included quota)
VOICE_OVERAGE_PER_MINUTE_CENTS = 29  # $0.29/min

# ── Product definitions ────────────────────────────────────────────────────────

PLAN_PRODUCTS = [
    {
        "code": "pro",
        "name": "Setod Pro",
        "price_usd_cents": 4900,
        "nickname": "Pro Monthly",
    },
    {
        "code": "business",
        "name": "Setod Business",
        "price_usd_cents": 14900,
        "nickname": "Business Monthly",
    },
]

ADDON_PRODUCTS = [
    {
        "code": "voice_lite",
        "name": "Setod Voice Lite",
        "price_usd_cents": 14900,
        "nickname": "Voice Lite Monthly",
    },
    {
        "code": "voice_standard",
        "name": "Setod Voice Standard",
        "price_usd_cents": 29900,
        "nickname": "Voice Standard Monthly",
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _find_or_create_product(name: str) -> str:
    """Return an existing product ID by exact name or create one."""
    products = stripe.Product.search(query=f'name:"{name}"', limit=5)
    for p in products.data:
        if p.name == name and p.active:
            return p.id
    prod = stripe.Product.create(name=name)
    print(f"  Created product: {prod.id} — {name}")
    return prod.id


def _find_existing_usd_price(product_id: str, amount: int, interval: str = "month") -> str | None:
    """Return an existing active USD recurring price for this product/amount, or None."""
    prices = stripe.Price.list(product=product_id, currency="usd", active=True, limit=20)
    for p in prices.data:
        rec = p.get("recurring") or {}
        if (
            p.unit_amount == amount
            and rec.get("interval") == interval
            and rec.get("usage_type") == "licensed"
        ):
            return p.id
    return None


def _archive_non_usd_prices(product_id: str) -> None:
    """Archive any non-USD prices on a product."""
    prices = stripe.Price.list(product=product_id, active=True, limit=50)
    for p in prices.data:
        if p.currency != "usd":
            stripe.Price.modify(p.id, active=False)
            print(f"  Archived non-USD price: {p.id}")


def _ensure_recurring_price(product_id: str, amount_cents: int, nickname: str) -> str:
    existing = _find_existing_usd_price(product_id, amount_cents)
    if existing:
        print(f"  Reusing USD price: {existing} ({nickname})")
        return existing
    price = stripe.Price.create(
        product=product_id,
        currency="usd",
        unit_amount=amount_cents,
        recurring={"interval": "month"},
        nickname=nickname,
    )
    print(f"  Created USD price: {price.id} ({nickname})")
    return price.id


def _ensure_billing_meter() -> str:
    """Find or create the Stripe Billing Meter for voice overage minutes.

    Returns the meter ID.  The meter is looked up by event_name so running
    this script multiple times is idempotent.
    """
    # List existing meters and find by event_name
    try:
        meters = stripe.billing.Meter.list(limit=20)
        for m in meters.data:
            if m.event_name == VOICE_OVERAGE_EVENT_NAME and m.status == "active":
                print(f"  Reusing Billing Meter: {m.id} ({m.event_name})")
                return m.id
    except Exception as e:
        print(f"  Warning: could not list meters: {e}")

    meter = stripe.billing.Meter.create(
        display_name="Voice Overage Minutes",
        event_name=VOICE_OVERAGE_EVENT_NAME,
        default_aggregation={"formula": "sum"},
        customer_mapping={
            "type": "by_id",
            "event_payload_key": "stripe_customer_id",
        },
        value_settings={
            "event_payload_key": "value",
        },
    )
    print(f"  Created Billing Meter: {meter.id} ({VOICE_OVERAGE_EVENT_NAME})")
    return meter.id


def _ensure_metered_price(product_id: str, meter_id: str, nickname: str) -> str:
    """Find or create a USD metered price backed by a Billing Meter."""
    prices = stripe.Price.list(product=product_id, currency="usd", active=True, limit=20)
    for p in prices.data:
        rec = p.recurring  # Stripe SDK object — use attribute access, not dict.get()
        if rec and getattr(rec, "usage_type", None) == "metered" and getattr(rec, "meter", None) == meter_id:
            print(f"  Reusing metered price: {p.id} ({nickname})")
            return p.id
    price = stripe.Price.create(
        product=product_id,
        currency="usd",
        unit_amount=VOICE_OVERAGE_PER_MINUTE_CENTS,
        recurring={
            "interval": "month",
            "usage_type": "metered",
            "meter": meter_id,
        },
        nickname=nickname,
    )
    print(f"  Created metered price: {price.id} ({nickname})")
    return price.id


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with AsyncSessionLocal() as db:
        # ── Plan prices ──────────────────────────────────────────────────────
        for spec in PLAN_PRODUCTS:
            print(f"\n[{spec['code']}]")
            prod_id = _find_or_create_product(spec["name"])
            _archive_non_usd_prices(prod_id)
            price_id = _ensure_recurring_price(prod_id, spec["price_usd_cents"], spec["nickname"])

            plan = await db.get(Plan, spec["code"])
            if plan:
                plan.stripe_monthly_price_id = price_id
                db.add(plan)
                print(f"  DB updated: plans.{spec['code']}.stripe_monthly_price_id = {price_id}")

        # ── Billing Meter (shared across both voice add-ons) ─────────────────
        print("\n[voice_overage_meter]")
        meter_id = _ensure_billing_meter()

        # ── Add-on prices ────────────────────────────────────────────────────
        voice_overage_product_id = _find_or_create_product("Setod Voice Overage")

        for spec in ADDON_PRODUCTS:
            print(f"\n[{spec['code']}]")
            prod_id = _find_or_create_product(spec["name"])
            _archive_non_usd_prices(prod_id)
            price_id = _ensure_recurring_price(prod_id, spec["price_usd_cents"], spec["nickname"])
            overage_price_id = _ensure_metered_price(
                voice_overage_product_id,
                meter_id,
                "Voice Overage per minute",
            )

            addon = await db.get(Addon, spec["code"])
            if addon:
                addon.stripe_price_id = price_id
                addon.stripe_overage_price_id = overage_price_id
                db.add(addon)
                print(f"  DB updated: addons.{spec['code']}.stripe_price_id = {price_id}")
                print(f"  DB updated: addons.{spec['code']}.stripe_overage_price_id = {overage_price_id}")

        await db.commit()
        print("\nDone. All USD prices synced.")


if __name__ == "__main__":
    asyncio.run(main())
