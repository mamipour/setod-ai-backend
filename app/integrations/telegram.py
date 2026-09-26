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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlmodel import select
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageService

from app.config import settings
from app.core.conversations import display_name
from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.db.models import Agent, AgentCursor, Connector, ConnectorType
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

BOT_API = "https://api.telegram.org"

# Group reading limits. Per chat: the newest N messages since the agent's cursor; anything
# older in the same window is reported as skipped, not read. Total: hard cap on messages
# handed to the model in one call.
PER_CHAT_LIMIT = 30
TOTAL_LIMIT = 150
MSG_TEXT_CHARS = 500
REPLY_EXCERPT_CHARS = 120
# How many dialogs to scan per call. Telegram rate-limits user accounts; 50 is safe.
DIALOG_SCAN_LIMIT = 50


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


@dataclass
class TgMessage:
    """One message as the read tool sees it (provider-agnostic, testable)."""

    msg_id: int
    date: datetime
    text: str                    # body, or a media marker such as "[voice message]"
    speaker_id: str = ""
    speaker_name: str = ""
    reply_to_id: int | None = None
    reply_to_speaker: str = ""
    reply_to_text: str = ""


@dataclass
class ChatBatch:
    """New messages in one dialog since the agent's cursor, oldest first."""

    chat_id: str
    chat_name: str
    is_group: bool
    top_id: int                  # highest message id seen — becomes the new cursor
    messages: list[TgMessage] = field(default_factory=list)
    older_skipped: bool = False  # more than PER_CHAT_LIMIT new messages; oldest dropped


def _media_marker(msg: Any) -> str:
    """Marker for a message with no text. Media is never fetched on this path."""
    if getattr(msg, "voice", None):
        return "[voice message]"
    if getattr(msg, "audio", None):
        return "[audio]"
    if getattr(msg, "photo", None):
        return "[photo]"
    if getattr(msg, "video", None) or getattr(msg, "video_note", None):
        return "[video]"
    if getattr(msg, "sticker", None):
        return "[sticker]"
    if getattr(msg, "document", None):
        name = getattr(getattr(msg, "file", None), "name", None)
        return f"[file: {name}]" if name else "[file]"
    if getattr(msg, "geo", None):
        return "[location]"
    if getattr(msg, "contact", None):
        return "[contact]"
    return "[media]" if getattr(msg, "media", None) else "[no content]"


def _msg_text(msg: Any) -> str:
    body = (getattr(msg, "message", None) or "").strip()
    if body:
        return body[:MSG_TEXT_CHARS]
    return _media_marker(msg)


async def _speaker(client: TelegramClient, msg: Any, cache: dict[int, tuple[str, str]]) -> tuple[str, str]:
    """(speaker_id, speaker_name) for a message, memoised per sender within one call."""
    sid = getattr(msg, "sender_id", None)
    if not sid:
        return "", ""
    if sid in cache:
        return cache[sid]
    name = ""
    try:
        ent = await msg.get_sender()
        if ent is not None:
            # Channels post as themselves: they have a title, not a first name.
            name = getattr(ent, "title", None) or display_name(
                getattr(ent, "first_name", None),
                getattr(ent, "last_name", None),
                getattr(ent, "username", None),
                sid,
            )
    except Exception:
        # Privacy settings can block entity lookup; fall back to the id.
        pass
    if not name:
        name = f"ID:{sid}"
    cache[sid] = (str(sid), name)
    return cache[sid]


async def client_new_messages(
    config: dict[str, Any],
    *,
    cursors: dict[str, str],
    since: datetime,
    chat_limit: int,
    per_chat: int = PER_CHAT_LIMIT,
) -> list[ChatBatch]:
    """New messages per dialog since this agent's cursor.

    Discovery is by message id, never by `unread_count`: that counter is the owner's own
    read state and drops to zero the moment they open the group on their phone.

    - Dialog with a cursor: messages with id > cursor, newest `per_chat` of them.
    - Dialog without a cursor (first run): messages dated at/after `since` — monitoring
      starts when the agent was created, not when the group was.
    - Own outgoing messages and service messages (joins, pins) are excluded from the
      output but still advance the cursor.
    - Replies inline their parent (one level — all Telegram exposes).
    """
    client = mtproto_client(config)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise IntegrationError("Telegram session expired — reconnect the account.")

        batches: list[ChatBatch] = []
        async for dialog in client.iter_dialogs(limit=DIALOG_SCAN_LIMIT):
            if not dialog.message:
                continue
            chat_id = str(dialog.id)
            top = dialog.message
            cursor = cursors.get(chat_id)
            cursor_id = int(cursor) if cursor else None

            # Nothing new in this dialog: skip without an API call.
            if cursor_id is not None and top.id <= cursor_id:
                continue
            if cursor_id is None and top.date < since:
                continue

            entity = dialog.entity
            fetched: list[Any] = []
            kwargs: dict[str, Any] = {"limit": per_chat + 1}
            if cursor_id is not None:
                kwargs["min_id"] = cursor_id
            async for m in client.iter_messages(entity, **kwargs):
                if cursor_id is None and m.date < since:
                    break
                fetched.append(m)

            if not fetched:
                continue
            older_skipped = len(fetched) > per_chat
            fetched = fetched[:per_chat]          # newest first → keep the newest N
            fetched.reverse()                      # oldest first for reading

            batch = ChatBatch(
                chat_id=chat_id,
                chat_name=dialog.name or chat_id,
                is_group=bool(dialog.is_group or dialog.is_channel),
                top_id=max(top.id, fetched[-1].id),
                older_skipped=older_skipped,
            )

            speakers: dict[int, tuple[str, str]] = {}
            by_id: dict[int, Any] = {m.id: m for m in fetched}
            # Parents of replies that are not in the fetched window: one batched lookup.
            want = {
                m.reply_to.reply_to_msg_id
                for m in fetched
                if getattr(m, "reply_to", None) and getattr(m.reply_to, "reply_to_msg_id", None)
                and m.reply_to.reply_to_msg_id not in by_id
            }
            parents: dict[int, Any] = {}
            if want:
                try:
                    for p in await client.get_messages(entity, ids=list(want)):
                        if p is not None:
                            parents[p.id] = p
                except Exception:
                    pass

            for m in fetched:
                if isinstance(m, MessageService) or getattr(m, "out", False):
                    continue
                sid, sname = await _speaker(client, m, speakers)
                tm = TgMessage(
                    msg_id=m.id, date=m.date, text=_msg_text(m),
                    speaker_id=sid, speaker_name=sname,
                )
                rid = getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)
                if rid:
                    tm.reply_to_id = rid
                    parent = by_id.get(rid) or parents.get(rid)
                    if parent is not None:
                        _, tm.reply_to_speaker = (
                            ("", "You") if getattr(parent, "out", False)
                            else await _speaker(client, parent, speakers)
                        )
                        tm.reply_to_text = _msg_text(parent)[:REPLY_EXCERPT_CHARS]
                batch.messages.append(tm)

            batches.append(batch)
            if len(batches) >= chat_limit:
                break
        return batches
    finally:
        await client.disconnect()


def _who(m: TgMessage) -> str:
    """'Reza (@reza) #123' — name for reading, id for the lead alert. No id twice."""
    if not m.speaker_id:
        return m.speaker_name or "unknown"
    if not m.speaker_name or m.speaker_name.startswith("ID:"):
        return f"#{m.speaker_id}"
    return f"{m.speaker_name} #{m.speaker_id}"


def format_batches(batches: list[ChatBatch], *, total_limit: int = TOTAL_LIMIT) -> str:
    """Render fetched chats for the model. Pure function — covered by unit tests."""
    shown = 0
    blocks: list[str] = []
    for b in batches:
        if not b.messages:
            continue
        remaining = total_limit - shown
        if remaining <= 0:
            blocks.append(f"… further chats omitted (limit of {total_limit} messages reached).")
            break
        msgs = b.messages[-remaining:]           # keep the newest if we must cut
        cut = len(b.messages) - len(msgs)
        shown += len(msgs)

        kind = "Group" if b.is_group else "Chat"
        n = len(b.messages)
        head = f'{kind} "{b.chat_name}" (chat_id={b.chat_id}) — {n} new message{"s" if n != 1 else ""}'
        notes = []
        if b.older_skipped:
            notes.append(f"older messages beyond the newest {PER_CHAT_LIMIT} skipped")
        if cut:
            notes.append(f"{cut} oldest omitted for length")
        if notes:
            head += f" ({'; '.join(notes)})"

        lines = [head]
        for m in msgs:
            who = _who(m)
            reply = ""
            if m.reply_to_id:
                target = m.reply_to_speaker or "earlier message"
                reply = (
                    f'↳ re {target}: "{m.reply_to_text}" — ' if m.reply_to_text
                    else f"↳ re {target} — "
                )
            lines.append(f"  [{m.date.strftime('%m-%d %H:%M')}] {who} [msg {m.msg_id}]: {reply}{m.text}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return "\n\n".join(blocks)


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
        # `chat_id` lets the agent reply to the actual inbound sender; defaults to admin chat
        chat_id = str(args.get("chat_id", "")).strip() or admin_chat_id
        if not text:
            return "Error: 'message' is required."
        if dry_run:
            return f"Would have sent Telegram message to {chat_id}: {text[:500]}"
        await bot_send(token, chat_id, text)
        # Record outbound into the conversation thread when context is available
        if ctx.conversation_id and not dry_run:
            from app.core.conversations import record_outbound
            from app.db.models import MessageAuthor
            await record_outbound(
                ctx.db,
                connector=ctx.connector,
                conversation_id=ctx.conversation_id,
                peer_id=chat_id,
                text=text,
                session_id=ctx.session_id,
                author=MessageAuthor.agent,
                dry_run=False,
            )
        return "Telegram message sent."

    return [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("send_telegram_message"),
                ctx.describe(
                    "Send a Telegram message. When replying to an inbound message always "
                    "pass the sender's chat_id so the reply reaches them; for admin "
                    "notifications omit chat_id."
                ),
                {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Plain text. No HTML."},
                        "chat_id": {
                            "type": "string",
                            "description": (
                                "Telegram chat id of the recipient. "
                                "Required when replying to an inbound message. "
                                "Omit to send to the configured admin chat."
                            ),
                        },
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
        if ctx.conversation_id:
            from app.core.conversations import record_outbound
            from app.db.models import MessageAuthor
            await record_outbound(
                ctx.db,
                connector=ctx.connector,
                conversation_id=ctx.conversation_id,
                peer_id=to,
                text=text,
                session_id=ctx.session_id,
                author=MessageAuthor.agent,
                dry_run=False,
            )
        return f"Telegram message sent to {to}."

    async def read_unread(args: dict[str, Any], dry_run: bool) -> str:
        from app.core.conversations import Peer, record_inbound

        chat_limit = min(int(args.get("limit", 10)), 25)

        # Every dialog's stored cursor for this agent, loaded in one query.
        cursor_rows = await ctx.db.exec(
            select(AgentCursor.scope, AgentCursor.cursor).where(
                AgentCursor.agent_id == ctx.agent_id,
                AgentCursor.connector_id == ctx.connector.id,
            )
        )
        cursors = {scope: cur for scope, cur in cursor_rows.all()}
        cursors.update(ctx.cursors)  # advanced earlier in this run, not yet flushed

        agent = await ctx.db.get(Agent, ctx.agent_id)
        batches = await client_new_messages(
            config, cursors=cursors, since=agent.created_at, chat_limit=chat_limit
        )
        if not batches:
            return "No new Telegram messages since the last run."

        # Ledger keys are chat-scoped: message ids repeat across supergroups.
        keys = {f"{b.chat_id}:{m.msg_id}": (b, m) for b in batches for m in b.messages}
        fresh_keys = await ctx.unprocessed(list(keys))

        for b in batches:
            ctx.set_cursor(b.chat_id, str(b.top_id))
            kept: list[TgMessage] = []
            for m in b.messages:
                key = f"{b.chat_id}:{m.msg_id}"
                if key not in fresh_keys:
                    continue
                kept.append(m)
                ctx.note_seen(key)
                # Same chat:msg convention as `key`, so the transcript can find parents.
                reply_key = f"{b.chat_id}:{m.reply_to_id}" if m.reply_to_id else ""
                peer = Peer(
                    peer_id=b.chat_id,
                    peer_name=b.chat_name,
                    is_group=b.is_group,
                    speaker_id=m.speaker_id,
                    speaker_name=m.speaker_name,
                    reply_to_external_id=reply_key,
                    reply_to_text=m.reply_to_text,
                )
                await record_inbound(
                    ctx.db,
                    ctx.connector,
                    external_id=key,
                    text=m.text,
                    sender=b.chat_id,
                    payload={
                        "peer_id": b.chat_id,
                        "peer_name": b.chat_name,
                        "is_group": b.is_group,
                        "speaker_id": m.speaker_id,
                        "speaker_name": m.speaker_name,
                        "reply_to_external_id": reply_key,
                        "reply_to_text": m.reply_to_text,
                        "text": m.text,
                    },
                    peer=peer,
                    attachments=[],
                    created_at=m.date,
                )
            b.messages = kept

        body = format_batches(batches)
        if not body:
            return "No new Telegram messages since the last run."
        total = sum(len(b.messages) for b in batches)
        chats = sum(1 for b in batches if b.messages)
        return f"{total} new message(s) across {chats} chat(s):\n\n{body}"

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
                    "New messages in the account's chats and groups since this agent's "
                    "last run, grouped by chat and oldest first. Each line has the time, "
                    "the sender (name, @username, #id) and the message; replies quote the "
                    "message they answer. Independent of Telegram's read/unread state. "
                    f"At most {PER_CHAT_LIMIT} newest messages per chat; excludes messages "
                    "this agent already handled and your own outgoing messages."
                ),
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max chats, default 10, max 25."}
                    },
                },
            ),
            read_unread,
        ),
    ]
