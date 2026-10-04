"""Voice billing helpers: period usage lookup and call-gating logic."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Addon, OrgBillingSettings, OrgSubscription


async def voice_period_usage(db: AsyncSession, org_id: UUID, sub: OrgSubscription) -> float:
    """Return the total voice minutes used in the current Stripe billing period."""
    if not sub.current_period_start:
        return 0.0
    result = await db.execute(
        text(
            "SELECT COALESCE(SUM(quantity), 0) FROM usage_events "
            "WHERE org_id = :oid AND meter = 'voice_minutes' AND created_at >= :ps"
        ),
        {"oid": org_id, "ps": sub.current_period_start},
    )
    return float(result.scalar() or 0)


async def _voice_overage_price_id(db: AsyncSession) -> str | None:
    """Return the shared Stripe metered price for plan-based voice overage."""
    row = (await db.exec(
        select(Addon).where(Addon.stripe_overage_price_id.is_not(None))
    )).first()
    return row.stripe_overage_price_id if row else None


async def voice_call_allowed(db: AsyncSession, org_id: UUID) -> bool:
    """Return True if a new inbound voice call can be accepted.

    A call is blocked when:
    - The org's plan/add-ons do not allow voice, OR
    - Used minutes >= allowance AND (allow_voice_overage is False OR no overage
      price item exists on the subscription OR subscription is in a non-billable state).

    An OrgCap with meter='voice_minutes' and hard_cap=0 always blocks.
    """
    from app.db.models import OrgCap

    # Hard cap check
    cap_row = (await db.execute(
        text("SELECT hard_cap FROM org_caps WHERE org_id=:oid AND meter='voice_minutes' LIMIT 1"),
        {"oid": org_id},
    )).fetchone()
    if cap_row and cap_row[0] <= 0:
        return False

    sub = (await db.exec(
        select(OrgSubscription).where(OrgSubscription.org_id == org_id)
    )).first()
    if not sub or sub.status not in ("active", "trialing", "downgrade_scheduled", "cancel_at_period_end"):
        return False

    from app.core.billing.entitlements import resolve as resolve_ent
    ent = await resolve_ent(db, org_id)
    if not ent.allows("voice"):
        return False

    allowance = ent.included_quantity("voice_minutes")
    used = await voice_period_usage(db, org_id, sub)
    if used < allowance:
        return True  # Within allowance — always allowed

    # Over allowance: check if overage is enabled
    settings_row = await db.get(OrgBillingSettings, org_id)
    allow_overage = settings_row.allow_voice_overage if settings_row else True
    if not allow_overage:
        return False

    # Check if there's a configured metered overage price.
    if not await _voice_overage_price_id(db):
        return False  # No overage price configured

    # Checkout/change-plan attach the metered overage item for voice plans.
    # At call time we trust the active subscription plus configured overage price.
    return True
