"""
Google Business Profile integration
=====================================
Auth: Google OAuth 2.0 — scope ``https://www.googleapis.com/auth/business.manage``.
The OAuth flow is handled by dedicated start/callback routes in the connectors router.
The access_token (and refresh handling) is stored in connector.config.

Separate connector from Gmail — same Google Cloud Client ID, different scope.
The user's Google account must be an owner or manager of the GBP location,
and the location must be verified on Google.

Tools (4):
  list_gbp_locations()
  list_gbp_reviews(location_id, limit?)
  reply_to_gbp_review(location_id, review_id, comment)
  delete_gbp_reply(location_id, review_id)

All write tools honour dry_run.
"""

import asyncio
from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_GBP_BASE = "https://mybusiness.googleapis.com/v4"
_ACCOUNTS_BASE = "https://mybusinessaccountmanagement.googleapis.com/v1"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _get_account_id(token: str) -> str:
    """Resolve the first Google Business Profile account id."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_ACCOUNTS_BASE}/accounts", headers=_headers(token))
    if resp.status_code == 401:
        raise IntegrationError("GBP token expired. Please reconnect your Google Business Profile.")
    if resp.status_code != 200:
        raise IntegrationError(f"GBP accounts error {resp.status_code}: {resp.text[:300]}")
    accounts = resp.json().get("accounts", [])
    if not accounts:
        raise IntegrationError(
            "No Google Business Profile accounts found. "
            "Make sure this Google account is an owner or manager of a GBP location."
        )
    return accounts[0]["name"]  # e.g. "accounts/123456789"


async def test_connection(token: str) -> str:
    account_name = await _get_account_id(token)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_GBP_BASE}/{account_name}/locations?pageSize=3",
            headers=_headers(token),
        )
    if resp.status_code != 200:
        raise IntegrationError(f"GBP locations error {resp.status_code}: {resp.text[:300]}")
    locations = resp.json().get("locations", [])
    if not locations:
        return "Connected — no verified locations found on this account yet"
    names = [loc.get("locationName", "?") for loc in locations[:3]]
    return "Connected — locations: " + ", ".join(names)


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    token: str = raw["access_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    async def _account() -> str:
        return await _get_account_id(token)

    # ── list_gbp_locations ────────────────────────────────────────────────────

    async def list_gbp_locations(args: dict[str, Any], dry_run: bool) -> str:
        account = await _account()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GBP_BASE}/{account}/locations?pageSize=20",
                headers=_headers(token),
            )
        if resp.status_code != 200:
            raise IntegrationError(f"GBP error {resp.status_code}: {resp.text[:300]}")
        locations = resp.json().get("locations", [])
        if not locations:
            return "No locations found. Location must be verified on Google."
        lines = []
        for loc in locations:
            loc_id = loc.get("name", "").split("/")[-1]
            loc_name = loc.get("locationName", "?")
            addr = loc.get("address", {})
            city = addr.get("locality", "")
            lines.append(f"• {loc_name} (location_id: {loc_id})" + (f" — {city}" if city else ""))
        return "\n".join(lines)

    # ── list_gbp_reviews ──────────────────────────────────────────────────────

    def _star_label(rating: str) -> str:
        stars = {"ONE": "⭐", "TWO": "⭐⭐", "THREE": "⭐⭐⭐", "FOUR": "⭐⭐⭐⭐", "FIVE": "⭐⭐⭐⭐⭐"}
        return stars.get(rating, rating)

    async def list_gbp_reviews(args: dict[str, Any], dry_run: bool) -> str:
        account = await _account()
        location_id = str(args.get("location_id", "")).strip()
        limit = min(int(args.get("limit", 10)), 50)
        if not location_id:
            return "Error: 'location_id' is required. Use list_gbp_locations to get the id."
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GBP_BASE}/{account}/locations/{location_id}/reviews"
                f"?pageSize={limit}&orderBy=updateTime desc",
                headers=_headers(token),
            )
        if resp.status_code == 404:
            return f"Location {location_id} not found or not accessible."
        if resp.status_code != 200:
            raise IntegrationError(f"GBP reviews error {resp.status_code}: {resp.text[:300]}")
        reviews = resp.json().get("reviews", [])
        if not reviews:
            return "No reviews found."
        lines = []
        for rv in reviews:
            reviewer = rv.get("reviewer", {}).get("displayName", "Anonymous")
            rating = _star_label(rv.get("starRating", ""))
            comment = (rv.get("comment") or "").strip()[:200]
            review_id = rv.get("reviewId", "")
            has_reply = bool(rv.get("reviewReply"))
            replied = " [replied]" if has_reply else " [no reply]"
            lines.append(f"{rating} — {reviewer}{replied} (id: {review_id})")
            if comment:
                lines.append(f"  \"{comment}\"")
        return "\n".join(lines)

    # ── reply_to_gbp_review ───────────────────────────────────────────────────

    async def reply_to_gbp_review(args: dict[str, Any], dry_run: bool) -> str:
        account = await _account()
        location_id = str(args.get("location_id", "")).strip()
        review_id = str(args.get("review_id", "")).strip()
        comment = str(args.get("comment", "")).strip()
        if not location_id or not review_id or not comment:
            return "Error: 'location_id', 'review_id', and 'comment' are required."
        if len(comment) > 4096:
            return "Error: Reply must be ≤ 4096 characters."
        if dry_run:
            return f"[simulated] Would reply to review {review_id} on location {location_id}: {comment[:80]}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                f"{_GBP_BASE}/{account}/locations/{location_id}/reviews/{review_id}/reply",
                headers={**_headers(token), "Content-Type": "application/json"},
                json={"comment": comment},
            )
        if resp.status_code == 404:
            return "Review not found. Use list_gbp_reviews to get valid review ids."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"GBP reply error {resp.status_code}: {resp.text[:300]}")
        return f"Reply posted to review {review_id}."

    # ── delete_gbp_reply ──────────────────────────────────────────────────────

    async def delete_gbp_reply(args: dict[str, Any], dry_run: bool) -> str:
        account = await _account()
        location_id = str(args.get("location_id", "")).strip()
        review_id = str(args.get("review_id", "")).strip()
        if not location_id or not review_id:
            return "Error: 'location_id' and 'review_id' are required."
        if dry_run:
            return f"[simulated] Would delete reply to review {review_id} on location {location_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{_GBP_BASE}/{account}/locations/{location_id}/reviews/{review_id}/reply",
                headers=_headers(token),
            )
        if resp.status_code == 404:
            return "Reply not found — it may have already been deleted."
        if resp.status_code not in (200, 204):
            raise IntegrationError(f"GBP error {resp.status_code}: {resp.text[:300]}")
        return f"Reply to review {review_id} deleted."

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_gbp_locations"),
                description=(
                    "List all Google Business Profile locations on this account. "
                    "Returns location names and ids. Use the id with other GBP tools."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_gbp_locations,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_gbp_reviews"),
                description=(
                    "List recent Google Business Profile reviews for a location. "
                    "Returns star rating, reviewer name, comment, and whether a reply exists. "
                    "Use list_gbp_locations first to get the location_id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "location_id": {"type": "string", "description": "GBP location id (from list_gbp_locations)."},
                        "limit": {"type": "integer", "description": "Max reviews to return (default 10, max 50)."},
                    },
                    "required": ["location_id"],
                },
            ),
            handler=list_gbp_reviews,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("reply_to_gbp_review"),
                description=(
                    "Post a reply to a Google Business Profile review. "
                    "Creates the reply if none exists, or updates an existing one. "
                    "Reply must be ≤ 4096 characters. Use a professional, helpful tone."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "location_id": {"type": "string", "description": "GBP location id."},
                        "review_id": {"type": "string", "description": "Review id (from list_gbp_reviews)."},
                        "comment": {"type": "string", "description": "Reply text (≤ 4096 characters)."},
                    },
                    "required": ["location_id", "review_id", "comment"],
                },
            ),
            handler=reply_to_gbp_review,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("delete_gbp_reply"),
                description=(
                    "Delete your existing reply to a Google Business Profile review. "
                    "Use this to remove an incorrect or outdated reply before posting a corrected one."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "location_id": {"type": "string", "description": "GBP location id."},
                        "review_id": {"type": "string", "description": "Review id."},
                    },
                    "required": ["location_id", "review_id"],
                },
            ),
            handler=delete_gbp_reply,
        ),
    ]
