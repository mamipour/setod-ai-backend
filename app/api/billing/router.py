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
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Billing not configured")
    import stripe
    stripe.api_key = settings.stripe_secret_key

    plan_code = body.get("plan_code", "pro")
    plan = await db.get(Plan, plan_code)
    if not plan or not plan.stripe_monthly_price_id:
        raise HTTPException(400, f"Plan '{plan_code}' has no Stripe price configured")

    sub = (await db.exec(
        __import__("sqlmodel", fromlist=["select"]).select(OrgSubscription)
        .where(OrgSubscription.org_id == org_id)
    )).first()

    session_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": plan.stripe_monthly_price_id, "quantity": 1}],
        "success_url": f"{settings.frontend_origin}/settings/plan?checkout=success",
        "cancel_url": f"{settings.frontend_origin}/settings/plan?checkout=cancel",
        "metadata": {"org_id": str(org_id)},
        "automatic_tax": {"enabled": True},
    }
    if sub and sub.stripe_customer_id:
        session_params["customer"] = sub.stripe_customer_id

    checkout = stripe.checkout.Session.create(**session_params)
    return {"url": checkout.url}


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
        "automatic_tax": {"enabled": True},
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

    portal = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=f"{settings.frontend_origin}/settings/plan",
    )
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
    from sqlmodel import select
    event_type = event["type"]
    data = event["data"]["object"]
    log.info(f"stripe webhook: {event_type}")

    if event_type == "checkout.session.completed":
        org_id = UUID(data["metadata"]["org_id"])
        customer_id = data["customer"]
        subscription_id = data["subscription"]
        addon_code = data.get("metadata", {}).get("addon_code")

        if addon_code and subscription_id:
            # Voice add-on purchase
            import stripe
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
            # Ensure customer ID is stored
            sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
            if sub and not sub.stripe_customer_id:
                sub.stripe_customer_id = customer_id
                db.add(sub)
        elif subscription_id:
            import stripe
            sub_obj = stripe.Subscription.retrieve(subscription_id)
            plan_code = _plan_from_stripe_sub(sub_obj)
            sub = (await db.exec(select(OrgSubscription).where(OrgSubscription.org_id == org_id))).first()
            if sub:
                sub.stripe_customer_id = customer_id
                sub.stripe_subscription_id = subscription_id
                sub.plan_code = plan_code
                sub.status = "active"
                sub.current_period_start = datetime.fromtimestamp(sub_obj["current_period_start"], tz=UTC)
                sub.current_period_end = datetime.fromtimestamp(sub_obj["current_period_end"], tz=UTC)
                db.add(sub)
            else:
                db.add(OrgSubscription(
                    org_id=org_id,
                    plan_code=plan_code,
                    status="active",
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_start=datetime.fromtimestamp(sub_obj["current_period_start"], tz=UTC),
                    current_period_end=datetime.fromtimestamp(sub_obj["current_period_end"], tz=UTC),
                ))

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        stripe_sub_id = data["id"]
        sub = (await db.exec(
            select(OrgSubscription).where(OrgSubscription.stripe_subscription_id == stripe_sub_id)
        )).first()
        if sub:
            if event_type == "customer.subscription.deleted":
                sub.status = "cancelled"
            else:
                sub.status = data.get("status", sub.status)
                if data.get("current_period_start"):
                    sub.current_period_start = datetime.fromtimestamp(data["current_period_start"], tz=UTC)
                if data.get("current_period_end"):
                    sub.current_period_end = datetime.fromtimestamp(data["current_period_end"], tz=UTC)
                plan_code = _plan_from_stripe_sub(data)
                if plan_code:
                    sub.plan_code = plan_code
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


def _plan_from_stripe_sub(sub: dict) -> str:
    """Extract plan code from a Stripe subscription object via Stripe price ID → plans table."""
    items = sub.get("items", {}).get("data", [])
    if not items:
        return "free"
    price_id = items[0].get("price", {}).get("id")
    # We store price IDs on the plans table — this is a best-effort sync.
    # Full resolution happens via the sqladmin panel when price IDs are entered.
    return sub.get("metadata", {}).get("plan_code", "pro")
