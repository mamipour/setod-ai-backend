"""Managed-model credit ledger.

All amounts are in USD cents (integers).

Draw order (oldest-expiring first, plan before purchase within same expiry bucket):
  1. Plan grants ('plan' source) — expire at period end; cannot roll over.
  2. Purchased top-ups ('purchase', 'auto_recharge') — expire in 12 months.
  3. Promo grants ('promo') — custom expiry set by staff.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession  # SQLModel's AsyncSession (superset)

from app.db.models import CreditGrant

log = logging.getLogger(__name__)

# Allow a small overdraft (one dollar) so a run near-zero still completes.
OVERDRAFT_TOLERANCE_CENTS = 100


async def balance(db: AsyncSession, org_id: UUID) -> int:
    """Return the current credit balance in USD cents (may be negative if overdraft)."""
    now = datetime.now(UTC)
    result = await db.execute(
        text("""
            SELECT COALESCE(SUM(remaining_cents), 0)
            FROM credit_grants
            WHERE org_id = :org_id
              AND expires_at > :now
              AND remaining_cents > 0
        """),
        {"org_id": str(org_id), "now": now},
    )
    row = result.one()
    return int(row[0]) if row else 0


async def grant(
    db: AsyncSession,
    *,
    org_id: UUID,
    amount_cents: int,
    source: str,
    expires_at: datetime,
    stripe_ref: str | None = None,
) -> CreditGrant:
    """Create a new credit grant and persist it."""
    g = CreditGrant(
        org_id=org_id,
        amount_cents=amount_cents,
        remaining_cents=amount_cents,
        source=source,
        stripe_ref=stripe_ref,
        expires_at=expires_at,
    )
    db.add(g)
    await db.flush()
    log.info("granted %d cents (%s) to org %s; expires %s", amount_cents, source, org_id, expires_at.date())
    return g


async def grant_plan_credit(
    db: AsyncSession,
    org_id: UUID,
    amount_cents: int,
    period_end: datetime,
    stripe_ref: str | None = None,
) -> CreditGrant:
    """Grant a plan credit, expiring any existing un-expired plan grants first.

    Called on checkout.session.completed and invoice.paid.
    Idempotent: if stripe_ref already exists, returns the existing grant.
    """
    from sqlmodel import select

    # Idempotency: skip if this invoice was already processed
    if stripe_ref:
        existing = (await db.exec(
            select(CreditGrant).where(
                CreditGrant.org_id == org_id,
                CreditGrant.stripe_ref == stripe_ref,
                CreditGrant.source == "plan",
            )
        )).first()
        if existing:
            log.info("plan credit already granted for ref %s — skipping", stripe_ref)
            return existing

    # Expire previous plan grants (they don't roll over)
    await db.execute(
        text("""
            UPDATE credit_grants
            SET remaining_cents = 0
            WHERE org_id = :org_id
              AND source = 'plan'
              AND remaining_cents > 0
              AND expires_at > :now
        """),
        {"org_id": str(org_id), "now": datetime.now(UTC)},
    )

    return await grant(
        db,
        org_id=org_id,
        amount_cents=amount_cents,
        source="plan",
        expires_at=period_end,
        stripe_ref=stripe_ref,
    )


async def draw(db: AsyncSession, org_id: UUID, cost_usd: float) -> int:
    """Deduct cost_usd from the org's credit balance (oldest-expiring first).

    Returns the number of cents actually drawn.  If balance is insufficient,
    draws as much as available (partial draw — the caller must check balance
    separately before gating; this function only tracks the accounting).

    Uses a serialized UPDATE to prevent concurrent over-draws.
    """
    if cost_usd <= 0:
        return 0

    cents_to_draw = math.ceil(cost_usd * 100)
    remaining_to_draw = cents_to_draw
    now = datetime.now(UTC)

    # Lock and fetch grants ordered: plan first, then by expiry ascending
    result = await db.execute(
        text("""
            SELECT id, remaining_cents
            FROM credit_grants
            WHERE org_id = :org_id
              AND remaining_cents > 0
              AND expires_at > :now
            ORDER BY
                CASE source WHEN 'plan' THEN 0 ELSE 1 END,
                expires_at ASC
            FOR UPDATE SKIP LOCKED
        """),
        {"org_id": str(org_id), "now": now},
    )
    rows = result.all()

    total_drawn = 0
    for row in rows:
        if remaining_to_draw <= 0:
            break
        grant_id, grant_remaining = row[0], int(row[1])
        deduct = min(grant_remaining, remaining_to_draw)
        await db.execute(
            text("""
                UPDATE credit_grants
                SET remaining_cents = remaining_cents - :deduct
                WHERE id = :id
            """),
            {"deduct": deduct, "id": str(grant_id)},
        )
        remaining_to_draw -= deduct
        total_drawn += deduct

    # Check if we should trigger auto-recharge after the draw
    if total_drawn > 0:
        remaining = await balance(db, org_id)
        await _maybe_auto_recharge(db, org_id, remaining)

    return total_drawn


async def _maybe_auto_recharge(db: AsyncSession, org_id: UUID, current_balance: int) -> None:
    """Trigger an off-session auto-recharge if balance is below threshold."""
    from sqlmodel import select
    from app.db.models import OrgBillingSettings

    settings_row = await db.get(OrgBillingSettings, org_id)
    if not settings_row or not settings_row.auto_recharge_enabled:
        return
    if current_balance >= settings_row.threshold_cents:
        return

    # Avoid recharging if a recent failure exists (back-off: 1 hour)
    if settings_row.auto_recharge_failed_at:
        from datetime import timedelta
        if datetime.now(UTC) - settings_row.auto_recharge_failed_at < timedelta(hours=1):
            log.debug("auto-recharge: skipping due to recent failure for org %s", org_id)
            return

    # Import here to avoid circular import
    import asyncio
    asyncio.create_task(_run_auto_recharge(org_id))


async def _run_auto_recharge(org_id: UUID) -> None:
    """Background task: attempt auto-recharge via Stripe off-session PaymentIntent."""
    try:
        from app.config import settings as _cfg
        if not _cfg.stripe_secret_key:
            return
        import stripe
        stripe.api_key = _cfg.stripe_secret_key
        from app.db.base import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            from app.api.billing.router import _do_auto_recharge
            await _do_auto_recharge(db, stripe, org_id)
    except Exception:
        log.exception("auto-recharge background task failed for org %s", org_id)


async def grant_topup(
    db: AsyncSession,
    org_id: UUID,
    amount_cents: int,
    stripe_ref: str,
    source: str = "purchase",
) -> CreditGrant:
    """Grant a purchased top-up or auto-recharge credit.  Rolls over, expires in 12 months."""
    expires_at = datetime.now(UTC) + timedelta(days=365)
    return await grant(
        db,
        org_id=org_id,
        amount_cents=amount_cents,
        source=source,
        expires_at=expires_at,
        stripe_ref=stripe_ref,
    )
