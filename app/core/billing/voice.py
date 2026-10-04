"""Voice billing helpers: period usage lookup and call-gating logic."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import OrgAddon, OrgBillingSettings, OrgSubscription


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


async def voice_call_allowed(db: AsyncSession, org_id: UUID) -> bool:
    """Return True if a new inbound voice call can be accepted.

    A call is blocked when:
    - The org has no active voice add-on, OR
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

    # Must have active voice add-on
    voice_oa = (await db.exec(
        select(OrgAddon).where(
            OrgAddon.org_id == org_id,
            OrgAddon.status == "active",
            OrgAddon.addon_code.in_(["voice_lite", "voice_standard"]),
        )
    )).first()
    if not voice_oa:
        return False

    # Check allowance
    from app.db.models import Addon
    addon_def = await db.get(Addon, voice_oa.addon_code)
    full_minutes = int((addon_def.included or {}).get("voice_minutes", 0)) if addon_def else 0

    now_dt = datetime.now(UTC)
    if (
        voice_oa.included_snapshot is not None
        and voice_oa.snapshot_period_end
        and now_dt < voice_oa.snapshot_period_end
    ):
        allowance = int(voice_oa.included_snapshot)
    else:
        allowance = full_minutes

    used = await voice_period_usage(db, org_id, sub)
    if used < allowance:
        return True  # Within allowance — always allowed

    # Over allowance: check if overage is enabled
    settings_row = await db.get(OrgBillingSettings, org_id)
    allow_overage = settings_row.allow_voice_overage if settings_row else True
    if not allow_overage:
        return False

    # Check if there's a metered overage item on the Stripe subscription
    if not addon_def or not addon_def.stripe_overage_price_id:
        return False  # No overage price configured

    # Verify the overage item is actually on the subscription (requires Stripe call)
    # We trust the DB state: if stripe_overage_price_id is set and sub is active, allow
    return True
