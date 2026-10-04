"""Stripe webhook event handlers.

Extracted from app.api.billing.router (R5 refactor) into the core billing
layer so that the event logic is testable independently of the HTTP layer.

Entry point: ``handle_stripe_event(db, event)``
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import (
    Addon,
    CreditGrant,
    OrgAddon,
    OrgSubscription,
    Organization,
    Plan,
)

log = logging.getLogger(__name__)

async def handle_stripe_event(db: AsyncSession, event: dict) -> None:
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
