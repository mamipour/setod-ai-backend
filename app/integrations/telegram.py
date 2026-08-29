"""
Telegram integration
====================
Two connector types share this module because they are the same product to a user and two
completely different APIs underneath.

A **bot** talks over the HTTP Bot API and can only message chats that have messaged it first,
so its tools are notification-shaped: one destination, fire and forget.

A **client** is the user's own account over MTProto. It can message anyone the user can, and
it can read. That makes it the right tool for "watch my DMs" workflows and the wrong tool for
anything high volume — Telegram rate-limits user accounts far more aggressively than bots.
"""

from typing import Any

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser

from app.config import settings
from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.db.models import Connector, ConnectorType
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

BOT_API = "https://api.telegram.org"


# ── Bot API ────────────────────────────────────────────────────────────────────

async def bot_call(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BOT_API}/bot{token}/{method}", json=payload, timeout=15)
    data = resp.json()
    if not data.get("ok"):
        raise IntegrationError(f"Telegram: {data.get('description', 'request failed')}")
    return data.get("result", {})


async def bot_send(token: str, chat_id: str | int, text: str) -> dict[str, Any]:
    return await bot_call(token, "sendMessage", {"chat_id": chat_id, "text": text})


# ── MTProto client ─────────────────────────────────────────────────────────────

def mtproto_client(config: dict[str, Any]) -> TelegramClient:
    """A Telethon client on the stored session.

    The device fields must stay identical to the ones used at login. Telegram treats a
    changed device fingerprint as a new session and will invalidate the stored one.
    """
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise IntegrationError("Telegram API credentials are not configured on this server.")
    return TelegramClient(
        StringSession(config["session_string"]),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
        device_model="Pixel 5",
        system_version="11",
        app_version="8.4.1",
        lang_code="en",
        system_lang_code="en-US",
    )


async def client_send(config: dict[str, Any], to: str, text: str) -> None:
    client = mtproto_client(config)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise IntegrationError("Telegram session expired — reconnect the account.")
        await client.send_message(to, text)
    finally:
        await client.disconnect()


async def client_unread(config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    client = mtproto_client(config)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise IntegrationError("Telegram session expired — reconnect the account.")
        out = []
        async for dialog in client.iter_dialogs(limit=50):
            if dialog.unread_count <= 0 or not dialog.message:
                continue

            msg = dialog.message
            sender_id: int | None = None
            sender_name: str | None = None
            sender_username: str | None = None

            # from_id is populated for messages inside groups/channels.
            # For private DMs the dialog entity itself is the sender.
            if msg.from_id and isinstance(msg.from_id, PeerUser):
                sender_id = msg.from_id.user_id
            elif hasattr(msg, "sender_id") and msg.sender_id:
                sender_id = msg.sender_id

            if sender_id:
                try:
                    entity = await client.get_entity(sender_id)
                    parts = [
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    ]
                    sender_name = " ".join(p for p in parts if p) or None
                    sender_username = getattr(entity, "username", None)
                except Exception:
                    # User may have privacy settings that block entity lookup.
                    pass

            out.append({
                "id": str(dialog.id),
                "chat": dialog.name or str(dialog.id),
                "unread": dialog.unread_count,
                "last_message_id": str(msg.id),
                "text": (msg.message or "")[:500],
                "sender_id": str(sender_id) if sender_id else None,
                "sender_name": sender_name,
                "sender_username": sender_username,
            })
            if len(out) >= limit:
                break
        return out
    finally:
        await client.disconnect()


# ── Tools ──────────────────────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    config = decrypt_json(ctx.connector.config)

    if ctx.connector.type == ConnectorType.telegram_bot:
        return _bot_tools(ctx, config)
    return _client_tools(ctx, config)


def _bot_tools(ctx: ToolContext, config: dict[str, Any]) -> list[RegisteredTool]:
    token = config["bot_token"]
    admin_chat_id = config["admin_chat_id"]

    async def notify(args: dict[str, Any], dry_run: bool) -> str:
        text = str(args.get("message", "")).strip()
        if not text:
            return "Error: 'message' is required."
        if dry_run:
            return f"Would have sent this Telegram message: {text[:500]}"
        await bot_send(token, admin_chat_id, text)
        return "Telegram message sent."

    return [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("send_telegram_message"),
                ctx.describe(
                    "Send a Telegram message to the configured chat. Use for alerts and "
                    "notifications to the account owner."
                ),
                {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Plain text. No HTML."}
                    },
                    "required": ["message"],
                },
            ),
            notify,
        )
    ]


def _client_tools(ctx: ToolContext, config: dict[str, Any]) -> list[RegisteredTool]:
    async def send(args: dict[str, Any], dry_run: bool) -> str:
        to = str(args.get("to", "")).strip()
        text = str(args.get("message", "")).strip()
        if not (to and text):
            return "Error: 'to' and 'message' are required."
        if dry_run:
            return f"Would have sent {to} this Telegram message: {text[:500]}"
        await client_send(config, to, text)
        return f"Telegram message sent to {to}."

    async def read_unread(args: dict[str, Any], dry_run: bool) -> str:
        limit = min(int(args.get("limit", 10)), 25)
        chats = await client_unread(config, limit)
        if not chats:
            return "No unread Telegram messages."

        fresh_ids = await ctx.unprocessed([c["last_message_id"] for c in chats])
        fresh = [c for c in chats if c["last_message_id"] in fresh_ids]
        if not fresh:
            return "No new unread Telegram messages since the last run."

        for chat in fresh:
            ctx.note_seen(chat["last_message_id"])

        lines = []
        for i, c in enumerate(fresh):
            sender_parts = []
            if c.get("sender_name"):
                sender_parts.append(c["sender_name"])
            if c.get("sender_username"):
                sender_parts.append(f"@{c['sender_username']}")
            if c.get("sender_id"):
                sender_parts.append(f"ID:{c['sender_id']}")
            sender_str = " / ".join(sender_parts) if sender_parts else "unknown sender"

            lines.append(
                f"{i + 1}. Group: {c['chat']} | {c['unread']} unread\n"
                f"   Sender: {sender_str}\n"
                f"   Message: {c['text'][:300]}"
            )
        return f"{len(fresh)} chat(s) with unread messages:\n\n" + "\n\n".join(lines)

    return [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("send_telegram_message"),
                ctx.describe(
                    "Send a Telegram message from the connected account to any username, "
                    "phone number, or chat id."
                ),
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "@username, phone, or chat id."},
                        "message": {"type": "string"},
                    },
                    "required": ["to", "message"],
                },
            ),
            send,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("read_telegram_messages"),
                ctx.describe(
                    "List chats with unread messages. Excludes chats already handled on a "
                    "previous run."
                ),
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max chats, default 10."}
                    },
                },
            ),
            read_unread,
        ),
    ]
