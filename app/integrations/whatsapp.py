"""
WhatsApp Business integration (Meta Cloud API)
==============================================
Config fields:
  phone_number_id  — from Meta Business Manager
  access_token     — permanent system-user token
  verify_token     — arbitrary string you set; Meta echoes it during webhook verification

The 24-hour customer service window: after a user messages you, you have 24 hours to reply
freely. After that window only pre-approved template messages may be sent. This tool returns
a clear error when the window is closed so the agent can inform the human rather than silently
failing.

Tools:
  send_whatsapp_message(to, message) — send a plain-text reply
  read_whatsapp_messages()           — last unseen inbound messages from InboundEvent rows

Inbound flow:
  POST /webhooks/whatsapp/{connector_id}  →  _record() in webhooks router  →  InboundEvent
  Worker's fire_channel_triggers() picks these up just like Telegram.
"""

from typing import Any
from uuid import UUID

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.db.models import InboundEvent
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

API = "https://graph.facebook.com/v19.0"
WINDOW_ERR = "131026"  # Meta error code: user outside 24h window


async def validate(phone_number_id: str, access_token: str) -> str:
    """Return the display phone number if credentials work."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{API}/{phone_number_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "display_phone_number,verified_name"},
        )
    data = resp.json()
    if "error" in data:
        raise IntegrationError(data["error"].get("message", "Meta API error"))
    number = data.get("display_phone_number", phone_number_id)
    name = data.get("verified_name", "")
    return f"{name} ({number})" if name else number


async def send_text(phone_number_id: str, access_token: str, to: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
    data = resp.json()
    if "error" in data:
        code = str(data["error"].get("code", ""))
        msg = data["error"].get("message", "WhatsApp API error")
        if code == WINDOW_ERR:
            raise IntegrationError(
                "The 24-hour customer service window has closed. "
                "You can only send pre-approved template messages now. "
                "Ask the customer to message you first to reopen the window."
            )
        raise IntegrationError(msg)


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    config = decrypt_json(ctx.connector.config)
    phone_id = config["phone_number_id"]
    token = config["access_token"]

    # ── send ──────────────────────────────────────────────────────────────────

    async def send(args: dict[str, Any], dry_run: bool) -> str:
        to = str(args.get("to", "")).strip().lstrip("+")
        message = str(args.get("message", "")).strip()
        if not (to and message):
            return "Error: 'to' and 'message' are required."
        if dry_run:
            return f"[simulated] Would send WhatsApp to +{to}: {message[:200]}"
        try:
            await send_text(phone_id, token, to, message)
            return f"WhatsApp message sent to +{to}."
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── read ───────────────────────────────────────────────────────────────────

    async def read(args: dict[str, Any], dry_run: bool) -> str:
        limit = min(int(args.get("limit", 10)), 50)
        db: AsyncSession = ctx.db
        rows = await db.exec(
            select(InboundEvent)
            .where(InboundEvent.connector_id == ctx.connector.id)
            .order_by(InboundEvent.created_at.desc())
            .limit(limit)
        )
        events = rows.all()
        if not events:
            return "No WhatsApp messages found."
        lines = []
        for e in reversed(events):
            ctx.mark_seen(str(ctx.connector.id), e.external_id)
            lines.append(f"[{e.created_at.strftime('%Y-%m-%d %H:%M')}] {e.sender}: {e.text}")
        return "\n".join(lines)

    def n(base: str) -> str:
        return ctx.tool_name(base)

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("send_whatsapp_message"),
                description=(
                    "Send a WhatsApp text message to a phone number. "
                    "IMPORTANT: WhatsApp allows free-form replies only within 24 hours of "
                    "the customer's last message. After that window you will get an error — "
                    "tell the user and ask them to message first to reopen the window. "
                    "Use E.164 format for 'to' (e.g. 15551234567 without the leading +)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Recipient phone number in E.164 format without '+', e.g. 15551234567.",
                        },
                        "message": {
                            "type": "string",
                            "description": "The message text to send.",
                        },
                    },
                    "required": ["to", "message"],
                },
            ),
            handler=send,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("read_whatsapp_messages"),
                description=(
                    "Read recent inbound WhatsApp messages. Returns messages from customers "
                    "who have written to your number, newest first. Use this to see what "
                    "customers are asking before deciding how to reply."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of messages to return (default 10, max 50).",
                        }
                    },
                    "required": [],
                },
            ),
            handler=read,
        ),
    ]
