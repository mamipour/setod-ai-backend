"""Entitlements — resolve what an org is allowed to do.

Priority stack (highest to lowest):
  1. OrgOverride  — manual staff grants with optional expiry
  2. OrgAddon     — purchased add-ons (voice_lite, voice_standard, rows_100k)
  3. OrgSubscription → Plan — the org's active subscription plan
  4. plans('free') — every org that has no subscription row

Gates raise ``EntitlementError`` (→ 402).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Addon, OrgAddon, OrgCap, OrgOverride, OrgSubscription, Plan


class EntitlementError(Exception):
    """Raised when a feature or quota is not available to the org."""

    def __init__(self, feature: str, plan_required: str | None = None, detail: str | None = None):
        self.feature = feature
        self.plan_required = plan_required
        self.detail = detail or f"Feature '{feature}' not available on current plan."
        super().__init__(self.detail)


def _to_http(err: EntitlementError, upgrade_url: str = "/settings/plan") -> HTTPException:
    return HTTPException(
        status_code=402,
        detail={
            "feature": err.feature,
            "plan_required": err.plan_required,
            "upgrade_url": upgrade_url,
            "message": err.detail,
        },
    )


@dataclass
class Entitlements:
    plan_code: str
    features: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    included: dict[str, Any] = field(default_factory=dict)
    caps: dict[str, int] = field(default_factory=dict)
    # Raw override values keyed by override.key
    _overrides: dict[str, str] = field(default_factory=dict)

    def allows(self, feature: str) -> bool:
        """Return True if the feature is enabled (override > plan/addon)."""
        if feature in self._overrides:
            return json.loads(self._overrides[feature])
        return bool(self.features.get(feature, False))

    def limit(self, key: str) -> int:
        """Return numeric limit; -1 means unlimited."""
        if key in self._overrides:
            return int(json.loads(self._overrides[key]))
        return int(self.limits.get(key, 0))

    def included_quantity(self, meter: str) -> float:
        if meter in self._overrides:
            return float(json.loads(self._overrides[meter + "_included"] if meter + "_included" in self._overrides else self._overrides.get(meter, "0")))
        return float(self.included.get(meter, 0.0))

    def hard_cap(self, meter: str) -> int | None:
        return self.caps.get(meter)

    # ── Gates ─────────────────────────────────────────────────────────────────

    def require(self, feature: str, plan_required: str = "pro") -> None:
        if not self.allows(feature):
            raise EntitlementError(feature, plan_required)

    def require_limit(self, key: str, current: int, plan_required: str = "pro") -> None:
        """Raise if ``current`` has already reached the limit (-1 = unlimited)."""
        lim = self.limit(key)
        if lim == -1:
            return
        if current >= lim:
            raise EntitlementError(
                key,
                plan_required,
                f"Limit of {lim} {key} reached. Upgrade to add more.",
            )

    def require_quota(self, meter: str, plan_required: str = "pro") -> None:
        """Raise if the org is over quota for this meter."""
        cap = self.hard_cap(meter)
        if cap is not None and cap <= 0:
            raise EntitlementError(meter, plan_required, f"Hard cap of {cap} {meter} reached.")
        # Soft quota (included) checked elsewhere at run time via usage_periods.
        # Hard caps stored in org_caps prevent all usage.


# ── Resolve ───────────────────────────────────────────────────────────────────

_FREE_PLAN = Plan(
    code="free",
    display_name="Free",
    features={"managed_models": False, "voice": False},
    limits={"agents": 5, "rows": 5000},
    included={},
    monthly_credit_cents=0,
)


async def resolve(db: AsyncSession, org_id: UUID) -> Entitlements:
    """Build an Entitlements object for the org from the DB."""

    # 1. Base plan
    sub = (await db.exec(
        select(OrgSubscription).where(OrgSubscription.org_id == org_id)
    )).first()

    # Grace period: past_due orgs keep their plan for up to 3 days
    GRACE_DAYS = 3
    if sub and sub.status not in ("cancelled",):
        if sub.status == "past_due" and sub.current_period_end:
            grace_cutoff = sub.current_period_end + __import__("datetime").timedelta(days=GRACE_DAYS)
            if datetime.now(UTC) > grace_cutoff:
                plan = _FREE_PLAN
            else:
                plan = await db.get(Plan, sub.plan_code) or _FREE_PLAN
        else:
            plan = await db.get(Plan, sub.plan_code) or _FREE_PLAN
    else:
        plan = _FREE_PLAN

    features: dict[str, Any] = dict(plan.features or {})
    limits: dict[str, Any] = dict(plan.limits or {})
    included: dict[str, Any] = dict(plan.included or {})

    # 2. Add-ons — only active rows contribute; voice_minutes uses prorated snapshot when set
    addon_rows = (await db.exec(
        select(OrgAddon).where(OrgAddon.org_id == org_id, OrgAddon.status == "active")
    )).all()
    now_dt = datetime.now(UTC)
    for oa in addon_rows:
        addon = await db.get(Addon, oa.addon_code)
        if addon and addon.active:
            features.update(addon.features or {})
            for meter, qty in (addon.included or {}).items():
                if meter == "voice_minutes":
                    # Use prorated snapshot for the period the add-on was activated mid-period
                    if (
                        oa.included_snapshot is not None
                        and oa.snapshot_period_end
                        and now_dt < oa.snapshot_period_end
                    ):
                        qty = oa.included_snapshot
                included[meter] = included.get(meter, 0) + qty

    # 3. Overrides
    now = datetime.now(UTC)
    override_rows = (await db.exec(
        select(OrgOverride).where(
            OrgOverride.org_id == org_id,
        )
    )).all()
    raw_overrides: dict[str, str] = {}
    for ov in override_rows:
        if ov.expires_at and ov.expires_at < now:
            continue
        raw_overrides[ov.key] = ov.value

    # 4. Hard caps
    cap_rows = (await db.exec(
        select(OrgCap).where(OrgCap.org_id == org_id)
    )).all()
    caps = {c.meter: c.hard_cap for c in cap_rows}

    return Entitlements(
        plan_code=plan.code,
        features=features,
        limits=limits,
        included=included,
        caps=caps,
        _overrides=raw_overrides,
    )
