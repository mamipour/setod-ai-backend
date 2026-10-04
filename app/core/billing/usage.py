"""Usage metering — record and query billable events.

Meters:
  model_credits  — one event per LLM turn.  ``quantity`` is total tokens (for
                   analytics); ``cost_usd`` is the marked-up USD cost and is only
                   non-zero for managed (platform-key) turns.  BYOK turns are
                   recorded with billable=false / cost_usd=0 and never draw credits.
                   Rollups and the usage display sum billable cost_usd, not tokens.
  voice_minutes  — completed call minutes, rounded up per call

Records are idempotent on ``idempotency_key``.  Duplicate inserts are silently
dropped so callers can retry safely.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import ModelPrice, UsageEvent, UsagePeriod

log = logging.getLogger(__name__)


# ── Pricing lookup ────────────────────────────────────────────────────────────

async def _get_price(db: AsyncSession, provider: str, model_slug: str) -> ModelPrice | None:
    """Return the most recent active price row for this provider+model."""
    rows = await db.exec(
        select(ModelPrice)
        .where(ModelPrice.provider == provider, ModelPrice.model_slug == model_slug, ModelPrice.active == True)
        .order_by(ModelPrice.active_from.desc())
        .limit(1)
    )
    return rows.first()


async def has_price(db: AsyncSession, provider: str, model_slug: str) -> bool:
    """True if we can account for this provider+model (an active price row exists)."""
    return await _get_price(db, provider, model_slug) is not None


async def default_managed_model(db: AsyncSession, provider: str) -> str | None:
    """Default slug for managed runs on a provider, or None if nothing is priced.

    Prefers the recommended model (``DEFAULT_MODELS``) when it has an active price row;
    otherwise falls back to the cheapest priced model so a managed run is never unpriced.
    """
    from app.core.llm.client import DEFAULT_MODELS

    preferred = DEFAULT_MODELS.get(provider)
    if preferred and await _get_price(db, provider, preferred) is not None:
        return preferred

    rows = await db.exec(
        select(ModelPrice.model_slug)
        .where(ModelPrice.provider == provider, ModelPrice.active == True)  # noqa: E712
        .order_by(ModelPrice.input_per_m.asc(), ModelPrice.output_per_m.asc())
        .limit(1)
    )
    return rows.first()


def _compute_llm_cost(price: ModelPrice | None, prompt: int, completion: int, billable: bool, markup: bool = False) -> float:
    if not price or not billable:
        return 0.0
    base = (prompt * price.input_per_m + completion * price.output_per_m) / 1_000_000
    if markup:
        base *= price.managed_markup
    return round(base, 8)


# ── Record helpers ────────────────────────────────────────────────────────────

async def record_event(
    db: AsyncSession,
    *,
    org_id: UUID,
    meter: str,
    quantity: float,
    idempotency_key: str,
    billable: bool = True,
    agent_id: UUID | None = None,
    session_id: UUID | None = None,
    cost_usd: float = 0.0,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Insert one usage event.  Returns False if the key already existed (idempotent)."""
    event = UsageEvent(
        id=uuid4(),
        org_id=org_id,
        agent_id=agent_id,
        session_id=session_id,
        meter=meter,
        quantity=quantity,
        cost_usd=cost_usd,
        billable=billable,
        idempotency_key=idempotency_key,
        meta=meta,
    )
    try:
        db.add(event)
        await db.commit()
        return True
    except IntegrityError:
        await db.rollback()
        return False


async def record_llm_usage(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: UUID | None,
    session_id: UUID,
    turn: int,
    prompt_tokens: int,
    completion_tokens: int,
    provider: str,
    model_slug: str,
    billable: bool,
    managed: bool = False,          # True when Setod's platform key was used
) -> None:
    """Record one LLM turn as a model_credits event.

    When managed=True, the cost is drawn from the org's prepaid credit ledger.
    """
    price = await _get_price(db, provider, model_slug)
    if managed and price is None:
        # A managed run on an unpriced model would be free — flag it loudly.
        log.warning("managed LLM usage with no price row: provider=%s model=%s org=%s", provider, model_slug, org_id)
    cost = _compute_llm_cost(price, prompt_tokens, completion_tokens, billable, markup=managed)
    total_tokens = prompt_tokens + completion_tokens

    # Insert the event first; the idempotency key decides whether this turn is new.
    inserted = await record_event(
        db,
        org_id=org_id,
        meter="model_credits",
        quantity=float(total_tokens),
        idempotency_key=f"session:{session_id}:turn:{turn}",
        billable=billable,
        agent_id=agent_id,
        session_id=session_id,
        cost_usd=cost,
        meta={
            "provider": provider,
            "model_slug": model_slug,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "managed": managed,
        },
    )

    # Only draw credits for a newly recorded managed turn — a retry of the same
    # (session, turn) must not deduct twice.
    if inserted and managed and cost > 0:
        from app.core.billing.credits import draw as _draw
        await _draw(db, org_id, cost)
        await db.commit()


async def record_voice_minutes(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: UUID | None,
    session_id: UUID,
    call_sid: str,
    duration_seconds: float,
) -> None:
    """Record one call's voice minutes (ceiling to nearest minute)."""
    minutes = math.ceil(duration_seconds / 60)
    await record_event(
        db,
        org_id=org_id,
        meter="voice_minutes",
        quantity=float(minutes),
        idempotency_key=f"call:{call_sid}:minutes",
        billable=True,
        agent_id=agent_id,
        session_id=session_id,
        meta={"call_sid": call_sid, "duration_seconds": duration_seconds},
    )


# ── Monthly rollup ────────────────────────────────────────────────────────────

async def rollup_usage_periods(db: AsyncSession) -> None:
    """Aggregate usage_events into usage_periods for the current calendar month.

    Called by the worker hourly.  Uses an INSERT … ON CONFLICT DO UPDATE so it
    is idempotent and safe to call multiple times.
    """
    now = datetime.now(UTC)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    await db.exec(
        text("""
            INSERT INTO usage_periods (id, org_id, period_start, meter, included, used, overage, updated_at)
            SELECT
                gen_random_uuid(),
                org_id,
                :period_start,
                meter,
                0,
                -- model_credits is accounted in USD of *billable* (managed) spend;
                -- BYOK tokens are recorded but cost nothing, so they must not inflate it.
                COALESCE(SUM(
                    CASE
                        WHEN meter = 'model_credits' THEN CASE WHEN billable THEN cost_usd ELSE 0 END
                        ELSE quantity
                    END
                ), 0),
                0,
                now()
            FROM usage_events
            WHERE created_at >= :period_start
            GROUP BY org_id, meter
            ON CONFLICT ON CONSTRAINT uq_usage_periods_slot
            DO UPDATE SET used = EXCLUDED.used, updated_at = now()
        """),
        {"period_start": period_start},
    )
    await db.commit()


# ── Stripe overage push ───────────────────────────────────────────────────────

async def push_voice_overage_to_stripe(db: AsyncSession) -> int:
    """Push voice minute overage deltas to Stripe via Billing Meter events.

    Uses an incremental approach: plan-based voice tracks reported overage on
    OrgSubscription. Idempotent event IDs keep retries from double-billing.

    Returns the number of MeterEvent records sent.
    """
    from app.config import settings
    from app.db.models import OrgSubscription, Organization
    if not settings.stripe_secret_key:
        return 0

    import stripe
    stripe.api_key = settings.stripe_secret_key

    # Shared event name from stripe_sync_prices.py
    VOICE_OVERAGE_EVENT_NAME = "voice_overage_minutes"

    pushed = 0

    from sqlmodel import select as _sel
    from app.core.billing.entitlements import resolve as resolve_ent

    subs = (await db.exec(
        _sel(OrgSubscription).where(
            OrgSubscription.status.in_(["active", "trialing", "downgrade_scheduled", "cancel_at_period_end"]),
            OrgSubscription.current_period_start.is_not(None),
        )
    )).all()

    for sub in subs:
        org_id = sub.org_id
        ent = await resolve_ent(db, org_id)
        if not ent.allows("voice"):
            continue
        allowance = float(ent.included_quantity("voice_minutes"))

        # Get Stripe customer ID
        org = await db.get(Organization, org_id)
        customer_id = org.stripe_customer_id if org else None
        if not customer_id:
            continue

        # Compute total voice minutes used in current Stripe billing period
        r = await db.execute(
            text(
                "SELECT COALESCE(SUM(quantity), 0) FROM usage_events "
                "WHERE org_id=:oid AND meter='voice_minutes' AND created_at>=:ps"
            ),
            {"oid": org_id, "ps": sub.current_period_start},
        )
        used = float(r.scalar() or 0)

        overage = max(0.0, used - allowance)

        # Detect period boundary (reset if period changed)
        if sub.voice_overage_period_start != sub.current_period_start:
            sub.voice_overage_reported = 0.0
            sub.voice_overage_period_start = sub.current_period_start
            db.add(sub)

        delta = overage - sub.voice_overage_reported
        if delta <= 0:
            continue

        delta_int = math.ceil(delta)
        # Stable identifier: org + period start + already-reported amount (ensures no double-send)
        period_key = sub.current_period_start.strftime("%Y%m%d%H%M") if sub.current_period_start else "noperiod"
        event_id = f"voice_overage:{org_id}:{period_key}:{int(sub.voice_overage_reported)}"

        try:
            stripe.billing.MeterEvent.create(
                event_name=VOICE_OVERAGE_EVENT_NAME,
                payload={
                    "stripe_customer_id": customer_id,
                    "value": str(delta_int),
                },
                identifier=event_id,
            )
            sub.voice_overage_reported = sub.voice_overage_reported + delta_int
            db.add(sub)
            pushed += 1
            log.info(
                "voice overage: org=%s delta=%d total_reported=%.0f customer=%s",
                org_id, delta_int, sub.voice_overage_reported, customer_id,
            )
        except Exception:
            log.exception("failed to send voice overage meter event for org %s", org_id)

    if pushed:
        await db.commit()
    return pushed


# ── Query helpers ─────────────────────────────────────────────────────────────

async def get_org_usage(
    db: AsyncSession,
    org_id: UUID,
    period_start: datetime | None = None,
) -> list[dict]:
    """Return per-meter usage totals for the given org and period (default: current month).

    For voice_minutes: overrides used/included with live values from usage_events
    so the display is accurate relative to the Stripe billing period (not calendar month).
    """
    if period_start is None:
        now = datetime.now(UTC)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result = await db.exec(
        select(UsagePeriod).where(
            UsagePeriod.org_id == org_id,
            UsagePeriod.period_start == period_start,
        )
    )
    rows = result.all()
    meters = {
        r.meter: {"meter": r.meter, "included": r.included, "used": r.used, "overage": r.overage}
        for r in rows
    }

    # Override voice_minutes with live period data from Stripe billing period
    from sqlmodel import select as _sel
    from app.core.billing.entitlements import resolve as resolve_ent
    from app.db.models import OrgSubscription
    sub = (await db.exec(_sel(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
    if sub and sub.current_period_start:
        r = await db.execute(
            text("SELECT COALESCE(SUM(quantity), 0) FROM usage_events "
                 "WHERE org_id=:oid AND meter='voice_minutes' AND created_at>=:ps"),
            {"oid": org_id, "ps": sub.current_period_start},
        )
        voice_used = float(r.scalar() or 0)

        ent = await resolve_ent(db, org_id)
        allowance = ent.included_quantity("voice_minutes") if ent.allows("voice") else 0.0

        meters["voice_minutes"] = {
            "meter": "voice_minutes",
            "included": allowance,
            "used": voice_used,
            "overage": max(0.0, voice_used - allowance),
        }

    # model_credits: USD drawn from credits this period (managed/billable only).
    # BYOK turns are recorded with cost_usd=0 and billable=false, so they never count.
    credits_period_start = sub.current_period_start if (sub and sub.current_period_start) else period_start
    r = await db.execute(
        text("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events "
             "WHERE org_id=:oid AND meter='model_credits' AND billable AND created_at>=:ps"),
        {"oid": org_id, "ps": credits_period_start},
    )
    credits_used_usd = round(float(r.scalar() or 0), 2)

    credits_included_usd = 0.0
    if sub:
        from app.db.models import Plan
        plan = await db.get(Plan, sub.plan_code)
        if plan and plan.monthly_credit_cents:
            credits_included_usd = plan.monthly_credit_cents / 100

    meters["model_credits"] = {
        "meter": "model_credits",
        "included": credits_included_usd,
        "used": credits_used_usd,
        "overage": max(0.0, credits_used_usd - credits_included_usd) if credits_included_usd > 0 else 0.0,
    }

    return list(meters.values())
