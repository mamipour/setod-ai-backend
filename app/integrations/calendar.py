"""
Google Calendar connector — CalDAV via App Password
====================================================
Uses the same connector as Gmail (same email + App Password).
Connects to Google's CalDAV endpoint which accepts App Passwords,
so no OAuth or Google Cloud Console review is required.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta, date
from typing import Any
from zoneinfo import ZoneInfo

from app.core.llm.client import ToolSpec
from app.db.models import Connector
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext
from app.integrations.gmail import _cfg

DEFAULT_TZ = "America/Toronto"

# Google's CalDAV endpoint — this URL accepts App Passwords.
# The newer apidata.googleusercontent.com endpoint requires OAuth tokens.
_CALDAV_BASE = "https://www.google.com/calendar/dav/{email}/events/"


# ── CalDAV sync primitives ────────────────────────────────────────────────────

def _client(email_addr: str, app_password: str):  # type: ignore[return]
    try:
        import caldav
    except ImportError as exc:
        raise IntegrationError(
            "caldav library not installed. Run: pip install caldav"
        ) from exc
    return caldav.DAVClient(
        url=_CALDAV_BASE.format(email=email_addr),
        username=email_addr,
        password=app_password,
    )


def _primary_calendar(email_addr: str, app_password: str):
    """Return the primary calendar object for this account."""
    try:
        import caldav
        client = _client(email_addr, app_password)
        # The events/ URL directly points to the primary calendar — no principal discovery needed.
        return caldav.Calendar(client=client, url=_CALDAV_BASE.format(email=email_addr))
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(
            f"Could not connect to Google Calendar — check the App Password. ({exc})"
        ) from exc


def _to_datetime(value: str, tz_name: str, *, end_of_day: bool = False) -> datetime:
    """Parse YYYY-MM-DD or YYYY-MM-DDTHH:MM to a timezone-aware datetime."""
    tz = ZoneInfo(tz_name)
    raw = value.strip()
    if "T" in raw or " " in raw:
        raw = raw.replace(" ", "T")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz)
    if end_of_day:
        day += timedelta(days=1)
    return day


def _event_to_dict(event) -> dict[str, Any]:
    """Convert a caldav Event to a plain dict."""
    try:
        comp = event.icalendar_component
    except Exception:
        return {}

    def _dt_str(prop_name: str) -> str:
        prop = comp.get(prop_name)
        if prop is None:
            return ""
        val = prop.dt
        if isinstance(val, datetime):
            return val.isoformat()
        if isinstance(val, date):
            return val.isoformat()
        return str(val)

    attendees = []
    raw_att = comp.get("ATTENDEE")
    if raw_att:
        if not isinstance(raw_att, list):
            raw_att = [raw_att]
        for a in raw_att:
            email_val = str(a).replace("mailto:", "")
            if email_val:
                attendees.append(email_val)

    return {
        "id": str(comp.get("UID", "")),
        "title": str(comp.get("SUMMARY", "(no title)")),
        "start": _dt_str("DTSTART"),
        "end": _dt_str("DTEND"),
        "location": str(comp.get("LOCATION", "")),
        "description": str(comp.get("DESCRIPTION", "")),
        "attendees": attendees,
    }


def _list_events_sync(
    email_addr: str,
    app_password: str,
    date_from: str,
    date_to: str,
    limit: int,
    tz_name: str,
) -> list[dict[str, Any]]:
    calendar = _primary_calendar(email_addr, app_password)
    start = _to_datetime(date_from, tz_name)
    end = _to_datetime(date_to, tz_name, end_of_day=True)
    try:
        events = calendar.date_search(start=start, end=end, expand=True)
    except Exception as exc:
        raise IntegrationError(f"Calendar search failed: {exc}") from exc
    results = [d for e in events[:limit] if (d := _event_to_dict(e))]
    # Sort by start time
    results.sort(key=lambda e: e.get("start", ""))
    return results


def _create_event_sync(
    email_addr: str,
    app_password: str,
    title: str,
    start: str,
    end: str,
    description: str,
    location: str,
    attendees: list[str],
    tz_name: str,
) -> dict[str, Any]:
    try:
        import icalendar
    except ImportError as exc:
        raise IntegrationError("icalendar library not installed (install caldav which pulls it in)") from exc

    calendar = _primary_calendar(email_addr, app_password)
    tz = ZoneInfo(tz_name)

    start_dt = _to_datetime(start, tz_name)
    end_dt = _to_datetime(end, tz_name)

    cal = icalendar.Calendar()
    cal.add("PRODID", "-//Setod//Setod//EN")
    cal.add("VERSION", "2.0")

    event = icalendar.Event()
    event.add("SUMMARY", title)
    event.add("DTSTART", start_dt)
    event.add("DTEND", end_dt)
    uid = str(uuid.uuid4())
    event.add("UID", uid)
    event.add("DTSTAMP", datetime.now(UTC))
    if description:
        event.add("DESCRIPTION", description)
    if location:
        event.add("LOCATION", location)
    for att in attendees:
        attendee = icalendar.vCalAddress(f"mailto:{att}")
        attendee.params["CN"] = att
        event.add("ATTENDEE", attendee)

    cal.add_component(event)
    try:
        calendar.save_event(cal.to_ical().decode())
    except Exception as exc:
        raise IntegrationError(f"Failed to create calendar event: {exc}") from exc
    return {"id": uid, "title": title}


def _primary_tz_sync(email_addr: str, app_password: str) -> str:
    """Return the primary calendar's timezone string."""
    try:
        calendar = _primary_calendar(email_addr, app_password)
        # Try to read VTIMEZONE from the calendar object
        tz = getattr(calendar, "get_supported_components", lambda: None)()
        # Fallback: read from calendar properties
        props = calendar.get_properties()
        for key in props:
            if "calendar-timezone" in str(key).lower():
                return str(props[key]) or DEFAULT_TZ
        return DEFAULT_TZ
    except Exception:
        return DEFAULT_TZ


# ── Async wrappers ─────────────────────────────────────────────────────────────

async def list_events(
    connector: Connector,
    date_from: str,
    date_to: str,
    limit: int,
    timezone: str | None = None,
) -> list[dict[str, Any]]:
    email_addr, pwd = _cfg(connector)
    tz = timezone or DEFAULT_TZ
    return await asyncio.to_thread(
        _list_events_sync, email_addr, pwd, date_from, date_to, limit, tz
    )


async def create_event(
    connector: Connector,
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    email_addr, pwd = _cfg(connector)
    tz = timezone or DEFAULT_TZ
    return await asyncio.to_thread(
        _create_event_sync,
        email_addr, pwd, title, start, end, description, location, attendees or [], tz,
    )


# ── Tools ──────────────────────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    connector = ctx.connector

    async def list_upcoming(args: dict[str, Any], dry_run: bool) -> str:
        today = datetime.now(UTC).date().isoformat()
        date_from = str(args.get("date_from") or today).strip()
        date_to = str(args.get("date_to") or date_from).strip()
        tz = str(args.get("timezone") or "").strip() or None
        limit = min(int(args.get("limit", 20)), 50)
        events = await list_events(connector, date_from, date_to, limit, tz)
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
            connector, title, start, end, description, location, attendees or None, tz
        )
        return f"Created event {ev.get('id')} — {title}."

    return [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("list_calendar_events"),
                ctx.describe(
                    "List events on the primary Google Calendar between two dates (YYYY-MM-DD). "
                    "Defaults to today. Times use the calendar's timezone unless timezone is set "
                    "(e.g. America/Toronto, America/Vancouver)."
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
