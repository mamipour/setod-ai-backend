"""Stripe subscription management helpers.

These pure-business-logic functions were extracted from app.api.billing.router
(R5 refactor) so they can be imported by the API layer, the webhook handler,
and future background jobs without pulling in the FastAPI router itself.

The Stripe SDK is imported lazily (``import stripe``) inside each function so
this module can be imported in non-production contexts without the key set.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.db.models import Addon, OrgSubscription, Organization

log = logging.getLogger(__name__)


async def get_or_create_stripe_customer(
    db: AsyncSession,
    stripe_mod,
    org_id: UUID,
) -> str:
    """Return the Stripe customer ID for an org, creating one if it doesn't exist.

    ``stripe_customer_id`` is stored on Organization (canonical) and mirrored
    on OrgSubscription for convenience.  This helper always reads from / writes
    to Organization so that flushing the subscription row never orphans the
    customer.
    """
    org = await db.get(Organization, org_id)
    if not org:
        raise ValueError(f"Organization {org_id} not found")

    if org.stripe_customer_id:
        return org.stripe_customer_id

    customer = stripe_mod.Customer.create(
        email=None,
        metadata={"org_id": str(org_id), "slug": org.slug},
        name=org.name,
    )
    org.stripe_customer_id = customer["id"]
    db.add(org)
    await db.flush()
    return customer["id"]


def resolve_addon_price_for_currency(
    stripe_mod,
    addon: "Addon",
    sub_currency: str,
) -> str:
    """Return the Stripe price ID to use for an add-on in the subscription's currency.

    Addons carry both a USD price and an optional non-USD price.  For non-USD
    subscriptions we use the alternate price when available; otherwise we fall
    back to the primary USD price (Stripe will handle currency conversion).
    """
    if sub_currency.lower() != "usd" and addon.stripe_price_id_noncurrency:
        return addon.stripe_price_id_noncurrency
    return addon.stripe_price_id or ""


async def get_voice_overage_price_id(db: AsyncSession) -> str | None:
    """Return the shared Stripe metered price for plan-based voice overage."""
    row = (
        await db.exec(select(Addon).where(Addon.stripe_overage_price_id.is_not(None)))
    ).first()
    return row.stripe_overage_price_id if row else None


async def get_active_subscription(db: AsyncSession, org_id: UUID) -> "OrgSubscription | None":
    """Return the org's active OrgSubscription, or None."""
    return (
        await db.exec(
            select(OrgSubscription).where(OrgSubscription.org_id == org_id)
        )
    ).first()
