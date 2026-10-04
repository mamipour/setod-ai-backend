"""Billing API — Stripe Checkout/Portal, usage queries, webhook ingestion."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user, require_owner
from app.config import settings
from app.core.billing.entitlements import resolve as resolve_ent
from app.core.billing.usage import get_org_usage, rollup_usage_periods
from app.db.models import Addon, Agent, CreditGrant, OrgAddon, OrgBillingSettings, OrgSubscription, OrgTableRow, Organization, Plan, StripeEvent, User
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


async def _get_or_create_stripe_customer(db: AsyncSession, stripe_mod, org_id: UUID) -> str:
    """Return the Stripe customer ID for an org, creating one if it doesn't exist.

    stripe_customer_id is stored on Organization (canonical) and mirrored on
    OrgSubscription for convenience.  This helper always reads from / writes to
    Organization so that flushing the subscription row never orphans the customer.
    """
    from sqlmodel import select
    org = await db.get(Organization, org_id)
    if org and org.stripe_customer_id:
        return org.stripe_customer_id

    # Look up by email to avoid creating a second customer for the same address
    from app.db.models import User as _User, OrganizationMember as _OM
    owner_row = (await db.exec(
        select(_User)
        .join(_OM, _OM.user_id == _User.id)
        .where(_OM.organization_id == org_id, _OM.role == "owner")
        .limit(1)
    )).first()
    email = owner_row.email if owner_row else None

    customers = stripe_mod.Customer.search(query=f'email:"{email}"', limit=5) if email else None
    customer_id: str | None = None
    if customers:
        for c in customers.data:
            cd = c._to_dict_recursive() if hasattr(c, "_to_dict_recursive") else dict(c)
            if cd.get("email") == email:
                customer_id = cd["id"]
                break

    if not customer_id:
        cust = stripe_mod.Customer.create(email=email, metadata={"org_id": str(org_id)})
        customer_id = cust.id
        log.info("created Stripe customer %s for org %s", customer_id, org_id)

    if org:
        org.stripe_customer_id = customer_id
        db.add(org)
        await db.commit()

    return customer_id


# ── Subscription item helpers ─────────────────────────────────────────────────

def _resolve_addon_price_for_currency(stripe_mod, addon: "Addon", sub_currency: str) -> str:
    """Return a Stripe price ID for this add-on in the subscription's currency.

    If the subscription is in USD we return addon.stripe_price_id directly.
    Otherwise we look for an existing active price on the same product in that
    currency, or create one with the same nominal unit_amount (same number,
    different currency — typical SaaS pricing parity approach).
    """
    if not sub_currency or sub_currency.lower() == "usd":
        return addon.stripe_price_id

    # Need a price in a non-USD currency
    try:
        base_price = stripe_mod.Price.retrieve(addon.stripe_price_id)
        product_id = base_price.product
        unit_amount = addon.price_usd_monthly  # same nominal amount in local currency

        # Search for an existing matching price
        prices = stripe_mod.Price.list(product=product_id, currency=sub_currency.lower(), active=True, limit=20)
        for p in prices.data:
            rec = p.recurring
            if (
                p.unit_amount == unit_amount
                and rec is not None
                and getattr(rec, "interval", None) == "month"
                and getattr(rec, "usage_type", "licensed") == "licensed"
            ):
                return p.id

        # Create a new price in the subscription's currency
        new_price = stripe_mod.Price.create(
            product=product_id,
            currency=sub_currency.lower(),
            unit_amount=unit_amount,
            recurring={"interval": "month"},
            nickname=f"{addon.code} Monthly ({sub_currency.upper()})",
        )
        log.info("Created %s price for addon %s in currency %s: %s", sub_currency, addon.code, sub_currency, new_price.id)
        return new_price.id
    except Exception:
        log.exception("Could not resolve price in currency %s for addon %s, falling back to USD price", sub_currency, addon.code)
        return addon.stripe_price_id


async def _classify_items(db: AsyncSession, items: list[dict]) -> dict:
    """Classify Stripe subscription items by type.

    Returns:
        {
          "plan_item": item_dict | None,
          "voice_item": item_dict | None,   # active voice add-on item
          "overage_item": item_dict | None, # metered overage item
          "other": [...]
        }
    """
    from sqlmodel import select as _sel
    plans = (await db.exec(_sel(Plan).where(Plan.active == True))).all()
    addons = (await db.exec(_sel(Addon))).all()

    plan_price_ids = {p.stripe_monthly_price_id for p in plans if p.stripe_monthly_price_id}
    voice_price_ids = {a.stripe_price_id for a in addons if a.stripe_price_id and a.code in ("voice_lite", "voice_standard")}
    overage_price_ids = {a.stripe_overage_price_id for a in addons if a.stripe_overage_price_id}

    result: dict = {"plan_item": None, "voice_item": None, "overage_item": None, "other": []}
    for item in items:
        pid = item.get("price", {}).get("id")
        if pid in plan_price_ids:
            result["plan_item"] = item
        elif pid in voice_price_ids:
            result["voice_item"] = item
        elif pid in overage_price_ids:
            result["overage_item"] = item
        else:
            result["other"].append(item)
    return result


async def _voice_price_to_addon(db: AsyncSession, price_id: str) -> "Addon | None":
    """Return the Addon whose stripe_price_id matches price_id."""
    from sqlmodel import select as _sel
    return (await db.exec(
        _sel(Addon).where(Addon.stripe_price_id == price_id, Addon.active == True)
    )).first()


async def _voice_overage_price_id(db: AsyncSession) -> str | None:
    """Return the shared Stripe metered price for voice overage."""
    from sqlmodel import select as _sel
    addon = (await db.exec(
        _sel(Addon).where(Addon.stripe_overage_price_id.is_not(None))
    )).first()
    return addon.stripe_overage_price_id if addon else None


async def _compute_prorated_minutes(total_minutes: int, period_start: datetime, period_end: datetime) -> int:
    """Return the prorated minutes for the remainder of the billing period (ceiling)."""
    import math
    now = datetime.now(UTC)
    period_secs = (period_end - period_start).total_seconds()
    remaining_secs = max(0, (period_end - now).total_seconds())
    if period_secs <= 0:
        return total_minutes
    fraction = remaining_secs / period_secs
    return math.ceil(total_minutes * fraction)


async def _build_phase2_items(db: AsyncSession, org_id: UUID, sub: "OrgSubscription", classified: dict) -> list[dict]:
    """Build the item list for phase 2 of a subscription schedule (next period).

    Includes:
    - Plan price (pending_plan_code if set, otherwise current plan_code)
    - Voice add-on price (active non-cancelled addons, or pending_addon_code)
    - Overage metered price (if any voice add-on will remain)
    """
    from sqlmodel import select as _sel

    # Plan price
    target_plan_code = sub.pending_plan_code or sub.plan_code
    target_plan = await db.get(Plan, target_plan_code)
    items: list[dict] = []
    if target_plan and target_plan.stripe_monthly_price_id:
        items.append({"price": target_plan.stripe_monthly_price_id, "quantity": 1})

    # Voice add-on(s)
    addon_rows = (await db.exec(
        _sel(OrgAddon).where(
            OrgAddon.org_id == org_id,
            OrgAddon.status == "active",
            OrgAddon.addon_code.in_(["voice_lite", "voice_standard"]),
        )
    )).all()

    voice_in_phase2 = False
    for oa in addon_rows:
        if oa.cancel_at:
            # This addon is scheduled for removal — check if it has a swap target
            if oa.pending_addon_code:
                swap_addon = await db.get(Addon, oa.pending_addon_code)
                if swap_addon and swap_addon.stripe_price_id:
                    items.append({"price": swap_addon.stripe_price_id, "quantity": 1})
                    voice_in_phase2 = True
            # else: no swap target, drops entirely
        else:
            addon = await db.get(Addon, oa.addon_code)
            if addon and addon.stripe_price_id:
                items.append({"price": addon.stripe_price_id, "quantity": 1})
                voice_in_phase2 = True

    # Overage item if the next-period plan/add-on set includes voice.
    target_has_plan_voice = bool((target_plan.features or {}).get("voice")) if target_plan else False
    if voice_in_phase2 or target_has_plan_voice:
        overage_price_id = await _voice_overage_price_id(db)
        if overage_price_id:
            items.append({"price": overage_price_id})

    return items


async def _sync_period_end_schedule(
    stripe_mod, sub: "OrgSubscription", stripe_sub: dict, phase2_items: list[dict]
) -> None:
    """Create or update a subscription schedule so phase 2 = phase2_items at period end.

    Phase 1 = ALL current subscription items (preserves add-ons during plan downgrades).
    If phase2_items equals current items, releases any existing schedule.
    """
    items = stripe_sub.get("items", {}).get("data", [])
    current_prices = sorted(i.get("price", {}).get("id", "") for i in items)
    next_prices = sorted(i.get("price", "") for i in phase2_items)

    schedule_id = stripe_sub.get("schedule")

    if current_prices == next_prices:
        if schedule_id:
            stripe_mod.SubscriptionSchedule.release(schedule_id)
        return

    # Phase 1 = all current items
    phase1_items = [{"price": i.get("price", {}).get("id"), "quantity": i.get("quantity", 1)} for i in items]
    period_end_ts = stripe_sub.get("current_period_end")

    if schedule_id:
        _raw = stripe_mod.SubscriptionSchedule.retrieve(schedule_id)
        sched = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
        phases = sched.get("phases", [])
        phase1_start = phases[0].get("start_date") if phases else None

        stripe_mod.SubscriptionSchedule.modify(
            schedule_id,
            phases=[
                {"items": phase1_items, "start_date": phase1_start, "end_date": period_end_ts},
                {"items": phase2_items, "start_date": period_end_ts},
            ],
            end_behavior="release",
        )
    else:
        _raw = stripe_mod.SubscriptionSchedule.create(from_subscription=sub.stripe_subscription_id)
        sched = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
        phase1_start = sched.get("phases", [{}])[0].get("start_date")

        stripe_mod.SubscriptionSchedule.modify(
            sched["id"],
            phases=[
                {"items": phase1_items, "start_date": phase1_start, "end_date": period_end_ts},
                {"items": phase2_items, "start_date": period_end_ts},
            ],
            end_behavior="release",
        )


# ── Catalog ───────────────────────────────────────────────────────────────────

@router.get("/catalog")
async def get_catalog(
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Public plan + add-on catalog. No auth required (used by plan page)."""
    from sqlmodel import select
    plans = (await db.exec(select(Plan).where(Plan.active == True).order_by(Plan.sort_order))).all()
    addons = (await db.exec(select(Addon).where(Addon.active == True).order_by(Addon.sort_order))).all()
    return {
        "plans": [
            {
                "code": p.code,
                "display_name": p.display_name,
                "price_usd_monthly": p.price_usd_monthly,
                "price_usd_annual": p.price_usd_annual,
                "monthly_credit_cents": p.monthly_credit_cents,
                "max_agents": (p.limits or {}).get("agents", 5),
                "max_rows": (p.limits or {}).get("rows", 5000),
                "features": p.features,
                "limits": p.limits,
                "included": p.included,
                "sort_order": p.sort_order,
            }
            for p in plans
        ],
        "addons": [
            {
                "code": a.code,
                "display_name": a.display_name,
                "price_usd_monthly": a.price_usd_monthly,
                "included_minutes": (a.included or {}).get("voice_minutes", 0),
                # overage_price_per_unit in cents (4 cents = $0.04/min)
                # Use round() not int() to avoid float truncation.
                "overage_price_per_unit": round(((a.features or {}).get("overage_price_usd", 0.04)) * 100),
                "features": a.features,
                "sort_order": a.sort_order,
            }
            for a in addons
        ],
    }


@router.get("/{org_id}/downgrade-impact")
async def downgrade_impact(
    org_id: UUID,
    target_plan: str,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Return items that would exceed limits if the org downgrades to target_plan.

    Response shape:
      {
        "agents": {"current": 12, "limit": 5, "over": 7},  # or null if OK
        "rows":   {"current": 80000, "limit": 50000, "over": 30000},  # or null if OK
      }
    """
    from sqlmodel import select, func
    from app.db.models import Agent, OrgTableRow

    plan = await db.get(Plan, target_plan)
    if not plan:
        raise HTTPException(404, f"Plan '{target_plan}' not found")

    limits = plan.limits or {}
    agent_limit = int(limits.get("agents", 5))
    row_limit = int(limits.get("rows", 5000))

    # Current counts
    agent_count = (await db.exec(
        select(func.count()).where(Agent.org_id == org_id, Agent.status != "deleted")
    )).one()

    row_count = (await db.exec(
        select(func.count()).where(OrgTableRow.org_id == org_id, OrgTableRow.deleted_at.is_(None))
    )).one()

    return {
        "agents": {
            "current": int(agent_count),
            "limit": agent_limit,
            "over": max(0, int(agent_count) - agent_limit),
        } if agent_limit != -1 and int(agent_count) > agent_limit else None,
        "rows": {
            "current": int(row_count),
            "limit": row_limit,
            "over": max(0, int(row_count) - row_limit),
        } if row_limit != -1 and int(row_count) > row_limit else None,
    }


# ── Usage query ───────────────────────────────────────────────────────────────

@router.get("/{org_id}/usage")
async def get_usage(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
    year: int | None = None,
    month: int | None = None,
):
    if year and month:
        period_start = datetime(year, month, 1, tzinfo=UTC)
    else:
        now = datetime.now(UTC)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return await get_org_usage(db, org_id, period_start)


@router.get("/{org_id}/plan")
async def get_plan(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    ent = await resolve_ent(db, org_id)
    sub = (await db.exec(
        __import__("sqlmodel", fromlist=["select"]).select(OrgSubscription)
        .where(OrgSubscription.org_id == org_id)
    )).first()
    plan = await db.get(Plan, ent.plan_code)
    # Credit balance
    from app.core.billing.credits import balance as credit_balance
    from sqlmodel import select as _select
    balance_cents = await credit_balance(db, org_id)
    monthly_credit_cents = plan.monthly_credit_cents if plan else 0

    # Active add-on codes with detail
    active_addon_rows = (await db.exec(
        _select(OrgAddon).where(
            OrgAddon.org_id == org_id,
            OrgAddon.status == "active",
        )
    )).all()

    # Voice usage this period (for allowance display)
    voice_used: float = 0.0
    if sub and sub.current_period_start:
        from sqlalchemy import text as _text
        _r = await db.execute(
            _text("SELECT COALESCE(SUM(quantity), 0) FROM usage_events "
                  "WHERE org_id=:oid AND meter='voice_minutes' AND created_at>=:ps"),
            {"oid": org_id, "ps": sub.current_period_start},
        )
        voice_used = float(_r.scalar() or 0)

    active_addons = []
    for oa in active_addon_rows:
        addon_def = await db.get(Addon, oa.addon_code)
        full_minutes = int((addon_def.included or {}).get("voice_minutes", 0)) if addon_def else 0
        now_dt = datetime.now(UTC)
        # Allowance this period: snapshot if mid-period activation, else full
        if oa.included_snapshot is not None and oa.snapshot_period_end and now_dt < oa.snapshot_period_end:
            allowance = int(oa.included_snapshot)
        else:
            allowance = full_minutes
        active_addons.append({
            "code": oa.addon_code,
            "status": oa.status,
            "cancel_at": oa.cancel_at.isoformat() if oa.cancel_at else None,
            "pending_addon_code": oa.pending_addon_code,
            "allowance_minutes": allowance,
            "used_minutes": int(voice_used),
        })

    # pending_plan_code from DB (no Stripe round-trip needed)
    pending_plan_code = sub.pending_plan_code if sub else None

    return {
        "plan_code": ent.plan_code,
        "plan_name": plan.display_name if plan else ent.plan_code,
        "features": ent.features,
        "limits": ent.limits,
        "included": ent.included,
        "monthly_credit_cents": monthly_credit_cents,
        "credit_balance_cents": balance_cents,
        "active_addons": active_addons,
        "subscription": {
            "status": sub.status if sub else None,
            "stripe_customer_id": sub.stripe_customer_id if sub else None,
            "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
            "pending_plan_code": pending_plan_code,
        } if sub else None,
    }


# ── Stripe Checkout / Portal ───────────────────────────────────────────────────

@router.post("/{org_id}/checkout")
async def create_checkout(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Create a Stripe Checkout session for new subscribers (no existing subscription)."""
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    plan_code = body.get("plan_code", "pro")
    plan = await db.get(Plan, plan_code)
    if not plan or not plan.stripe_monthly_price_id:
        raise HTTPException(400, f"Plan '{plan_code}' has no Stripe price configured")

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()

    # If already subscribed, do an in-place subscription modification instead
    if sub and sub.stripe_subscription_id and sub.status in ("active", "trialing", "downgrade_scheduled"):
        return await _change_plan_inline(db, stripe, sub, plan, plan_code)

    customer_id = await _get_or_create_stripe_customer(db, stripe, org_id)
    line_items = [{"price": plan.stripe_monthly_price_id, "quantity": 1}]
    if (plan.features or {}).get("voice"):
        overage_price_id = await _voice_overage_price_id(db)
        if overage_price_id:
            line_items.append({"price": overage_price_id})

    session_params: dict = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": line_items,
        "success_url": f"{settings.frontend_origin}/settings/plan?checkout=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?checkout=cancel",
        "metadata": {"org_id": str(org_id), "plan_code": plan_code},
    }
    checkout = stripe.checkout.Session.create(**session_params)
    return {"url": checkout.url}


@router.post("/{org_id}/change-plan")
async def change_plan(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Upgrade or downgrade an existing subscription in-place (no Stripe redirect needed).
    For new subscribers, falls back to a Checkout session URL.
    Returns either {"status": "ok"} for in-place change or {"url": "..."} for new checkout.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    plan_code = body.get("plan_code")
    if not plan_code:
        raise HTTPException(400, "plan_code required")

    plan = await db.get(Plan, plan_code)
    if not plan or not plan.stripe_monthly_price_id:
        raise HTTPException(400, f"Plan '{plan_code}' has no Stripe price configured")

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()

    if sub and sub.stripe_subscription_id and sub.status in ("active", "trialing", "downgrade_scheduled"):
        return await _change_plan_inline(db, stripe, sub, plan, plan_code)

    # No existing sub — return a checkout URL
    customer_id = await _get_or_create_stripe_customer(db, stripe, org_id)
    line_items = [{"price": plan.stripe_monthly_price_id, "quantity": 1}]
    if (plan.features or {}).get("voice"):
        overage_price_id = await _voice_overage_price_id(db)
        if overage_price_id:
            line_items.append({"price": overage_price_id})

    session_params: dict = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": line_items,
        "success_url": f"{settings.frontend_origin}/settings/plan?checkout=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?checkout=cancel",
        "metadata": {"org_id": str(org_id), "plan_code": plan_code},
    }
    checkout = stripe.checkout.Session.create(**session_params)
    return {"url": checkout.url}


async def _change_plan_inline(db: AsyncSession, stripe_mod, sub: OrgSubscription, plan, plan_code: str) -> dict:
    """Upgrade or schedule a downgrade for an existing Stripe subscription.

    • Upgrade  (higher sort_order) → immediate with proration.
    • Downgrade (lower sort_order) → scheduled at period end via subscription schedule.
    """
    # Determine direction: compare plan sort_order
    current_plan = await db.get(Plan, sub.plan_code)
    current_rank = current_plan.sort_order if current_plan else 0
    target_rank = plan.sort_order if plan else 0

    # Retrieve current Stripe subscription
    _raw = stripe_mod.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
    stripe_sub: dict = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
    items = stripe_sub.get("items", {}).get("data", [])
    if not items:
        raise HTTPException(500, "Stripe subscription has no line items")

    classified = await _classify_items(db, items)
    plan_item = classified["plan_item"] or items[0]
    item_id = plan_item["id"]
    current_price_id = plan_item.get("price", {}).get("id", "")

    # No-op if already on this plan
    if current_price_id == plan.stripe_monthly_price_id:
        return {"status": "ok", "message": "Already on this plan"}

    try:
        if target_rank > current_rank:
            # ── Upgrade: immediate switch with prorated charge ──
            update_items = [{"id": item_id, "price": plan.stripe_monthly_price_id}]
            if (plan.features or {}).get("voice") and not classified["overage_item"]:
                overage_price_id = await _voice_overage_price_id(db)
                if overage_price_id:
                    update_items.append({"price": overage_price_id})
            _upd = stripe_mod.Subscription.modify(
                sub.stripe_subscription_id,
                items=update_items,
                proration_behavior="create_prorations",
                metadata={"plan_code": plan_code},
            )
            updated: dict = _upd._to_dict_recursive() if hasattr(_upd, "_to_dict_recursive") else dict(_upd)

            sub.plan_code = plan_code
            sub.status = updated.get("status", "active")
            if updated.get("current_period_start"):
                sub.current_period_start = datetime.fromtimestamp(updated["current_period_start"], tz=UTC)
            if updated.get("current_period_end"):
                sub.current_period_end = datetime.fromtimestamp(updated["current_period_end"], tz=UTC)
            db.add(sub)
            await db.commit()
            log.info("upgrade: sub=%s → plan=%s (immediate)", sub.stripe_subscription_id, plan_code)
            return {"status": "ok", "effective": "immediate"}

        else:
            # ── Downgrade: schedule the new plan for next billing period ──
            # Build desired phase-2 items (new plan + retained add-ons)
            sub.pending_plan_code = plan_code  # set temporarily so _build_phase2_items uses it
            phase2_items = await _build_phase2_items(db, sub.org_id, sub, classified)

            await _sync_period_end_schedule(stripe_mod, sub, stripe_sub, phase2_items)

            # Mark pending downgrade in our DB
            sub.status = "downgrade_scheduled"
            sub.pending_plan_code = plan_code
            current_end = stripe_sub.get("current_period_end")
            if current_end:
                sub.current_period_end = datetime.fromtimestamp(current_end, tz=UTC)
            db.add(sub)
            await db.commit()

            effective_date = datetime.fromtimestamp(current_end, tz=UTC).strftime("%Y-%m-%d") if current_end else "next billing cycle"
            log.info("downgrade scheduled: sub=%s → plan=%s on %s", sub.stripe_subscription_id, plan_code, effective_date)
            return {"status": "ok", "effective": "end_of_period", "effective_date": effective_date}

    except stripe_mod.error.InvalidRequestError as e:
        log.error("Stripe plan change error: %s", e.user_message)
        raise HTTPException(400, f"Stripe error: {e.user_message}")


@router.post("/{org_id}/reactivate")
async def reactivate(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Undo a scheduled cancellation (cancel_at_period_end → active)."""
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(400, "No subscription found")
    if sub.status not in ("cancel_at_period_end", "downgrade_scheduled"):
        raise HTTPException(400, "Subscription is not scheduled for cancellation or downgrade")

    try:
        if sub.status == "downgrade_scheduled":
            # Release the schedule — subscription reverts to normal renewal
            _raw = stripe.Subscription.retrieve(sub.stripe_subscription_id)
            stripe_sub = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
            schedule_id = stripe_sub.get("schedule")
            if schedule_id:
                stripe.SubscriptionSchedule.release(schedule_id)
        else:
            # Undo cancellation — clear both the period-end flag and any portal-set cancel_at timestamp
            _raw = stripe.Subscription.retrieve(sub.stripe_subscription_id)
            stripe_sub = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
            params: dict = {}
            if stripe_sub.get("cancel_at_period_end"):
                params["cancel_at_period_end"] = False
            if stripe_sub.get("cancel_at"):
                params["cancel_at"] = ""  # empty string clears the field
            if params:
                stripe.Subscription.modify(sub.stripe_subscription_id, **params)
    except stripe.error.InvalidRequestError as e:
        log.error("Stripe reactivate error: %s", e.user_message)
        raise HTTPException(400, f"Stripe error: {e.user_message}")

    sub.status = "active"
    sub.cancel_at = None
    db.add(sub)
    await db.commit()
    log.info("subscription reactivated: org=%s sub=%s", org_id, sub.stripe_subscription_id)
    return {"status": "ok"}


@router.post("/{org_id}/topup-checkout")
async def create_topup_checkout(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Start a Stripe Checkout session (mode=payment) for a prepaid credit top-up pack.

    body: { "pack_id": "pack_1000" | "pack_2500" | "pack_5000" | "pack_10000" }

    Top-up packs are one-time payments.  The webhook handler grants credits on
    checkout.session.completed with billing_reason=payment (not subscription).
    """
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key

    # Predefined top-up packs (amount_cents → display_label)
    TOPUP_PACKS: dict[str, dict] = {
        "pack_500":   {"cents": 500,   "price_usd": 500,   "label": "$5 — 500 credit cents"},
        "pack_1000":  {"cents": 1000,  "price_usd": 1000,  "label": "$10 — 1,000 credit cents"},
        "pack_2500":  {"cents": 2500,  "price_usd": 2500,  "label": "$25 — 2,500 credit cents"},
        "pack_5000":  {"cents": 5000,  "price_usd": 5000,  "label": "$50 — 5,000 credit cents"},
        "pack_10000": {"cents": 10000, "price_usd": 10000, "label": "$100 — 10,000 credit cents"},
    }

    pack_id = body.get("pack_id", "pack_2500")
    pack = TOPUP_PACKS.get(pack_id)
    if not pack:
        raise HTTPException(400, f"Unknown pack '{pack_id}'. Valid: {list(TOPUP_PACKS)}")

    customer_id = await _get_or_create_stripe_customer(db, stripe, org_id)
    checkout = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        payment_intent_data={
            "setup_future_usage": "off_session",  # save card for auto-recharge
        },
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": pack["price_usd"],
                "product_data": {"name": f"AI Credit Top-up — {pack['label']}"},
            },
            "quantity": 1,
        }],
        success_url=f"{settings.frontend_origin}/settings/plan?topup=success",
        cancel_url=f"{settings.frontend_origin}/settings/plan?topup=cancel",
        metadata={"org_id": str(org_id), "topup_cents": str(pack["cents"]), "pack_id": pack_id},
    )
    return {"url": checkout.url}


@router.get("/{org_id}/billing-settings")
async def get_billing_settings(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Return auto-recharge and notification settings for the org."""
    from app.db.models import OrgBillingSettings
    settings_row = await db.get(OrgBillingSettings, org_id)
    if not settings_row:
        return {
            "auto_recharge_enabled": False,
            "threshold_cents": 500,
            "recharge_amount_cents": 2500,
            "monthly_cap_cents": 20000,
            "auto_recharged_this_month_cents": 0,
            "auto_recharge_failed_at": None,
            "allow_voice_overage": True,
        }
    return {
        "auto_recharge_enabled": settings_row.auto_recharge_enabled,
        "threshold_cents": settings_row.threshold_cents,
        "recharge_amount_cents": settings_row.recharge_amount_cents,
        "monthly_cap_cents": settings_row.monthly_cap_cents,
        "auto_recharged_this_month_cents": settings_row.auto_recharged_this_month_cents,
        "auto_recharge_failed_at": settings_row.auto_recharge_failed_at,
        "allow_voice_overage": settings_row.allow_voice_overage,
    }


@router.patch("/{org_id}/billing-settings")
async def update_billing_settings(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Update auto-recharge and voice-overage settings."""
    from app.db.models import OrgBillingSettings

    settings_row = await db.get(OrgBillingSettings, org_id)
    if not settings_row:
        settings_row = OrgBillingSettings(org_id=org_id)

    enabling_auto_recharge = body.get("auto_recharge_enabled") is True and not settings_row.auto_recharge_enabled
    if "auto_recharge_enabled" in body:
        settings_row.auto_recharge_enabled = bool(body["auto_recharge_enabled"])
    if "threshold_cents" in body:
        settings_row.threshold_cents = max(100, int(body["threshold_cents"]))
    if "recharge_amount_cents" in body:
        settings_row.recharge_amount_cents = max(500, int(body["recharge_amount_cents"]))
    if "monthly_cap_cents" in body:
        settings_row.monthly_cap_cents = max(0, int(body["monthly_cap_cents"]))
    if "allow_voice_overage" in body:
        settings_row.allow_voice_overage = bool(body["allow_voice_overage"])

    # Validate that a saved payment method exists when enabling auto-recharge
    if enabling_auto_recharge and settings.stripe_secret_key:
        import stripe
        stripe.api_key = settings.stripe_secret_key
        org = await db.get(Organization, org_id)
        customer_id = org.stripe_customer_id if org else None
        if customer_id:
            try:
                cust = stripe.Customer.retrieve(customer_id)
                cust_dict = cust._to_dict_recursive() if hasattr(cust, "_to_dict_recursive") else dict(cust)
                pm_id = cust_dict.get("invoice_settings", {}).get("default_payment_method") or cust_dict.get("default_source")
                if not pm_id:
                    raise HTTPException(400, "Add a payment method first — click Manage billing to add a card.")
            except HTTPException:
                raise
            except Exception:
                log.exception("failed to verify payment method for org %s", org_id)

    settings_row.updated_at = datetime.now(UTC)
    db.add(settings_row)
    await db.commit()
    return {"status": "ok"}


@router.post("/{org_id}/auto-recharge")
async def trigger_auto_recharge(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Manually trigger an auto-recharge attempt.  Useful for testing or after a card update."""
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    await _do_auto_recharge(db, stripe, org_id)
    return {"status": "ok"}


async def _do_auto_recharge(db, stripe_mod, org_id: UUID) -> bool:
    """Attempt an off-session PaymentIntent charge for the auto-recharge amount.

    Returns True if charge succeeded and credits were granted.
    """
    from app.db.models import OrgBillingSettings
    from app.core.billing.credits import balance as _balance, grant_topup

    settings_row = await db.get(OrgBillingSettings, org_id)
    if not settings_row or not settings_row.auto_recharge_enabled:
        return False

    # Cap: don't exceed monthly_cap_cents
    remaining_cap = settings_row.monthly_cap_cents - settings_row.auto_recharged_this_month_cents
    if remaining_cap <= 0:
        log.info("auto-recharge: org %s hit monthly cap of %d", org_id, settings_row.monthly_cap_cents)
        return False

    charge_cents = min(settings_row.recharge_amount_cents, remaining_cap)
    org = await db.get(Organization, org_id)
    customer_id = org.stripe_customer_id if org else None
    if not customer_id:
        log.warning("auto-recharge: no customer for org %s", org_id)
        return False

    # Find the customer's default payment method
    try:
        customer = stripe_mod.Customer.retrieve(customer_id)
        cust_dict = customer._to_dict_recursive() if hasattr(customer, "_to_dict_recursive") else dict(customer)
        pm_id = cust_dict.get("invoice_settings", {}).get("default_payment_method") or cust_dict.get("default_source")
        if not pm_id:
            log.warning("auto-recharge: no saved payment method for org %s", org_id)
            return False

        pi = stripe_mod.PaymentIntent.create(
            amount=charge_cents,
            currency="usd",
            customer=customer_id,
            payment_method=pm_id,
            off_session=True,
            confirm=True,
            metadata={"org_id": str(org_id), "type": "auto_recharge", "credits_cents": charge_cents},
        )
        pi_dict = pi._to_dict_recursive() if hasattr(pi, "_to_dict_recursive") else dict(pi)
        if pi_dict.get("status") == "succeeded":
            await grant_topup(db, org_id, charge_cents, stripe_ref=pi_dict["id"], source="auto_recharge")
            settings_row.auto_recharged_this_month_cents = (settings_row.auto_recharged_this_month_cents or 0) + charge_cents
            settings_row.auto_recharge_failed_at = None
            db.add(settings_row)
            await db.commit()
            log.info("auto-recharge: granted %d cents to org %s via pi %s", charge_cents, org_id, pi_dict["id"])
            return True
        else:
            raise RuntimeError(f"PaymentIntent status={pi_dict.get('status')}")

    except Exception as exc:
        log.error("auto-recharge failed for org %s: %s", org_id, exc)
        if settings_row:
            settings_row.auto_recharge_failed_at = datetime.now(UTC)
            db.add(settings_row)
            await db.commit()
        return False


@router.get("/{org_id}/addon-preview")
async def addon_preview(
    org_id: UUID,
    addon_code: str,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Return a preview of adding/removing a voice add-on.

    Used by AddonDialog to show the exact charge (or end date) before confirming.
    Returns:
      {
        action: "add" | "swap_up" | "swap_down" | "remove",
        effective: "immediate" | "period_end",
        amount_due_cents: int,      # prorated charge today (0 for period-end)
        minutes_this_period: int,   # prorated allowance for rest of current period
        period_end: str,            # ISO date of current period end
        renewal_price_cents: int,   # monthly price from next period
      }
    """
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe as _stripe
    _stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    addon = await db.get(Addon, addon_code)
    if not addon or not addon.stripe_price_id:
        raise HTTPException(404, f"Add-on '{addon_code}' not found or not configured")

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(400, "No active subscription — subscribe to a plan first")

    # Current voice add-on (if any)
    current_voice = (await db.exec(
        select(OrgAddon).where(
            OrgAddon.org_id == org_id,
            OrgAddon.status == "active",
            OrgAddon.addon_code.in_(["voice_lite", "voice_standard"]),
        )
    )).first()

    period_end = sub.current_period_end
    period_start = sub.current_period_start or period_end

    # Determine action
    is_current = current_voice and current_voice.addon_code == addon_code and not current_voice.cancel_at
    if is_current:
        action = "remove"
    elif current_voice:
        current_def = await db.get(Addon, current_voice.addon_code)
        current_mins = int((current_def.included or {}).get("voice_minutes", 0)) if current_def else 0
        new_mins = int((addon.included or {}).get("voice_minutes", 0))
        action = "swap_up" if new_mins > current_mins else "swap_down"
    else:
        action = "add"

    effective = "period_end" if action in ("remove", "swap_down") else "immediate"
    renewal_price_cents = int(addon.price_usd_monthly) if action not in ("remove",) else 0  # price_usd_monthly is already in cents

    # Prorated charge for immediate actions
    amount_due_cents = 0
    minutes_this_period = 0

    if effective == "immediate" and period_end:
        try:
            # Use Stripe preview invoice to get exact prorated amount
            customer_id = await _get_or_create_stripe_customer(db, _stripe, org_id)
            _stripe_raw = _stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
            _stripe_sub = _stripe_raw._to_dict_recursive() if hasattr(_stripe_raw, "_to_dict_recursive") else dict(_stripe_raw)
            sub_currency = _stripe_sub.get("currency", "usd")
            classified = await _classify_items(db, _stripe_sub.get("items", {}).get("data", []))
            # Resolve price in the subscription's currency (handles CAD/non-USD subscriptions)
            resolved_price_id = _resolve_addon_price_for_currency(_stripe, addon, sub_currency)

            preview_items = []
            if classified["voice_item"]:
                preview_items.append({"id": classified["voice_item"]["id"], "price": resolved_price_id})
            else:
                preview_items.append({"price": resolved_price_id, "quantity": 1})
                # Add overage item if missing
                if not classified["overage_item"] and addon.stripe_overage_price_id:
                    preview_items.append({"price": addon.stripe_overage_price_id, "quantity": 1})

            preview = _stripe.Invoice.create_preview(
                customer=customer_id,
                subscription=sub.stripe_subscription_id,
                subscription_details={
                    "items": preview_items,
                    "proration_behavior": "always_invoice",
                },
            )
            pv = preview._to_dict_recursive() if hasattr(preview, "_to_dict_recursive") else dict(preview)
            amount_due_cents = max(0, pv.get("amount_due", 0))
        except Exception:
            log.exception("addon-preview: failed to create invoice preview for org %s", org_id)
            # Fallback: compute locally
            import math
            total_minutes = int((addon.included or {}).get("voice_minutes", 0))
            if period_end and period_start:
                now_dt = datetime.now(UTC)
                period_secs = (period_end - period_start).total_seconds()
                remaining_secs = max(0, (period_end - now_dt).total_seconds())
                fraction = remaining_secs / period_secs if period_secs > 0 else 1.0
                prorated_cents = int(renewal_price_cents * fraction)
                if action == "swap_up" and current_voice and current_def:
                    old_price_cents = int(current_def.price_usd_monthly or 0)  # already in cents
                    prorated_cents = int((renewal_price_cents - old_price_cents) * fraction)
                amount_due_cents = max(0, prorated_cents)

        # Prorated minutes
        total_minutes = int((addon.included or {}).get("voice_minutes", 0))
        if period_end and period_start:
            minutes_this_period = await _compute_prorated_minutes(total_minutes, period_start, period_end)
            if action == "swap_up" and current_voice:
                # Extra minutes above current allowance
                current_def2 = await db.get(Addon, current_voice.addon_code)
                current_snap = current_voice.included_snapshot
                if current_snap is None:
                    current_mins2 = int((current_def2.included or {}).get("voice_minutes", 0)) if current_def2 else 0
                else:
                    current_mins2 = int(current_snap)
                minutes_this_period = await _compute_prorated_minutes(
                    max(0, total_minutes - int((current_def2.included or {}).get("voice_minutes", 0))),
                    period_start, period_end,
                ) + current_mins2
    else:
        # Period-end: show full next period allowance
        minutes_this_period = int((addon.included or {}).get("voice_minutes", 0))

    return {
        "action": action,
        "effective": effective,
        "amount_due_cents": amount_due_cents,
        "minutes_this_period": minutes_this_period,
        "period_end": period_end.isoformat() if period_end else None,
        "renewal_price_cents": renewal_price_cents,
    }


@router.post("/{org_id}/addon-checkout")
async def create_addon_checkout(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Activate a voice add-on on the existing plan subscription.

    add / swap_up: immediate with prorated charge + snapshot allowance.
    swap_down / remove: falls back to schedule path (see DELETE endpoint).
    No active subscription: falls back to Stripe Checkout redirect.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    addon_code = body.get("addon_code")
    if not addon_code:
        raise HTTPException(400, "addon_code required")

    addon = await db.get(Addon, addon_code)
    if not addon or not addon.price_usd_monthly:
        raise HTTPException(400, f"Add-on '{addon_code}' has no price configured")

    price_id = addon.stripe_price_id
    if not price_id:
        raise HTTPException(400, f"Add-on '{addon_code}' has no Stripe price ID. Run stripe_sync_prices.py first.")

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()

    if sub and sub.stripe_subscription_id and sub.status in ("active", "trialing", "downgrade_scheduled", "cancel_at_period_end"):
        return await _add_addon_line_item(db, stripe, sub, addon, addon_code, price_id, org_id)

    # No subscription — Checkout redirect (user must subscribe to plan first)
    customer_id = await _get_or_create_stripe_customer(db, stripe, org_id)
    session_params: dict = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{settings.frontend_origin}/settings/plan?addon=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?addon=cancel",
        "metadata": {"org_id": str(org_id), "addon_code": addon_code},
    }
    checkout = stripe.checkout.Session.create(**session_params)
    return {"url": checkout.url}


async def _add_addon_line_item(
    db: AsyncSession,
    stripe_mod,
    sub: OrgSubscription,
    addon: Addon,
    addon_code: str,
    price_id: str,
    org_id: UUID,
) -> dict:
    """Add (or swap-up) a voice add-on immediately with prorated charge + allowance snapshot.

    swap_down is routed to the scheduled path instead.
    """
    import math
    from sqlmodel import select

    # Get current active voice add-on (if any)
    current_voice = (await db.exec(
        select(OrgAddon).where(
            OrgAddon.org_id == org_id,
            OrgAddon.status == "active",
            OrgAddon.addon_code.in_(["voice_lite", "voice_standard"]),
        )
    )).first()

    new_minutes = int((addon.included or {}).get("voice_minutes", 0))

    # Detect swap_down → route to scheduled path
    if current_voice and current_voice.addon_code != addon_code:
        current_def = await db.get(Addon, current_voice.addon_code)
        current_mins = int((current_def.included or {}).get("voice_minutes", 0)) if current_def else 0
        if new_minutes < current_mins:
            # Schedule the swap-down for period end
            return await _schedule_addon_change(db, stripe_mod, sub, org_id, current_voice, addon_code)

    try:
        _stripe_raw = stripe_mod.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
        stripe_sub = _stripe_raw._to_dict_recursive() if hasattr(_stripe_raw, "_to_dict_recursive") else dict(_stripe_raw)
        sub_currency = stripe_sub.get("currency", "usd")
        items = stripe_sub.get("items", {}).get("data", [])
        classified = await _classify_items(db, items)

        # Resolve a price in the subscription's currency (handles non-USD subscriptions)
        resolved_price_id = _resolve_addon_price_for_currency(stripe_mod, addon, sub_currency)

        voice_item = classified["voice_item"]
        overage_item = classified["overage_item"]

        # _classify_items matches only stored USD price IDs; if this subscription
        # uses a non-USD price we may have missed the existing voice item.
        # Re-scan raw items for the resolved (currency-matched) price ID.
        if not voice_item:
            for raw_item in items:
                if raw_item.get("price", {}).get("id") == resolved_price_id:
                    voice_item = raw_item
                    break

        if voice_item:
            if voice_item.get("price", {}).get("id") == resolved_price_id:
                # Already on this exact price — just ensure snapshot/OrgAddon are updated below
                log.info("voice add-on already on price %s for org %s (idempotent)", resolved_price_id, org_id)
            else:
                # Different price → swap
                stripe_mod.SubscriptionItem.modify(
                    voice_item["id"],
                    price=resolved_price_id,
                    proration_behavior="always_invoice",
                )
                log.info("voice add-on swapped to %s for org %s", addon_code, org_id)
        else:
            # Add new voice item
            stripe_mod.SubscriptionItem.create(
                subscription=sub.stripe_subscription_id,
                price=resolved_price_id,
                quantity=1,
                proration_behavior="always_invoice",
                metadata={"addon_code": addon_code, "org_id": str(org_id)},
            )
            log.info("voice add-on %s added to sub %s for org %s", addon_code, org_id, sub.stripe_subscription_id)

        # Add metered overage item if absent
        if not overage_item and addon.stripe_overage_price_id:
            try:
                # Metered price also needs to match the subscription currency
                resolved_overage_price_id = addon.stripe_overage_price_id
                if sub_currency.lower() != "usd":
                    try:
                        overage_base = stripe_mod.Price.retrieve(addon.stripe_overage_price_id)
                        ov_product_id = overage_base.product
                        ov_prices = stripe_mod.Price.list(product=ov_product_id, currency=sub_currency.lower(), active=True, limit=20)
                        matched = None
                        for p in ov_prices.data:
                            rec = p.recurring
                            if rec and getattr(rec, "usage_type", None) == "metered":
                                matched = p.id
                                break
                        if matched:
                            resolved_overage_price_id = matched
                        else:
                            # Create metered price in local currency
                            from app.core.billing.usage import VOICE_OVERAGE_EVENT_NAME
                            meter_obj = stripe_mod.Price.retrieve(addon.stripe_overage_price_id)
                            meter_id = getattr(meter_obj.recurring, "meter", None) if meter_obj.recurring else None
                            create_params = {
                                "product": ov_product_id,
                                "currency": sub_currency.lower(),
                                "unit_amount": overage_base.unit_amount,
                                "recurring": {"interval": "month", "usage_type": "metered"},
                                "nickname": f"Voice Overage per minute ({sub_currency.upper()})",
                            }
                            if meter_id:
                                create_params["recurring"]["meter"] = meter_id
                            new_ov_price = stripe_mod.Price.create(**create_params)
                            resolved_overage_price_id = new_ov_price.id
                            log.info("Created %s metered overage price: %s", sub_currency, new_ov_price.id)
                    except Exception:
                        log.exception("Could not resolve overage price in currency %s, using USD fallback", sub_currency)
                # Idempotent: check raw items for this resolved overage price too
                already_has_overage = any(
                    raw.get("price", {}).get("id") == resolved_overage_price_id
                    for raw in items
                )
                if not already_has_overage:
                    stripe_mod.SubscriptionItem.create(
                        subscription=sub.stripe_subscription_id,
                        price=resolved_overage_price_id,
                        quantity=1,
                        proration_behavior="none",
                        metadata={"type": "voice_overage", "org_id": str(org_id)},
                    )
                    log.info("voice overage item added for org %s", org_id)
                else:
                    log.info("voice overage item already present for org %s (idempotent)", org_id)
            except Exception:
                log.exception("failed to add voice overage item for org %s", org_id)

    except Exception as exc:
        log.error("failed to add voice add-on line item for org %s: %s", org_id, exc)
        raise HTTPException(400, f"Stripe error: {exc}")

    # Compute prorated allowance snapshot for the current period
    now_dt = datetime.now(UTC)
    period_start = sub.current_period_start
    period_end = sub.current_period_end
    if period_start and period_end:
        snapshot = await _compute_prorated_minutes(new_minutes, period_start, period_end)
        if current_voice and current_voice.addon_code != addon_code:
            # Swap-up: add the extra minutes on top of current snapshot (what they already have)
            current_snap = current_voice.included_snapshot
            if current_snap is None:
                current_def2 = await db.get(Addon, current_voice.addon_code)
                current_snap = int((current_def2.included or {}).get("voice_minutes", 0)) if current_def2 else 0
            current_def3 = await db.get(Addon, current_voice.addon_code)
            old_mins = int((current_def3.included or {}).get("voice_minutes", 0)) if current_def3 else 0
            delta_snap = await _compute_prorated_minutes(new_minutes - old_mins, period_start, period_end)
            snapshot = int(current_snap) + delta_snap
    else:
        snapshot = new_minutes

    # Update DB
    existing_db_addon = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon_code)
    )).first()
    if existing_db_addon:
        existing_db_addon.status = "active"
        existing_db_addon.stripe_subscription_id = sub.stripe_subscription_id
        existing_db_addon.activated_at = now_dt
        existing_db_addon.included_snapshot = float(snapshot)
        existing_db_addon.snapshot_period_end = period_end
        existing_db_addon.cancel_at = None
        existing_db_addon.pending_addon_code = None
        existing_db_addon.overage_period_start = period_start
        existing_db_addon.overage_reported = 0.0
        db.add(existing_db_addon)
    else:
        db.add(OrgAddon(
            org_id=org_id,
            addon_code=addon_code,
            stripe_subscription_id=sub.stripe_subscription_id,
            status="active",
            activated_at=now_dt,
            included_snapshot=float(snapshot),
            snapshot_period_end=period_end,
            overage_period_start=period_start,
            overage_reported=0.0,
        ))

    # Cancel the old voice add-on DB row (for swap-up)
    if current_voice and current_voice.addon_code != addon_code:
        current_voice.status = "cancelled"
        current_voice.cancel_at = None
        db.add(current_voice)

    await db.commit()
    log.info("org %s activated addon %s (snapshot=%d min)", org_id, addon_code, snapshot)
    return {"status": "ok", "addon_code": addon_code, "snapshot_minutes": snapshot}


async def _schedule_addon_change(
    db: AsyncSession,
    stripe_mod,
    sub: OrgSubscription,
    org_id: UUID,
    current_voice: OrgAddon,
    new_addon_code: str,
) -> dict:
    """Schedule a voice add-on swap-down for the next billing period."""
    try:
        _stripe_raw = stripe_mod.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
        stripe_sub = _stripe_raw._to_dict_recursive() if hasattr(_stripe_raw, "_to_dict_recursive") else dict(_stripe_raw)
        phase2_items = await _build_phase2_items(db, org_id, sub, await _classify_items(db, stripe_sub.get("items", {}).get("data", [])))
        # Override voice in phase2 with new_addon_code
        new_addon = await db.get(Addon, new_addon_code)
        new_voice_price = new_addon.stripe_price_id if new_addon else None
        if new_voice_price:
            # Replace current voice price with new one in phase2_items
            from app.db.models import Addon as _Addon
            from sqlmodel import select as _sel
            all_voice_prices = {a.stripe_price_id for a in (await db.exec(_sel(_Addon).where(_Addon.active == True))).all() if a.stripe_price_id and a.code in ("voice_lite", "voice_standard")}
            phase2_items = [i for i in phase2_items if i.get("price") not in all_voice_prices]
            phase2_items.append({"price": new_voice_price, "quantity": 1})

        await _sync_period_end_schedule(stripe_mod, sub, stripe_sub, phase2_items)
    except Exception as exc:
        log.error("failed to schedule addon swap-down for org %s: %s", org_id, exc)
        raise HTTPException(400, f"Stripe error: {exc}")

    current_voice.cancel_at = sub.current_period_end
    current_voice.pending_addon_code = new_addon_code
    db.add(current_voice)
    await db.commit()

    period_end_str = sub.current_period_end.strftime("%Y-%m-%d") if sub.current_period_end else "next billing cycle"
    return {"status": "ok", "effective": "period_end", "effective_date": period_end_str}


@router.delete("/{org_id}/addon/{addon_code}")
async def remove_addon(
    org_id: UUID,
    addon_code: str,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Schedule a voice add-on for removal at the end of the current billing period.

    Access continues until period_end; no refund issued.
    The add-on line item is dropped from the Stripe subscription at renewal via a schedule.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")

    oa = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon_code, OrgAddon.status == "active")
    )).first()
    if not oa:
        raise HTTPException(404, "Active add-on not found")

    try:
        _stripe_raw = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
        stripe_sub = _stripe_raw._to_dict_recursive() if hasattr(_stripe_raw, "_to_dict_recursive") else dict(_stripe_raw)
        classified = await _classify_items(db, stripe_sub.get("items", {}).get("data", []))

        # Build phase-2 items without this add-on (and without its overage item if no voice remains)
        oa.cancel_at = sub.current_period_end  # temporarily set so _build_phase2_items excludes it
        phase2_items = await _build_phase2_items(db, org_id, sub, classified)
        oa.cancel_at = None  # reset — we'll set it properly after

        await _sync_period_end_schedule(stripe_mod=stripe, sub=sub, stripe_sub=stripe_sub, phase2_items=phase2_items)

    except Exception as exc:
        log.error("failed to schedule add-on removal %s for org %s: %s", addon_code, org_id, exc)
        raise HTTPException(400, f"Stripe error: {exc}")

    oa.cancel_at = sub.current_period_end
    db.add(oa)
    await db.commit()

    period_end_str = sub.current_period_end.strftime("%Y-%m-%d") if sub.current_period_end else "next billing cycle"
    return {"status": "ok", "effective": "period_end", "effective_date": period_end_str}


@router.post("/{org_id}/addon/{addon_code}/keep")
async def keep_addon(
    org_id: UUID,
    addon_code: str,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Undo a scheduled removal or swap-down for an add-on.

    Clears cancel_at / pending_addon_code and re-syncs the Stripe schedule.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")

    oa = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon_code, OrgAddon.status == "active")
    )).first()
    if not oa:
        raise HTTPException(404, "Active add-on not found")

    oa.cancel_at = None
    oa.pending_addon_code = None
    db.add(oa)

    try:
        _stripe_raw = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
        stripe_sub = _stripe_raw._to_dict_recursive() if hasattr(_stripe_raw, "_to_dict_recursive") else dict(_stripe_raw)
        classified = await _classify_items(db, stripe_sub.get("items", {}).get("data", []))
        phase2_items = await _build_phase2_items(db, org_id, sub, classified)
        await _sync_period_end_schedule(stripe_mod=stripe, sub=sub, stripe_sub=stripe_sub, phase2_items=phase2_items)
    except Exception as exc:
        log.error("failed to re-sync schedule after keep_addon %s for org %s: %s", addon_code, org_id, exc)
        raise HTTPException(400, f"Stripe error: {exc}")

    await db.commit()
    return {"status": "ok"}


@router.post("/{org_id}/portal")
async def create_portal(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key

    org = await db.get(Organization, org_id)
    customer_id = org.stripe_customer_id if org else None
    if not customer_id:
        # Fallback: check subscription row (legacy)
        sub = (await db.exec(
            __import__("sqlmodel", fromlist=["select"]).select(OrgSubscription)
            .where(OrgSubscription.org_id == org_id)
        )).first()
        customer_id = sub.stripe_customer_id if sub else None
    if not customer_id:
        raise HTTPException(400, "No active subscription — start with Checkout first")

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{settings.frontend_origin}/settings/plan",
        )
    except stripe.error.InvalidRequestError as e:
        log.error("Stripe portal error: %s", e.user_message)
        raise HTTPException(400, f"Stripe error: {e.user_message}")
    return {"url": portal.url}


# ── Stripe Webhook ────────────────────────────────────────────────────────────

@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature or "", settings.stripe_webhook_secret
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid Stripe signature")

    event_id = event["id"]
    # Idempotency: skip if already processed
    existing = await db.get(StripeEvent, event_id)
    if existing:
        return {"status": "already_processed"}

    db.add(StripeEvent(event_id=event_id))
    from app.core.billing.stripe_events import handle_stripe_event
    await handle_stripe_event(db, event)
    await db.commit()
    return {"status": "ok"}


async def _handle_stripe_event(db: AsyncSession, event: dict) -> None:
    event_type = event["type"]
    # The Stripe SDK returns StripeObject (not a plain dict) — convert so we can call .get()
    raw_data = event["data"]["object"]
    if hasattr(raw_data, "_to_dict_recursive"):
        data: dict = raw_data._to_dict_recursive()
    elif hasattr(raw_data, "to_dict"):
        data = raw_data.to_dict()
    else:
        data = dict(raw_data)
    log.info("stripe webhook: %s", event_type)

    try:
        await _process_stripe_event(db, event_type, data)
    except Exception:
        log.exception("stripe webhook handler failed for %s", event_type)
        raise


async def _process_stripe_event(db: AsyncSession, event_type: str, data: dict) -> None:
    from sqlmodel import select

    if event_type == "checkout.session.completed":
        meta = data.get("metadata") or {}
        org_id_str = meta.get("org_id")
        if not org_id_str:
            return
        org_id = UUID(org_id_str)
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        addon_code = meta.get("addon_code")
        plan_code = meta.get("plan_code", "pro")
        topup_cents = int(meta["topup_cents"]) if "topup_cents" in meta else None
        mode = data.get("mode", "subscription")

        # Always persist customer_id on the Organization (canonical)
        if customer_id:
            org = await db.get(Organization, org_id)
            if org and not org.stripe_customer_id:
                org.stripe_customer_id = customer_id
                db.add(org)

        # ── One-time payment top-up ──────────────────────────────────────────
        if mode == "payment" and topup_cents:
            payment_intent = data.get("payment_intent")
            from app.core.billing.credits import grant_topup
            await grant_topup(db, org_id, topup_cents, stripe_ref=payment_intent or data.get("id"), source="purchase")
            await db.commit()
            log.info("top-up: granted %d cents to org %s via session %s", topup_cents, org_id, data.get("id"))
            return

        if addon_code and subscription_id:
            # Voice add-on purchase (legacy path; Phase 4 moves to line items)
            existing_addon = (await db.exec(
                select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon_code)
            )).first()
            if existing_addon:
                existing_addon.stripe_subscription_id = subscription_id
                existing_addon.status = "active"
                db.add(existing_addon)
            else:
                db.add(OrgAddon(
                    org_id=org_id,
                    addon_code=addon_code,
                    stripe_subscription_id=subscription_id,
                    status="active",
                ))

        elif subscription_id:
            # Plan subscription — retrieve real period dates from Stripe
            import stripe as _stripe_mod
            _stripe_mod.api_key = settings.stripe_secret_key
            _stripe_raw = _stripe_mod.Subscription.retrieve(subscription_id, expand=["items"])
            _stripe_sub = _stripe_raw._to_dict_recursive() if hasattr(_stripe_raw, "_to_dict_recursive") else dict(_stripe_raw)
            _items = _stripe_sub.get("items", {}).get("data", [])
            # Period dates: newer Stripe API puts them on item level
            _item0 = _items[0] if _items else {}
            _ps_ts = _item0.get("current_period_start") or _stripe_sub.get("current_period_start")
            _pe_ts = _item0.get("current_period_end") or _stripe_sub.get("current_period_end")
            period_start = datetime.fromtimestamp(_ps_ts, tz=UTC) if _ps_ts else datetime.now(UTC)
            period_end = datetime.fromtimestamp(_pe_ts, tz=UTC) if _pe_ts else period_start

            sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
            if sub:
                sub.stripe_customer_id = customer_id
                sub.stripe_subscription_id = subscription_id
                sub.plan_code = plan_code
                sub.status = "active"
                sub.pending_plan_code = None
                sub.current_period_start = period_start
                sub.current_period_end = period_end
                db.add(sub)
            else:
                db.add(OrgSubscription(
                    org_id=org_id,
                    plan_code=plan_code,
                    status="active",
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_start=period_start,
                    current_period_end=period_end,
                ))
            log.info("org %s upgraded to plan=%s sub=%s", org_id, plan_code, subscription_id)

            # Grant plan credit for the new period
            if plan_code != "free":
                plan_obj = await db.get(Plan, plan_code)
                if plan_obj and plan_obj.monthly_credit_cents > 0:
                    from app.core.billing.credits import grant_plan_credit
                    await grant_plan_credit(db, org_id, plan_obj.monthly_credit_cents, period_end, stripe_ref=subscription_id)

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        stripe_sub_id = data["id"]
        sub = (await db.exec(
            select(OrgSubscription).where(OrgSubscription.stripe_subscription_id == stripe_sub_id)
        )).first()
        if sub:
            if event_type == "customer.subscription.deleted":
                sub.status = "cancelled"
                sub.plan_code = "free"
                # Cancel all add-ons tied to this subscription
                addon_rows = (await db.exec(
                    select(OrgAddon).where(
                        OrgAddon.org_id == sub.org_id,
                        OrgAddon.stripe_subscription_id == stripe_sub_id,
                    )
                )).all()
                for oa in addon_rows:
                    oa.status = "cancelled"
                    db.add(oa)
            else:
                # Map Stripe status → our status
                stripe_status = data.get("status", "active")
                # Portal cancellation sets `cancel_at` (timestamp); API cancellation sets `cancel_at_period_end`.
                cancel_at_ts = data.get("cancel_at")
                is_cancelling = bool(data.get("cancel_at_period_end")) or bool(cancel_at_ts)
                if is_cancelling and stripe_status in ("active", "trialing"):
                    sub.status = "cancel_at_period_end"
                elif data.get("schedule") and sub.status == "downgrade_scheduled":
                    sub.status = "downgrade_scheduled"  # preserve our scheduled-downgrade marker
                else:
                    sub.status = stripe_status  # active, past_due, trialing, etc.
                sub.cancel_at = datetime.fromtimestamp(cancel_at_ts, tz=UTC) if cancel_at_ts else None
                # Period dates: newer Stripe API puts them on item level
                items = data.get("items", {}).get("data", [])
                item0 = items[0] if items else {}
                ps_ts = item0.get("current_period_start") or data.get("current_period_start")
                pe_ts = cancel_at_ts or item0.get("current_period_end") or data.get("current_period_end")
                if pe_ts:
                    sub.current_period_end = datetime.fromtimestamp(pe_ts, tz=UTC)
                if ps_ts:
                    sub.current_period_start = datetime.fromtimestamp(ps_ts, tz=UTC)
                # Resolve plan_code: prefer metadata, fall back to price-ID lookup in plans table
                resolved = await _plan_code_from_stripe_sub(db, data)
                if resolved:
                    # If the plan actually changed (renewal after downgrade), clear pending
                    if resolved != sub.plan_code:
                        sub.pending_plan_code = None
                    sub.plan_code = resolved
                    log.info("subscription.updated: sub=%s → plan=%s status=%s", stripe_sub_id, resolved, sub.status)

                # Reconcile voice add-ons from line items
                await _reconcile_addons_from_items(db, sub.org_id, stripe_sub_id, data, sub)
            db.add(sub)

    elif event_type == "invoice.paid":
        # Grant monthly plan credit on renewal
        stripe_sub_id = data.get("subscription")
        period_end_ts = data.get("period_end") or data.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")
        if stripe_sub_id and period_end_ts:
            sub = (await db.exec(
                select(OrgSubscription).where(OrgSubscription.stripe_subscription_id == stripe_sub_id)
            )).first()
            if sub and sub.plan_code not in ("free",):
                plan_obj = await db.get(Plan, sub.plan_code)
                if plan_obj and plan_obj.monthly_credit_cents > 0:
                    period_end = datetime.fromtimestamp(period_end_ts, tz=UTC)
                    from app.core.billing.credits import grant_plan_credit
                    await grant_plan_credit(db, sub.org_id, plan_obj.monthly_credit_cents, period_end, stripe_ref=data.get("id"))

    elif event_type == "payment_intent.payment_failed":
        # Handle auto-recharge failure — mark the org so UI can surface it
        meta = data.get("metadata") or {}
        if meta.get("type") == "auto_recharge" and meta.get("org_id"):
            try:
                from app.db.models import OrgBillingSettings
                bid = UUID(meta["org_id"])
                settings_row = await db.get(OrgBillingSettings, bid)
                if settings_row:
                    settings_row.auto_recharge_failed_at = datetime.now(UTC)
                    db.add(settings_row)
                    await db.commit()
            except Exception:
                log.exception("failed to mark auto-recharge failure for org %s", meta.get("org_id"))

    elif event_type == "invoice.payment_failed":
        stripe_sub_id = data.get("subscription")
        if stripe_sub_id:
            sub = (await db.exec(
                select(OrgSubscription).where(OrgSubscription.stripe_subscription_id == stripe_sub_id)
            )).first()
            if sub:
                sub.status = "past_due"
                db.add(sub)


async def _reconcile_addons_from_items(
    db: AsyncSession,
    org_id: UUID,
    stripe_sub_id: str,
    sub_data: dict,
    sub: "OrgSubscription | None" = None,
) -> None:
    """Sync OrgAddon rows based on subscription line items.

    Called on customer.subscription.updated (including renewals):
    - Add-on present in items → ensure status=active; on renewal clear snapshot + reset overage
    - Add-on absent from items → cancelled (scheduled removal took effect)
    - pending_addon_code that appeared → activate it with full allowance
    """
    from sqlmodel import select

    items = sub_data.get("items", {}).get("data", [])
    item_price_ids = {item.get("price", {}).get("id") for item in items if item.get("price", {}).get("id")}
    # Also build a set of product IDs from the subscription items — Stripe always includes
    # price.product in webhook payloads, so we can match currency-agnostic.
    item_product_ids = {item.get("price", {}).get("product") for item in items if item.get("price", {}).get("product")}

    addons = (await db.exec(select(Addon).where(Addon.active == True))).all()
    # Map price_id → addon
    price_to_addon = {a.stripe_price_id: a for a in addons if a.stripe_price_id}

    # Resolve product IDs for our addon prices by looking them up in Stripe (once per unique price).
    # We cache results in a local dict to avoid repeat calls.
    import stripe as _stripe_mod
    price_product_cache: dict[str, str | None] = {}

    async def _product_for_price(price_id: str) -> str | None:
        if price_id in price_product_cache:
            return price_product_cache[price_id]
        try:
            p = _stripe_mod.Price.retrieve(price_id)
            pid = p.get("product") if isinstance(p, dict) else getattr(p, "product", None)
        except Exception:
            pid = None
        price_product_cache[price_id] = pid
        return pid

    period_start = sub.current_period_start if sub else None

    for addon in addons:
        if not addon.stripe_price_id:
            continue
        oa = (await db.exec(
            select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon.code)
        )).first()

        # Match by price ID first (fast path for USD subscriptions), then by product ID
        # (currency-agnostic path for CAD or any other non-USD subscription).
        in_sub = addon.stripe_price_id in item_price_ids
        if not in_sub and item_product_ids:
            addon_product = await _product_for_price(addon.stripe_price_id)
            in_sub = bool(addon_product and addon_product in item_product_ids)

        if in_sub:
            if oa is None:
                # Add-on appeared via portal
                db.add(OrgAddon(
                    org_id=org_id,
                    addon_code=addon.code,
                    stripe_subscription_id=stripe_sub_id,
                    status="active",
                    activated_at=datetime.now(UTC),
                    included_snapshot=None,  # full allowance from now
                    overage_period_start=period_start,
                    overage_reported=0.0,
                ))
            elif oa.status != "active":
                oa.status = "active"
                oa.stripe_subscription_id = stripe_sub_id
                db.add(oa)
            else:
                # Already active — check if this is a renewal (new period)
                if period_start and oa.overage_period_start != period_start:
                    # New billing period started — clear snapshot, reset overage reporting
                    oa.included_snapshot = None
                    oa.snapshot_period_end = None
                    oa.cancel_at = None
                    oa.pending_addon_code = None
                    oa.overage_reported = 0.0
                    oa.overage_period_start = period_start
                    db.add(oa)
        else:
            if oa and oa.status == "active":
                # Check if this is a swap to pending_addon_code
                if oa.pending_addon_code:
                    pending = oa.pending_addon_code
                    oa.status = "cancelled"
                    oa.cancel_at = None
                    oa.pending_addon_code = None
                    db.add(oa)
                    # Activate the pending add-on
                    new_oa = (await db.exec(
                        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == pending)
                    )).first()
                    if new_oa:
                        new_oa.status = "active"
                        new_oa.included_snapshot = None
                        new_oa.overage_reported = 0.0
                        new_oa.overage_period_start = period_start
                        db.add(new_oa)
                    else:
                        db.add(OrgAddon(
                            org_id=org_id,
                            addon_code=pending,
                            stripe_subscription_id=stripe_sub_id,
                            status="active",
                            activated_at=datetime.now(UTC),
                            overage_period_start=period_start,
                        ))
                else:
                    # Add-on removed (scheduled removal took effect or portal removal)
                    oa.status = "cancelled"
                    oa.cancel_at = None
                    db.add(oa)


async def _plan_code_from_stripe_sub(db: AsyncSession, sub: dict) -> str | None:
    """Resolve plan_code from a Stripe subscription object.
    Priority: 1) subscription metadata  2) price-ID lookup in plans table  3) None.
    """
    from sqlmodel import select

    # 1. Metadata set by our own modify() call
    meta_code = (sub.get("metadata") or {}).get("plan_code")
    if meta_code:
        return meta_code

    # 2. Look up by Stripe price ID in the plans table
    items = sub.get("items", {}).get("data", [])
    if items:
        price_id = items[0].get("price", {}).get("id")
        if price_id:
            plan = (await db.exec(
                select(Plan).where(Plan.stripe_monthly_price_id == price_id)
            )).first()
            if plan:
                return plan.plan_code

    return None
