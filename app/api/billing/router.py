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
                # overage_price_per_unit in cents (29 cents = $0.29/min)
                # Use round() not int() to avoid float truncation (0.29 * 100 = 28.999...)
                "overage_price_per_unit": round(((a.features or {}).get("overage_price_usd", 0.29)) * 100),
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
    # If downgrade is scheduled, fetch the pending plan code from metadata
    pending_plan_code = None
    if sub and sub.status == "downgrade_scheduled" and sub.stripe_subscription_id:
        try:
            import stripe
            stripe.api_key = settings.stripe_secret_key
            _raw = stripe.Subscription.retrieve(sub.stripe_subscription_id)
            stripe_sub = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
            schedule_id = stripe_sub.get("schedule")
            if schedule_id:
                _sched = stripe.SubscriptionSchedule.retrieve(schedule_id)
                sched = _sched._to_dict_recursive() if hasattr(_sched, "_to_dict_recursive") else dict(_sched)
                phases = sched.get("phases", [])
                if len(phases) > 1:
                    next_phase = phases[1]
                    next_items = next_phase.get("items", [])
                    if next_items:
                        next_price = next_items[0].get("price")
                        if isinstance(next_price, dict):
                            next_price = next_price.get("id")
                        if next_price:
                            from sqlmodel import select
                            next_plan = (await db.exec(select(Plan).where(Plan.stripe_monthly_price_id == next_price))).first()
                            if next_plan:
                                pending_plan_code = next_plan.code
        except Exception:
            log.exception("Failed to fetch pending downgrade plan")

    # Credit balance
    from app.core.billing.credits import balance as credit_balance
    from sqlmodel import select as _select
    balance_cents = await credit_balance(db, org_id)
    monthly_credit_cents = plan.monthly_credit_cents if plan else 0

    # Active add-on codes
    active_addon_rows = (await db.exec(
        _select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.status == "active")
    )).all()
    active_addons = [oa.addon_code for oa in active_addon_rows]

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
    session_params: dict = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": plan.stripe_monthly_price_id, "quantity": 1}],
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
    session_params: dict = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": plan.stripe_monthly_price_id, "quantity": 1}],
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
    _raw = stripe_mod.Subscription.retrieve(sub.stripe_subscription_id)
    stripe_sub: dict = _raw._to_dict_recursive() if hasattr(_raw, "_to_dict_recursive") else dict(_raw)
    items = stripe_sub.get("items", {}).get("data", [])
    if not items:
        raise HTTPException(500, "Stripe subscription has no line items")

    item_id = items[0]["id"]
    current_price_id = items[0].get("price", {}).get("id", "")

    # No-op if already on this plan
    if current_price_id == plan.stripe_monthly_price_id:
        return {"status": "ok", "message": "Already on this plan"}

    try:
        if target_rank > current_rank:
            # ── Upgrade: immediate switch with prorated charge ──
            _upd = stripe_mod.Subscription.modify(
                sub.stripe_subscription_id,
                items=[{"id": item_id, "price": plan.stripe_monthly_price_id}],
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
            # Use Stripe subscription schedule to switch price at period end.
            # First, check if a schedule already exists (from a previous pending downgrade).
            schedule_id = stripe_sub.get("schedule")
            if schedule_id:
                # Release the existing schedule so we can create a fresh one
                stripe_mod.SubscriptionSchedule.release(schedule_id)

            # Create a new schedule from the current subscription
            _sched = stripe_mod.SubscriptionSchedule.create(from_subscription=sub.stripe_subscription_id)
            sched: dict = _sched._to_dict_recursive() if hasattr(_sched, "_to_dict_recursive") else dict(_sched)

            # The schedule has one phase (current). Add a second phase for the new plan.
            current_phase = sched.get("phases", [{}])[0]
            current_end = current_phase.get("end_date")  # unix ts of period end

            stripe_mod.SubscriptionSchedule.modify(
                sched["id"],
                phases=[
                    # Phase 1: keep current plan until period end
                    {
                        "items": [{"price": current_price_id, "quantity": 1}],
                        "start_date": current_phase.get("start_date"),
                        "end_date": current_end,
                    },
                    # Phase 2: switch to downgraded plan, auto-renew
                    {
                        "items": [{"price": plan.stripe_monthly_price_id, "quantity": 1}],
                        "start_date": current_end,
                        "metadata": {"plan_code": plan_code},
                    },
                ],
                end_behavior="release",
                metadata={"plan_code": plan_code, "org_id": str(sub.org_id)},
            )

            # Mark pending downgrade in our DB
            sub.status = "downgrade_scheduled"
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
        }
    return {
        "auto_recharge_enabled": settings_row.auto_recharge_enabled,
        "threshold_cents": settings_row.threshold_cents,
        "recharge_amount_cents": settings_row.recharge_amount_cents,
        "monthly_cap_cents": settings_row.monthly_cap_cents,
        "auto_recharged_this_month_cents": settings_row.auto_recharged_this_month_cents,
        "auto_recharge_failed_at": settings_row.auto_recharge_failed_at,
    }


@router.patch("/{org_id}/billing-settings")
async def update_billing_settings(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Update auto-recharge settings."""
    from app.db.models import OrgBillingSettings

    settings_row = await db.get(OrgBillingSettings, org_id)
    if not settings_row:
        settings_row = OrgBillingSettings(org_id=org_id)

    if "auto_recharge_enabled" in body:
        settings_row.auto_recharge_enabled = bool(body["auto_recharge_enabled"])
    if "threshold_cents" in body:
        settings_row.threshold_cents = max(100, int(body["threshold_cents"]))
    if "recharge_amount_cents" in body:
        settings_row.recharge_amount_cents = max(500, int(body["recharge_amount_cents"]))
    if "monthly_cap_cents" in body:
        settings_row.monthly_cap_cents = max(0, int(body["monthly_cap_cents"]))

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


@router.post("/{org_id}/addon-checkout")
async def create_addon_checkout(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Add a voice add-on as a line item on the existing plan subscription.

    If the org already has an active subscription, the add-on price is added as a new
    subscription item (inline modification — no Checkout redirect needed).
    If there is no subscription yet, falls back to a Checkout session.
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

    # Prefer the USD price ID; fall back to legacy stripe_price_id
    price_id = addon.stripe_price_id
    if not price_id:
        raise HTTPException(400, f"Add-on '{addon_code}' has no Stripe price ID configured yet. Run stripe_sync_prices.py first.")

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()

    # If there's an existing active subscription, add as a line item inline
    if sub and sub.stripe_subscription_id and sub.status in ("active", "trialing"):
        return await _add_addon_line_item(db, stripe, sub, addon, addon_code, price_id, org_id)

    # No subscription — fall back to Checkout (only the add-on, user must have plan first)
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
    addon,
    addon_code: str,
    price_id: str,
    org_id: UUID,
) -> dict:
    """Add (or swap) a voice add-on as a subscription line item on the existing plan sub."""
    from sqlmodel import select

    # Check if this org already has a different voice add-on active
    existing_addon = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code != addon_code)
        .where(OrgAddon.addon_code.in_(["voice_lite", "voice_standard"]))
    )).first()

    try:
        stripe_sub = stripe_mod.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
        sub_dict = stripe_sub._to_dict_recursive() if hasattr(stripe_sub, "_to_dict_recursive") else dict(stripe_sub)

        # Find existing voice item (if any)
        items = sub_dict.get("items", {}).get("data", [])
        voice_item_id: str | None = None
        # Collect Stripe price IDs of currently active voice add-ons for this org
        voice_addon_codes = [oa.addon_code for oa in (await db.exec(
            select(OrgAddon).where(
                OrgAddon.org_id == org_id,
                OrgAddon.addon_code.in_(["voice_lite", "voice_standard"]),
                OrgAddon.status == "active",
            )
        )).all()]
        voice_prices = set()
        for vc in voice_addon_codes:
            va = await db.get(Addon, vc)
            if va and va.stripe_price_id:
                voice_prices.add(va.stripe_price_id)

        # Only match items whose price ID is a known voice add-on price
        all_voice_price_ids: set[str] = set()
        for vc_code in ("voice_lite", "voice_standard"):
            vc_addon = await db.get(Addon, vc_code)
            if vc_addon and vc_addon.stripe_price_id:
                all_voice_price_ids.add(vc_addon.stripe_price_id)

        for item in items:
            item_price_id = item.get("price", {}).get("id")
            if item_price_id and item_price_id in all_voice_price_ids:
                voice_item_id = item["id"]
                break

        if voice_item_id:
            # Swap: update the existing voice item to the new price, charge proration now
            stripe_mod.SubscriptionItem.modify(
                voice_item_id,
                price=price_id,
                proration_behavior="always_invoice",
            )
            log.info("voice add-on swapped to %s for org %s sub %s", addon_code, org_id, sub.stripe_subscription_id)
        else:
            # Add new line item and immediately invoice the prorated amount
            stripe_mod.SubscriptionItem.create(
                subscription=sub.stripe_subscription_id,
                price=price_id,
                quantity=1,
                proration_behavior="always_invoice",
                metadata={"addon_code": addon_code, "org_id": str(org_id)},
            )
            log.info("voice add-on %s added to sub %s for org %s", addon_code, org_id, sub.stripe_subscription_id)

    except Exception as exc:
        log.error("failed to add voice add-on line item for org %s: %s", org_id, exc)
        raise HTTPException(400, f"Stripe error: {exc}")

    # Update local DB
    existing_db_addon = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon_code)
    )).first()
    if existing_db_addon:
        existing_db_addon.status = "active"
        existing_db_addon.stripe_subscription_id = sub.stripe_subscription_id
        db.add(existing_db_addon)
    else:
        db.add(OrgAddon(
            org_id=org_id,
            addon_code=addon_code,
            stripe_subscription_id=sub.stripe_subscription_id,
            status="active",
        ))
    # Remove the swapped-out add-on if any
    if existing_addon:
        existing_addon.status = "cancelled"
        db.add(existing_addon)

    await db.commit()
    return {"status": "ok", "addon_code": addon_code}


@router.delete("/{org_id}/addon/{addon_code}")
async def remove_addon(
    org_id: UUID,
    addon_code: str,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Remove a voice add-on line item from the subscription."""
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")

    addon = await db.get(Addon, addon_code)
    if not addon or not addon.stripe_price_id:
        raise HTTPException(404, "Add-on not found")

    try:
        stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items"])
        sub_dict = stripe_sub._to_dict_recursive() if hasattr(stripe_sub, "_to_dict_recursive") else dict(stripe_sub)
        items = sub_dict.get("items", {}).get("data", [])
        item_id = next((it["id"] for it in items if it.get("price", {}).get("id") == addon.stripe_price_id), None)
        if item_id:
            # proration_behavior=always_invoice credits the unused days back to the customer immediately
            stripe.SubscriptionItem.delete(item_id, proration_behavior="always_invoice")
    except Exception as exc:
        log.error("failed to remove add-on %s for org %s: %s", addon_code, org_id, exc)
        raise HTTPException(400, f"Stripe error: {exc}")

    oa = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon_code)
    )).first()
    if oa:
        oa.status = "cancelled"
        db.add(oa)
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
    await _handle_stripe_event(db, event)
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
            # Plan subscription — plan_code is in session metadata
            sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
            from datetime import timedelta as _billing_td
            now = datetime.now(UTC)
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            period_end = (now.replace(day=1) + _billing_td(days=32)).replace(day=1)
            if sub:
                sub.stripe_customer_id = customer_id
                sub.stripe_subscription_id = subscription_id
                sub.plan_code = plan_code
                sub.status = "active"
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
                # Period end: newer API versions put it on the item, not the subscription
                items = data.get("items", {}).get("data", [])
                item_period_end = items[0].get("current_period_end") if items else None
                period_end_ts = cancel_at_ts or data.get("current_period_end") or item_period_end
                if period_end_ts:
                    sub.current_period_end = datetime.fromtimestamp(period_end_ts, tz=UTC)
                if data.get("current_period_start"):
                    sub.current_period_start = datetime.fromtimestamp(data["current_period_start"], tz=UTC)
                # Resolve plan_code: prefer metadata, fall back to price-ID lookup in plans table
                resolved = await _plan_code_from_stripe_sub(db, data)
                if resolved:
                    sub.plan_code = resolved
                    log.info("subscription.updated: sub=%s → plan=%s status=%s", stripe_sub_id, resolved, sub.status)

                # Reconcile voice add-ons from line items
                await _reconcile_addons_from_items(db, sub.org_id, stripe_sub_id, data)
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


async def _reconcile_addons_from_items(db: AsyncSession, org_id: UUID, stripe_sub_id: str, sub_data: dict) -> None:
    """Sync OrgAddon rows based on subscription line items.

    For each voice add-on: if its price is in the subscription items → active,
    otherwise → cancelled.  Handles both add and remove via Stripe portal or API.
    """
    from sqlmodel import select

    items = sub_data.get("items", {}).get("data", [])
    item_price_ids = {item.get("price", {}).get("id") for item in items if item.get("price", {}).get("id")}

    addons = (await db.exec(select(Addon).where(Addon.active == True))).all()
    for addon in addons:
        if not addon.stripe_price_id:
            continue
        oa = (await db.exec(
            select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.addon_code == addon.code)
        )).first()

        in_sub = addon.stripe_price_id in item_price_ids

        if in_sub and oa is None:
            # Add-on appeared in Stripe (e.g. added via portal)
            db.add(OrgAddon(
                org_id=org_id,
                addon_code=addon.code,
                stripe_subscription_id=stripe_sub_id,
                status="active",
            ))
        elif in_sub and oa and oa.status != "active":
            oa.status = "active"
            oa.stripe_subscription_id = stripe_sub_id
            db.add(oa)
        elif not in_sub and oa and oa.stripe_subscription_id == stripe_sub_id and oa.status == "active":
            # Add-on removed from subscription
            oa.status = "cancelled"
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
