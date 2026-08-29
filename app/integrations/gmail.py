"""
Google connector (Gmail + Calendar)
===================================
One OAuth token. Mail lives here; Calendar tools are appended from
`app.integrations.calendar` so token refresh stays in one place.

Gmail has no delete: removing the INBOX label is what archiving means, which is why
`archive_email` modifies labels rather than calling a delete endpoint.
"""

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx

from app.config import settings
from app.core.crypto import decrypt_json, encrypt_json
from app.core.llm.client import ToolSpec
from app.db.models import Connector, ConnectorStatus
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext
from sqlmodel.ext.asyncio.session import AsyncSession

API = "https://gmail.googleapis.com/gmail/v1/users/me"

REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/calendar.events",
}


# ── Token management ───────────────────────────────────────────────────────────

async def refresh_token(connector: Connector, db: AsyncSession) -> dict[str, Any]:
    """Exchange the stored refresh token for a fresh access token.

    Marks the connector revoked if Google refuses, which is the signal the UI uses to tell
    the user to reconnect rather than silently failing every run from then on.
    """
    config = decrypt_json(connector.config)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": config["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=15,
        )

    if resp.status_code != 200:
        connector.status = ConnectorStatus.revoked
        connector.updated_at = datetime.now(UTC)
        db.add(connector)
        await db.commit()
        raise IntegrationError("Gmail access was revoked — reconnect the account.")

    data = resp.json()
    config["access_token"] = data["access_token"]
    config["token_expires_at"] = datetime.now(UTC).timestamp() + data.get("expires_in", 3600)
    connector.config = encrypt_json(config)
    connector.status = ConnectorStatus.active
    connector.updated_at = datetime.now(UTC)
    db.add(connector)
    await db.commit()
    return config


async def valid_token(connector: Connector, db: AsyncSession) -> str:
    """A usable access token, refreshed if it is within 60s of expiring."""
    config = decrypt_json(connector.config)
    if datetime.now(UTC).timestamp() >= config.get("token_expires_at", 0) - 60:
        config = await refresh_token(connector, db)
    return config["access_token"]


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
            f"{API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            **kwargs,
        )
    if resp.status_code == 401:
        raise IntegrationError("Gmail rejected the access token — reconnect the account.")
    if resp.status_code >= 400:
        detail = resp.json().get("error", {}).get("message", resp.text[:200])
        raise IntegrationError(f"Gmail API error ({resp.status_code}): {detail}")
    return resp.json() if resp.content else {}


# ── Operations ─────────────────────────────────────────────────────────────────

async def get_profile(connector: Connector, db: AsyncSession) -> dict[str, Any]:
    return await _call(connector, db, "GET", "/profile")


async def check_scopes(connector: Connector, db: AsyncSession) -> set[str]:
    """Scopes actually granted, so the UI can warn before an agent fails mid-run."""
    token = await valid_token(connector, db)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/tokeninfo",
            params={"access_token": token},
            timeout=15,
        )
    if resp.status_code != 200:
        return set()
    return set(resp.json().get("scope", "").split())


async def list_messages(
    connector: Connector, db: AsyncSession, query: str, limit: int
) -> list[dict[str, Any]]:
    """Message ids matching a Gmail search query. Metadata comes from `get_message`."""
    data = await _call(
        connector, db, "GET", "/messages",
        params={"q": query, "maxResults": min(limit, 50)},
    )
    return data.get("messages", [])


async def get_message(connector: Connector, db: AsyncSession, message_id: str) -> dict[str, Any]:
    """Headers plus a text snippet — enough to triage without pulling whole attachments."""
    data = await _call(
        connector, db, "GET", f"/messages/{message_id}",
        params={
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Subject", "Date"],
        },
    )
    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    return {
        "id": data.get("id"),
        "thread_id": data.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", "(no subject)"),
        "date": headers.get("date", ""),
        "snippet": data.get("snippet", ""),
        "labels": data.get("labelIds", []),
    }


async def send_message(
    connector: Connector,
    db: AsyncSession,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    payload: dict[str, Any] = {
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()
    }
    if thread_id:
        payload["threadId"] = thread_id

    return await _call(connector, db, "POST", "/messages/send", json=payload)


async def archive_message(connector: Connector, db: AsyncSession, message_id: str) -> None:
    """Gmail has no archive endpoint — removing INBOX is what archiving is."""
    await _call(
        connector, db, "POST", f"/messages/{message_id}/modify",
        json={"removeLabelIds": ["INBOX"]},
    )


# ── Tools ──────────────────────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    connector, db = ctx.connector, ctx.db

    async def read_unread(args: dict[str, Any], dry_run: bool) -> str:
        limit = min(int(args.get("limit", 10)), 25)
        stubs = await list_messages(connector, db, "is:unread in:inbox", limit)
        ids = [s["id"] for s in stubs]

        fresh = await ctx.unprocessed(ids)
        skipped = len(ids) - len(fresh)
        if not fresh:
            return f"No new unread email. ({skipped} already handled in an earlier run.)"

        out = []
        for mid in [i for i in ids if i in fresh]:
            msg = await get_message(connector, db, mid)
            ctx.note_seen(mid)
            out.append(msg)

        lines = [
            f"{i + 1}. id={m['id']} from={m['from']} subject={m['subject']!r} — {m['snippet'][:160]}"
            for i, m in enumerate(out)
        ]
        suffix = f"\n({skipped} already handled in an earlier run, omitted.)" if skipped else ""
        return f"{len(out)} unread email(s):\n" + "\n".join(lines) + suffix

    async def search(args: dict[str, Any], dry_run: bool) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: query is required."
        stubs = await list_messages(connector, db, query, min(int(args.get("limit", 10)), 25))
        if not stubs:
            return f"No email matched {query!r}."
        msgs = [await get_message(connector, db, s["id"]) for s in stubs]
        return f"{len(msgs)} result(s):\n" + "\n".join(
            f"{i + 1}. id={m['id']} from={m['from']} subject={m['subject']!r} — {m['snippet'][:160]}"
            for i, m in enumerate(msgs)
        )

    async def send(args: dict[str, Any], dry_run: bool) -> str:
        to = str(args.get("to", "")).strip()
        subject = str(args.get("subject", "")).strip()
        body = str(args.get("body", "")).strip()
        if not (to and body):
            return "Error: 'to' and 'body' are required."
        if dry_run:
            return f"Would have emailed {to} — subject {subject!r}, body: {body[:300]}"
        result = await send_message(connector, db, to, subject, body)
        return f"Email sent to {to} (id {result.get('id')})."

    async def reply(args: dict[str, Any], dry_run: bool) -> str:
        message_id = str(args.get("message_id", "")).strip()
        body = str(args.get("body", "")).strip()
        if not (message_id and body):
            return "Error: 'message_id' and 'body' are required."

        original = await get_message(connector, db, message_id)
        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        if dry_run:
            return (
                f"Would have replied to {original['from']} about {original['subject']!r} "
                f"with: {body[:300]}"
            )
        await send_message(
            connector, db, original["from"], subject, body, thread_id=original["thread_id"]
        )
        await ctx.mark_processed(message_id)
        return f"Replied to {original['from']} about {original['subject']!r}."

    async def archive(args: dict[str, Any], dry_run: bool) -> str:
        message_id = str(args.get("message_id", "")).strip()
        if not message_id:
            return "Error: 'message_id' is required."
        if dry_run:
            msg = await get_message(connector, db, message_id)
            return f"Would have archived {msg['subject']!r} from {msg['from']}."
        await archive_message(connector, db, message_id)
        await ctx.mark_processed(message_id)
        return f"Archived message {message_id}."

    limit_prop = {"limit": {"type": "integer", "description": "Max messages, default 10, cap 25."}}

    tools = [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("read_unread_emails"),
                ctx.describe(
                    "List unread inbox email. Automatically excludes messages this agent "
                    "already handled on a previous run."
                ),
                {"type": "object", "properties": limit_prop},
            ),
            read_unread,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("search_emails"),
                ctx.describe("Search email using Gmail search syntax, e.g. 'from:bob after:2026/01/01'."),
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, **limit_prop},
                    "required": ["query"],
                },
            ),
            search,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("send_email"),
                ctx.describe("Send a new email."),
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient address."},
                        "subject": {"type": "string"},
                        "body": {"type": "string", "description": "Plain text body."},
                    },
                    "required": ["to", "body"],
                },
            ),
            send,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("reply_to_email"),
                ctx.describe("Reply to an email, keeping it in the same thread."),
                {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Id from read_unread_emails."},
                        "body": {"type": "string", "description": "Plain text reply."},
                    },
                    "required": ["message_id", "body"],
                },
            ),
            reply,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("archive_email"),
                ctx.describe(
                    "Archive an email by removing it from the inbox. Nothing is deleted."
                ),
                {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            ),
            archive,
        ),
    ]
    from app.integrations import calendar as google_calendar

    return tools + google_calendar.build_tools(ctx)
