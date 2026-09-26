"""
Calendly integration
====================
Auth: Personal Access Token (PAT).
  Generate at: calendly.com → Integrations → API & Webhooks → Personal Access Tokens.
  Required scopes (must be enabled when creating the PAT):
    event_types:read, scheduled_events:read, scheduled_events:write,
    invitees:write, scheduling_links:write
  Legacy PATs (created before scoped permissions were introduced) have full access by default.

Header: ``Authorization: Bearer {token}``
Base URL: ``https://api.calendly.com``

Almost every endpoint requires the current user's URI — we resolve it once per
tool invocation via ``GET /users/me`` and pass it through.

Tools (7):
  list_calendly_event_types()
  get_calendly_availability(event_type_uri, start_date?, end_date?)
  list_calendly_events(status?, days_ahead?)
  get_calendly_event(event_uri)
  create_calendly_booking(event_type_uri, start_time_utc, name, email, timezone?)
  cancel_calendly_event(event_uri, reason?)
  create_scheduling_link(event_type_uri)

All write tools honour dry_run.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_BASE = "https://api.calendly.com"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _uuid_from_uri(uri: str) -> str:
    """Extract UUID from a Calendly resource URI like .../event_types/XXXXX."""
    return uri.rstrip("/").split("/")[-1]


def _fmt_time(iso: str) -> str:
    """Convert UTC ISO string to readable format: 'Mon Sep 29 at 2:00 PM UTC'."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a %b %-d at %-I:%M %p UTC")
    except Exception:
        return iso


async def _get_user_uri(token: str) -> tuple[str, str, str]:
    """Returns (user_uri, name, email)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_BASE}/users/me", headers=_headers(token))
    if resp.status_code == 401:
        raise IntegrationError("Invalid or expired Calendly token")
    if resp.status_code == 403:
        raise IntegrationError(
            "Token lacks required scopes. Regenerate the PAT with: "
            "event_types:read, scheduled_events:read, scheduled_events:write, "
            "invitees:write, scheduling_links:write"
        )
    if resp.status_code != 200:
        raise IntegrationError(f"Calendly error {resp.status_code}: {resp.text[:200]}")
    resource = resp.json()["resource"]
    return resource["uri"], resource["name"], resource["email"]


async def test_connection(token: str) -> str:
    uri, name, email = await _get_user_uri(token)
    return f"Connected as {name} ({email})"


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    token: str = raw["api_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    # ── list_calendly_event_types ─────────────────────────────────────────────

    async def list_calendly_event_types(args: dict[str, Any], dry_run: bool) -> str:
        user_uri, _, _ = await _get_user_uri(token)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/event_types",
                headers=_headers(token),
                params={"user": user_uri, "active": "true", "count": 50},
            )
        if resp.status_code != 200:
            raise IntegrationError(f"Calendly error {resp.status_code}: {resp.text[:300]}")
        items = resp.json().get("collection", [])
        if not items:
            return "No active event types found."
        lines = []
        for et in items:
            duration = et.get("duration", "?")
            name = et.get("name", "?")
            link = et.get("scheduling_url", "")
            uri = et.get("uri", "")
            slug = _uuid_from_uri(uri)
            lines.append(f"• {name} ({duration} min) — {link} | uri: {slug}")
        return "\n".join(lines)

    # ── get_calendly_availability ─────────────────────────────────────────────

    async def get_calendly_availability(args: dict[str, Any], dry_run: bool) -> str:
        event_type_uri = str(args.get("event_type_uri", "")).strip()
        if not event_type_uri:
            return "Error: 'event_type_uri' is required. Use list_calendly_event_types to get URIs."
        # Accept both short slug and full URI
        if not event_type_uri.startswith("http"):
            event_type_uri = f"{_BASE}/event_types/{event_type_uri}"

        # Date range — default next 7 days
        now = datetime.now(UTC)
        start_date = str(args.get("start_date", "")).strip()
        end_date = str(args.get("end_date", "")).strip()
        try:
            start_dt = datetime.fromisoformat(start_date).replace(tzinfo=UTC) if start_date else now
            end_dt = datetime.fromisoformat(end_date).replace(tzinfo=UTC) if end_date else now + timedelta(days=7)
        except ValueError:
            return "Error: dates must be in YYYY-MM-DD or ISO format."

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/event_type_available_times",
                headers=_headers(token),
                params={
                    "event_type": event_type_uri,
                    "start_time": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_time": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
        if resp.status_code == 404:
            return "Event type not found. Use list_calendly_event_types to get valid URIs."
        if resp.status_code != 200:
            raise IntegrationError(f"Calendly error {resp.status_code}: {resp.text[:300]}")
        slots = resp.json().get("collection", [])
        if not slots:
            return f"No available slots between {start_dt.date()} and {end_dt.date()}."
        lines = [f"Available slots ({len(slots)} found):"]
        for slot in slots[:20]:  # cap display at 20
            lines.append(f"  • {_fmt_time(slot['start_time'])} (until {_fmt_time(slot['end_time'])})")
        if len(slots) > 20:
            lines.append(f"  … and {len(slots) - 20} more")
        lines.append(f"\nEvent type URI: {event_type_uri}")
        return "\n".join(lines)

    # ── list_calendly_events ──────────────────────────────────────────────────

    async def list_calendly_events(args: dict[str, Any], dry_run: bool) -> str:
        status = str(args.get("status", "active")).strip().lower()
        if status not in ("active", "canceled"):
            status = "active"
        days_ahead = min(int(args.get("days_ahead", 7)), 90)
        user_uri, _, _ = await _get_user_uri(token)
        now = datetime.now(UTC)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/scheduled_events",
                headers=_headers(token),
                params={
                    "user": user_uri,
                    "status": status,
                    "min_start_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "max_start_time": (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sort": "start_time:asc",
                    "count": 25,
                },
            )
        if resp.status_code != 200:
            raise IntegrationError(f"Calendly error {resp.status_code}: {resp.text[:300]}")
        events = resp.json().get("collection", [])
        if not events:
            return f"No {status} events in the next {days_ahead} day(s)."
        lines = []
        for ev in events:
            name = ev.get("name", "Meeting")
            start = _fmt_time(ev.get("start_time", ""))
            ev_uuid = _uuid_from_uri(ev.get("uri", ""))
            lines.append(f"• {start} — {name} (uuid: {ev_uuid})")
        return "\n".join(lines)

    # ── get_calendly_event ────────────────────────────────────────────────────

    async def get_calendly_event(args: dict[str, Any], dry_run: bool) -> str:
        event_ref = str(args.get("event_uri", "")).strip()
        if not event_ref:
            return "Error: 'event_uri' is required."
        if not event_ref.startswith("http"):
            event_ref = f"{_BASE}/scheduled_events/{event_ref}"
        ev_uuid = _uuid_from_uri(event_ref)

        async with httpx.AsyncClient(timeout=15) as client:
            ev_resp, inv_resp = await __import__("asyncio").gather(
                client.get(f"{_BASE}/scheduled_events/{ev_uuid}", headers=_headers(token)),
                client.get(f"{_BASE}/scheduled_events/{ev_uuid}/invitees", headers=_headers(token)),
            )
        if ev_resp.status_code == 404:
            return "Event not found."
        if ev_resp.status_code != 200:
            raise IntegrationError(f"Calendly error {ev_resp.status_code}: {ev_resp.text[:300]}")

        ev = ev_resp.json()["resource"]
        lines = [
            f"Event: {ev.get('name', '?')}",
            f"Status: {ev.get('status', '?')}",
            f"Start: {_fmt_time(ev.get('start_time', ''))} → {_fmt_time(ev.get('end_time', ''))}",
        ]
        loc = ev.get("location", {})
        if loc.get("join_url"):
            lines.append(f"Link: {loc['join_url']}")
        elif loc.get("location"):
            lines.append(f"Location: {loc['location']}")

        if inv_resp.status_code == 200:
            invitees = inv_resp.json().get("collection", [])
            for inv in invitees:
                lines.append(
                    f"Invitee: {inv.get('name', '?')} <{inv.get('email', '?')}>"
                    + (f" — cancel: {inv.get('cancel_url', '')}" if inv.get("cancel_url") else "")
                    + (f" | reschedule: {inv.get('reschedule_url', '')}" if inv.get("reschedule_url") else "")
                )
        return "\n".join(lines)

    # ── create_calendly_booking ───────────────────────────────────────────────

    async def create_calendly_booking(args: dict[str, Any], dry_run: bool) -> str:
        event_type_uri = str(args.get("event_type_uri", "")).strip()
        start_time = str(args.get("start_time_utc", "")).strip()
        name = str(args.get("name", "")).strip()
        email = str(args.get("email", "")).strip()
        timezone = str(args.get("timezone", "UTC")).strip() or "UTC"

        if not event_type_uri or not start_time or not name or not email:
            return "Error: 'event_type_uri', 'start_time_utc', 'name', and 'email' are required."
        if not event_type_uri.startswith("http"):
            event_type_uri = f"{_BASE}/event_types/{event_type_uri}"

        # Normalise time to UTC Z suffix
        start_time = re.sub(r"\+00:00$", "Z", start_time)
        if not start_time.endswith("Z"):
            start_time += "Z"

        if dry_run:
            return (
                f"[simulated] Would book '{event_type_uri}' at {start_time} "
                f"for {name} <{email}> (tz: {timezone})"
            )
        payload = {
            "event_type": event_type_uri,
            "start_time": start_time,
            "invitee": {"name": name, "email": email, "timezone": timezone},
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{_BASE}/invitees", headers=_headers(token), json=payload)
        if resp.status_code == 403:
            return (
                "Booking requires a paid Calendly plan (Professional or above). "
                "Upgrade your Calendly account or use create_scheduling_link to send "
                "the invitee a link they can book themselves."
            )
        if resp.status_code == 409:
            return "This time slot is no longer available. Use get_calendly_availability to find an open slot."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Calendly booking error {resp.status_code}: {resp.text[:300]}")
        resource = resp.json()["resource"]
        inv = resource.get("invitee", {})
        ev = resource.get("event", {})
        cancel_url = inv.get("cancel_url", "")
        reschedule_url = inv.get("reschedule_url", "")
        lines = [
            f"Booking confirmed for {name} <{email}>",
            f"Event URI: {_uuid_from_uri(ev.get('uri', ''))}",
        ]
        if cancel_url:
            lines.append(f"Cancel: {cancel_url}")
        if reschedule_url:
            lines.append(f"Reschedule: {reschedule_url}")
        return "\n".join(lines)

    # ── cancel_calendly_event ─────────────────────────────────────────────────

    async def cancel_calendly_event(args: dict[str, Any], dry_run: bool) -> str:
        event_ref = str(args.get("event_uri", "")).strip()
        reason = str(args.get("reason", "")).strip()
        if not event_ref:
            return "Error: 'event_uri' is required."
        if not event_ref.startswith("http"):
            event_ref = f"{_BASE}/scheduled_events/{event_ref}"
        ev_uuid = _uuid_from_uri(event_ref)
        if dry_run:
            return f"[simulated] Would cancel event {ev_uuid}" + (f" (reason: {reason})" if reason else "")
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/scheduled_events/{ev_uuid}/cancellation",
                headers=_headers(token),
                json=payload,
            )
        if resp.status_code == 404:
            return "Event not found."
        if resp.status_code == 409:
            return "Event is already cancelled."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Calendly error {resp.status_code}: {resp.text[:300]}")
        return f"Event {ev_uuid} cancelled. All invitees will be notified by Calendly."

    # ── create_scheduling_link ────────────────────────────────────────────────

    async def create_scheduling_link(args: dict[str, Any], dry_run: bool) -> str:
        event_type_uri = str(args.get("event_type_uri", "")).strip()
        if not event_type_uri:
            return "Error: 'event_type_uri' is required. Use list_calendly_event_types to get URIs."
        if not event_type_uri.startswith("http"):
            event_type_uri = f"{_BASE}/event_types/{event_type_uri}"
        if dry_run:
            return f"[simulated] Would create a single-use scheduling link for event type {_uuid_from_uri(event_type_uri)}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/scheduling_links",
                headers=_headers(token),
                json={"max_event_count": 1, "owner": event_type_uri, "owner_type": "EventType"},
            )
        if resp.status_code == 403:
            return "Creating scheduling links requires a paid Calendly plan."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Calendly error {resp.status_code}: {resp.text[:300]}")
        booking_url = resp.json()["resource"]["booking_url"]
        return f"Scheduling link (single-use): {booking_url}"

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_calendly_event_types"),
                description=(
                    "List all active Calendly event types (meeting templates) for this account. "
                    "Returns name, duration, scheduling URL, and URI for each type. "
                    "Use the URI with other tools."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_calendly_event_types,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_calendly_availability"),
                description=(
                    "Get available time slots for a Calendly event type. "
                    "Returns up to 20 open slots within the date range (default: next 7 days). "
                    "Use list_calendly_event_types first to get the event_type_uri."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_type_uri": {"type": "string", "description": "Full event type URI or UUID slug."},
                        "start_date": {"type": "string", "description": "Start of range (YYYY-MM-DD or ISO). Default: today."},
                        "end_date": {"type": "string", "description": "End of range (YYYY-MM-DD or ISO). Default: 7 days from today."},
                    },
                    "required": ["event_type_uri"],
                },
            ),
            handler=get_calendly_availability,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_calendly_events"),
                description=(
                    "List upcoming scheduled events on this Calendly account. "
                    "Filter by status (active or canceled) and how many days ahead to look. "
                    "Returns event name, start time, and UUID."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "active (default) or canceled."},
                        "days_ahead": {"type": "integer", "description": "How many days ahead to look (default 7, max 90)."},
                    },
                    "required": [],
                },
            ),
            handler=list_calendly_events,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_calendly_event"),
                description=(
                    "Get full details for a single scheduled Calendly event: "
                    "invitee name, email, meeting link, cancel/reschedule URLs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_uri": {"type": "string", "description": "Event UUID or full URI (from list_calendly_events)."},
                    },
                    "required": ["event_uri"],
                },
            ),
            handler=get_calendly_event,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_calendly_booking"),
                description=(
                    "Book a Calendly meeting programmatically on behalf of an invitee. "
                    "Requires a paid Calendly plan (Professional or above). "
                    "If on a free plan, use create_scheduling_link instead. "
                    "Use get_calendly_availability first to confirm the time slot is open."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_type_uri": {"type": "string", "description": "Full event type URI or UUID slug."},
                        "start_time_utc": {"type": "string", "description": "Start time in UTC ISO format (e.g. 2026-09-29T14:00:00Z)."},
                        "name": {"type": "string", "description": "Invitee's full name."},
                        "email": {"type": "string", "description": "Invitee's email address."},
                        "timezone": {"type": "string", "description": "Invitee's timezone (e.g. America/New_York). Default: UTC."},
                    },
                    "required": ["event_type_uri", "start_time_utc", "name", "email"],
                },
            ),
            handler=create_calendly_booking,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("cancel_calendly_event"),
                description=(
                    "Cancel a scheduled Calendly event. "
                    "Calendly will automatically notify all invitees. "
                    "Use list_calendly_events to get the event UUID."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_uri": {"type": "string", "description": "Event UUID or full URI."},
                        "reason": {"type": "string", "description": "Optional cancellation reason (shown to invitees)."},
                    },
                    "required": ["event_uri"],
                },
            ),
            handler=cancel_calendly_event,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_scheduling_link"),
                description=(
                    "Generate a single-use Calendly scheduling link for a specific event type. "
                    "Send this link to an invitee in a message — once used it expires. "
                    "Works on all paid plans. Use list_calendly_event_types to get the URI."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "event_type_uri": {"type": "string", "description": "Full event type URI or UUID slug."},
                    },
                    "required": ["event_type_uri"],
                },
            ),
            handler=create_scheduling_link,
        ),
    ]
