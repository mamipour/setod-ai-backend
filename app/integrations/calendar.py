"""
Google Calendar — same OAuth token as Gmail.
==========================================
Reads and writes the primary calendar. Tokens come from the Google connector;
this module never does its own OAuth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.llm.client import ToolSpec
from app.db.models import Connector
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext
from app.integrations.gmail import valid_token
from sqlmodel.ext.asyncio.session import AsyncSession

CAL_API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
DEFAULT_TZ = "America/Toronto"


async def _call(
    connector: Connector,
    db: AsyncSession,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    token = await valid_token(connector, db)
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method,
            f"{CAL_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            **kwargs,
        )
    if resp.status_code == 401:
        raise IntegrationError("Google rejected the access token — reconnect the account.")
    if resp.status_code == 403:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        if "accessNotConfigured" in resp.text or "has not been used" in detail.lower():
            raise IntegrationError(
                "Calendar API is not enabled on this Google Cloud project. "
                "Enable it, then reconnect."
            )
        raise IntegrationError(f"Calendar API error (403): {detail or resp.text[:200]}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise IntegrationError(f"Calendar API error ({resp.status_code}): {detail}")
    return resp.json() if resp.content else {}


async def primary_timezone(connector: Connector, db: AsyncSession) -> str:
    data = await _call(connector, db, "GET", "/calendars/primary")
    return str(data.get("timeZone") or DEFAULT_TZ)


def _rfc3339(value: str, tz_name: str, *, end_of_day: bool = False) -> str:
    raw = value.strip()
    tz = ZoneInfo(tz_name)
    if "T" in raw:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.isoformat()
    day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz)
    if end_of_day:
        day = day + timedelta(days=1)
    return day.isoformat()


async def list_events(
    connector: Connector,
    db: AsyncSession,
    *,
    date_from: str,
    date_to: str,
    limit: int,
    timezone: str | None = None,
) -> list[dict[str, Any]]:
    tz = timezone or await primary_timezone(connector, db)
    data = await _call(
        connector,
        db,
        "GET",
        "/calendars/primary/events",
        params={
            "timeMin": _rfc3339(date_from, tz),
            "timeMax": _rfc3339(date_to, tz, end_of_day=True),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(limit, 50),
        },
    )
    out = []
    for ev in data.get("items") or []:
        start = ev.get("start") or {}
        end = ev.get("end") or {}
        out.append({
            "id": ev.get("id"),
            "title": ev.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date") or "",
            "end": end.get("dateTime") or end.get("date") or "",
            "location": ev.get("location") or "",
            "attendees": [
                a.get("email") for a in (ev.get("attendees") or []) if a.get("email")
            ],
        })
    return out


async def create_event(
    connector: Connector,
    db: AsyncSession,
    *,
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    tz = timezone or await primary_timezone(connector, db)
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": _rfc3339(start, tz), "timeZone": tz},
        "end": {"dateTime": _rfc3339(end, tz), "timeZone": tz},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees if a]
    return await _call(connector, db, "POST", "/calendars/primary/events", json=body)


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    connector, db = ctx.connector, ctx.db

    async def list_upcoming(args: dict[str, Any], dry_run: bool) -> str:
        today = datetime.now(UTC).date().isoformat()
        date_from = str(args.get("date_from") or today).strip()
        date_to = str(args.get("date_to") or date_from).strip()
        tz = str(args.get("timezone") or "").strip() or None
        limit = min(int(args.get("limit", 20)), 50)
        events = await list_events(
            connector, db, date_from=date_from, date_to=date_to, limit=limit, timezone=tz,
        )
        if not events:
            return f"No events on the primary calendar from {date_from} to {date_to}."
        lines = []
        for i, ev in enumerate(events, 1):
            where = f" @ {ev['location']}" if ev["location"] else ""
            who = f" with {', '.join(ev['attendees'])}" if ev["attendees"] else ""
            lines.append(
                f"{i}. id={ev['id']} {ev['title']!r} {ev['start']} → {ev['end']}{where}{who}"
            )
        return f"{len(events)} event(s):\n" + "\n".join(lines)

    async def create(args: dict[str, Any], dry_run: bool) -> str:
        title = str(args.get("title") or "").strip()
        start = str(args.get("start") or "").strip()
        end = str(args.get("end") or "").strip()
        if not (title and start and end):
            return "Error: title, start, and end are required."
        tz = str(args.get("timezone") or "").strip() or None
        description = str(args.get("description") or "").strip()
        location = str(args.get("location") or "").strip()
        raw_attendees = args.get("attendees")
        attendees: list[str] = []
        if isinstance(raw_attendees, list):
            attendees = [str(a).strip() for a in raw_attendees if str(a).strip()]
        elif isinstance(raw_attendees, str) and raw_attendees.strip():
            attendees = [a.strip() for a in raw_attendees.split(",") if a.strip()]
        if dry_run:
            return f"Would have created {title!r} from {start} to {end}."
        ev = await create_event(
            connector,
            db,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees or None,
            timezone=tz,
        )
        return f"Created event {ev.get('id')} — {title}."

    return [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("list_calendar_events"),
                ctx.describe(
                    "List events on the primary Google Calendar between two dates "
                    "(YYYY-MM-DD). Defaults to today. Times use the calendar's timezone "
                    "unless timezone is set (e.g. America/Toronto, America/Vancouver)."
                ),
                {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "Start date YYYY-MM-DD. Default today."},
                        "date_to": {"type": "string", "description": "End date YYYY-MM-DD. Default date_from."},
                        "timezone": {"type": "string"},
                        "limit": {"type": "integer", "description": "Max events, default 20, cap 50."},
                    },
                },
            ),
            list_upcoming,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("create_calendar_event"),
                ctx.describe(
                    "Create an event on the primary Google Calendar. start and end are "
                    "YYYY-MM-DD or YYYY-MM-DDTHH:MM. Optional attendees receive a Google invite."
                ),
                {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "description": {"type": "string"},
                        "location": {"type": "string"},
                        "attendees": {
                            "type": "string",
                            "description": "Comma-separated email addresses.",
                        },
                        "timezone": {"type": "string"},
                    },
                    "required": ["title", "start", "end"],
                },
            ),
            create,
        ),
    ]
