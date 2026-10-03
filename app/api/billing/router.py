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
from app.db.models import Addon, OrgAddon, OrgSubscription, Plan, StripeEvent, User
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


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

    return {
        "plan_code": ent.plan_code,
        "plan_name": plan.display_name if plan else ent.plan_code,
        "features": ent.features,
        "limits": ent.limits,
        "included": ent.included,
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
    if sub and sub.stripe_subscription_id and sub.status in ("active", "trialing"):
        return await _change_plan_inline(db, stripe, sub, plan, plan_code)

    session_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": plan.stripe_monthly_price_id, "quantity": 1}],
        "success_url": f"{settings.frontend_origin}/settings/plan?checkout=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?checkout=cancel",
        "metadata": {"org_id": str(org_id), "plan_code": plan_code},
    }
    if sub and sub.stripe_customer_id:
        session_params["customer"] = sub.stripe_customer_id

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

    if sub and sub.stripe_subscription_id and sub.status in ("active", "trialing"):
        return await _change_plan_inline(db, stripe, sub, plan, plan_code)

    # No existing sub — return a checkout URL
    session_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": plan.stripe_monthly_price_id, "quantity": 1}],
        "success_url": f"{settings.frontend_origin}/settings/plan?checkout=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?checkout=cancel",
        "metadata": {"org_id": str(org_id), "plan_code": plan_code},
    }
    if sub and sub.stripe_customer_id:
        session_params["customer"] = sub.stripe_customer_id
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
            # Undo cancel_at_period_end
            stripe.Subscription.modify(
                sub.stripe_subscription_id,
                cancel_at_period_end=False,
            )
    except stripe.error.InvalidRequestError as e:
        log.error("Stripe reactivate error: %s", e.user_message)
        raise HTTPException(400, f"Stripe error: {e.user_message}")

    sub.status = "active"
    db.add(sub)
    await db.commit()
    log.info("subscription reactivated: org=%s sub=%s", org_id, sub.stripe_subscription_id)
    return {"status": "ok"}


@router.post("/{org_id}/addon-checkout")
async def create_addon_checkout(
    org_id: UUID,
    body: dict,
    current_user: Annotated[User, Depends(require_owner)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Start a Stripe Checkout session for a voice add-on (voice_lite or voice_standard)."""
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key
    from sqlmodel import select

    addon_code = body.get("addon_code")
    if not addon_code:
        raise HTTPException(400, "addon_code required")

    addon = await db.get(Addon, addon_code)
    if not addon or not addon.stripe_price_id:
        raise HTTPException(400, f"Add-on '{addon_code}' has no Stripe price configured")

    sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()

    session_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": addon.stripe_price_id, "quantity": 1}],
        "success_url": f"{settings.frontend_origin}/settings/plan?addon=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?addon=cancel",
        "metadata": {"org_id": str(org_id), "addon_code": addon_code},
    }
    if sub and sub.stripe_customer_id:
        session_params["customer"] = sub.stripe_customer_id

    checkout = stripe.checkout.Session.create(**session_params)
    return {"url": checkout.url}


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

    sub = (await db.exec(
        __import__("sqlmodel", fromlist=["select"]).select(OrgSubscription)
        .where(OrgSubscription.org_id == org_id)
    )).first()
    if not sub or not sub.stripe_customer_id:
        raise HTTPException(400, "No active subscription — start with Checkout first")

    try:
        portal = stripe.billing_portal.Session.create(
            customer=sub.stripe_customer_id,
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
        org_id = UUID(meta["org_id"])
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        addon_code = meta.get("addon_code")
        plan_code = meta.get("plan_code", "pro")

        if addon_code and subscription_id:
            # Voice add-on purchase
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
            # Store customer ID on the main subscription row
            sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
            if sub and not sub.stripe_customer_id:
                sub.stripe_customer_id = customer_id
                db.add(sub)

        elif subscription_id:
            # Plan upgrade — no external Stripe call needed; plan_code is in session metadata
            sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
            now = datetime.now(UTC)
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            from datetime import timedelta as _td
            period_end = (now.replace(day=1) + _td(days=32)).replace(day=1)
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

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        stripe_sub_id = data["id"]
        sub = (await db.exec(
            select(OrgSubscription).where(OrgSubscription.stripe_subscription_id == stripe_sub_id)
        )).first()
        if sub:
            if event_type == "customer.subscription.deleted":
                sub.status = "cancelled"
                sub.plan_code = "free"
            else:
                # Map Stripe status → our status
                stripe_status = data.get("status", "active")
                cancel_at_period_end = data.get("cancel_at_period_end", False)
                if cancel_at_period_end:
                    sub.status = "cancel_at_period_end"
                else:
                    sub.status = stripe_status  # active, past_due, trialing, etc.
                # Use cancel_at as the effective end date when scheduled for cancellation
                period_end_ts = data.get("cancel_at") or data.get("current_period_end")
                if period_end_ts:
                    sub.current_period_end = datetime.fromtimestamp(period_end_ts, tz=UTC)
                if data.get("current_period_start"):
                    sub.current_period_start = datetime.fromtimestamp(data["current_period_start"], tz=UTC)
                # Resolve plan_code: prefer metadata, fall back to price-ID lookup in plans table
                resolved = await _plan_code_from_stripe_sub(db, data)
                if resolved:
                    sub.plan_code = resolved
                    log.info("subscription.updated: sub=%s → plan=%s status=%s", stripe_sub_id, resolved, sub.status)
            db.add(sub)

    elif event_type == "invoice.payment_failed":
        stripe_sub_id = data.get("subscription")
        if stripe_sub_id:
            sub = (await db.exec(
                select(OrgSubscription).where(OrgSubscription.stripe_subscription_id == stripe_sub_id)
            )).first()
            if sub:
                sub.status = "past_due"
                db.add(sub)


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
