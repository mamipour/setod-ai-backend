"""Usage metering — record and query billable events.

Meters:
  model_credits  — LLM tokens consumed (1 credit = 1 token for accounting purposes)
  voice_minutes  — completed call minutes, rounded up per call

Records are idempotent on ``idempotency_key``.  Duplicate inserts are silently
dropped so callers can retry safely.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import ModelPrice, UsageEvent, UsagePeriod


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
    """Record one LLM turn as a model_credits event."""
    price = await _get_price(db, provider, model_slug)
    cost = _compute_llm_cost(price, prompt_tokens, completion_tokens, billable, markup=managed)
    total_tokens = prompt_tokens + completion_tokens
    await record_event(
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
                COALESCE(SUM(quantity), 0),
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


# ── Query helpers ─────────────────────────────────────────────────────────────

async def get_org_usage(
    db: AsyncSession,
    org_id: UUID,
    period_start: datetime | None = None,
) -> list[dict]:
    """Return per-meter usage totals for the given org and period (default: current month)."""
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
    return [
        {
            "meter": r.meter,
            "included": r.included,
            "used": r.used,
            "overage": r.overage,
        }
        for r in rows
    ]
