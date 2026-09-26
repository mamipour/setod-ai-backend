"""
Conversation layer
==================
Shared helpers for recording every inbound and outbound message into the conversation thread,
resolving the external peer from provider payloads, and extracting any attachments.

All channel handlers (webhooks/router.py, hooks/router.py, the Telegram account poller)
call `record_inbound` instead of writing to InboundEvent directly.  Send tools call
`record_outbound` after a successful (non-dry-run) send.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import (
    Connector,
    ConnectorType,
    Conversation,
    ConversationMessage,
    ConversationStatus,
    InboundEvent,
    InboundEventStatus,
    MessageAuthor,
    MessageDirection,
    MessageKind,
)

log = logging.getLogger(__name__)

# Maximum characters stored in InboundEvent.text (mirrors the limit in webhooks/router.py)
MAX_TEXT = 4_096

# ── Peer ──────────────────────────────────────────────────────────────────────


@dataclass
class Peer:
    """Provider-native identification of the external person."""

    peer_id: str          # stable channel-level id (wa_id, chat_id, IG user id, phone)
    peer_name: str = ""   # human-readable label when available
    thread_key: str = ""  # discriminator for Instagram comments (media_id), else ""


# ── Attachment ────────────────────────────────────────────────────────────────


@dataclass
class Attachment:
    """A non-text media item in a message.

    `provider_ref` is the provider-specific URL or id needed to fetch the bytes later.
    `status` starts as "pending"; the media worker advances it to "ready" or "failed".
    """

    kind: MessageKind
    provider_ref: str = ""       # URL / id to fetch from the provider
    mime: str = ""
    filename: str = ""
    caption: str = ""
    size: int = 0                # bytes, 0 if unknown before fetch
    duration: float = 0.0        # seconds, for audio/video
    # Populated after processing:
    stored_path: str = ""
    status: str = "pending"      # pending | ready | failed | unavailable
    error: str = ""
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


# ── resolve_peer ──────────────────────────────────────────────────────────────


def resolve_peer(connector_type: ConnectorType, payload: dict[str, Any]) -> Peer:
    """Extract peer identity from a raw provider payload.

    Called before writing InboundEvent so the conversation key is known.
    Returns a Peer with empty peer_id if the payload doesn't contain enough info —
    the caller should fall back to `InboundEvent.sender`.
    """
    match connector_type:
        case ConnectorType.telegram_bot:
            # payload is the full Telegram Update object
            chat = (
                payload.get("message", {}).get("chat")
                or payload.get("callback_query", {}).get("message", {}).get("chat")
                or {}
            )
            peer_id = str(chat.get("id", ""))
            peer_name = (
                chat.get("username")
                or chat.get("first_name", "")
            )
            return Peer(peer_id=peer_id, peer_name=peer_name)

        case ConnectorType.telegram_client:
            # payload recorded by the account poller (app/integrations/telegram.py)
            peer_id = str(payload.get("peer_id", payload.get("sender_id", "")))
            peer_name = payload.get("sender_name", payload.get("peer_name", ""))
            return Peer(peer_id=peer_id, peer_name=peer_name)

        case ConnectorType.twilio:
            # Twilio SMS/MMS form params flattened to dict
            peer_id = payload.get("From", "")
            peer_name = payload.get("FromCity", "") or peer_id
            return Peer(peer_id=peer_id, peer_name=peer_name)

        case ConnectorType.whatsapp:
            # msg dict from WhatsApp Cloud API (value.messages[])
            peer_id = payload.get("from", "")
            # contacts list is on value, not msg, so falls back to phone
            peer_name = payload.get("profile", {}).get("name", "") or peer_id
            return Peer(peer_id=peer_id, peer_name=peer_name)

        case ConnectorType.instagram:
            # Either a messaging event or a change-value dict.
            # DM: sender.id; comment: from.id / from.username
            sender = payload.get("sender", {})
            if sender:
                peer_id = str(sender.get("id", ""))
                peer_name = sender.get("name", sender.get("username", "")) or peer_id
                thread_key = ""
            else:
                # Comment payload
                from_info = payload.get("from", {})
                peer_id = str(from_info.get("id", ""))
                peer_name = from_info.get("username", "") or peer_id
                thread_key = str(payload.get("media_id", ""))
            return Peer(peer_id=peer_id, peer_name=peer_name, thread_key=thread_key)

        case _:
            return Peer(peer_id="")


# ── extract_attachments ───────────────────────────────────────────────────────


def extract_attachments(
    connector_type: ConnectorType,
    payload: dict[str, Any],
) -> tuple[str, list[Attachment]]:
    """Extract the text body and any media attachments from a provider payload.

    Returns (text, attachments).  `text` may be empty when a message is media-only.
    Attachments carry provider refs for the media worker to fetch and process later.
    """
    match connector_type:
        case ConnectorType.telegram_bot:
            msg = payload.get("message") or {}
            return _telegram_bot_attachments(msg)

        case ConnectorType.telegram_client:
            return _telegram_client_attachments(payload)

        case ConnectorType.twilio:
            return _twilio_attachments(payload)

        case ConnectorType.whatsapp:
            return _whatsapp_attachments(payload)

        case ConnectorType.instagram:
            return _instagram_attachments(payload)

        case _:
            return "", []


def _telegram_bot_attachments(msg: dict[str, Any]) -> tuple[str, list[Attachment]]:
    text = msg.get("text") or msg.get("caption") or ""
    atts: list[Attachment] = []

    if voice := msg.get("voice"):
        atts.append(Attachment(
            kind=MessageKind.audio,
            provider_ref=voice.get("file_id", ""),
            mime=voice.get("mime_type", "audio/ogg"),
            duration=voice.get("duration", 0),
            size=voice.get("file_size", 0),
            caption=text,
        ))
        text = ""  # caption goes on the attachment

    elif audio := msg.get("audio"):
        atts.append(Attachment(
            kind=MessageKind.audio,
            provider_ref=audio.get("file_id", ""),
            mime=audio.get("mime_type", "audio/mpeg"),
            duration=audio.get("duration", 0),
            size=audio.get("file_size", 0),
            filename=audio.get("file_name", ""),
            caption=text,
        ))
        text = ""

    elif photos := msg.get("photo"):
        # Telegram sends several sizes; take the largest
        photo = max(photos, key=lambda p: p.get("file_size", 0))
        atts.append(Attachment(
            kind=MessageKind.image,
            provider_ref=photo.get("file_id", ""),
            mime="image/jpeg",
            size=photo.get("file_size", 0),
            caption=text,
        ))
        text = ""

    elif video := msg.get("video"):
        atts.append(Attachment(
            kind=MessageKind.video,
            provider_ref=video.get("file_id", ""),
            mime=video.get("mime_type", "video/mp4"),
            duration=video.get("duration", 0),
            size=video.get("file_size", 0),
            caption=text,
        ))
        text = ""

    elif doc := msg.get("document"):
        atts.append(Attachment(
            kind=MessageKind.document,
            provider_ref=doc.get("file_id", ""),
            mime=doc.get("mime_type", "application/octet-stream"),
            size=doc.get("file_size", 0),
            filename=doc.get("file_name", "document"),
        ))

    elif sticker := msg.get("sticker"):
        atts.append(Attachment(
            kind=MessageKind.sticker,
            provider_ref=sticker.get("file_id", ""),
        ))

    elif location := msg.get("location"):
        lat = location.get("latitude", 0)
        lng = location.get("longitude", 0)
        atts.append(Attachment(
            kind=MessageKind.location,
            provider_ref=f"{lat},{lng}",
        ))

    elif contact := msg.get("contact"):
        phone = contact.get("phone_number", "")
        name = contact.get("first_name", "")
        atts.append(Attachment(
            kind=MessageKind.contact,
            provider_ref=phone,
            caption=f"{name} {phone}".strip(),
        ))

    return text, atts


def _telegram_client_attachments(payload: dict[str, Any]) -> tuple[str, list[Attachment]]:
    # Account-mode payloads are already text-only today; expand later
    text = payload.get("text", "")
    return text, []


def _twilio_attachments(payload: dict[str, Any]) -> tuple[str, list[Attachment]]:
    text = payload.get("Body", "")
    atts: list[Attachment] = []
    num_media = int(payload.get("NumMedia", "0") or "0")
    for i in range(num_media):
        url = payload.get(f"MediaUrl{i}", "")
        mime = payload.get(f"MediaContentType{i}", "")
        kind = _mime_to_kind(mime)
        atts.append(Attachment(kind=kind, provider_ref=url, mime=mime))
    return text, atts


def _whatsapp_attachments(payload: dict[str, Any]) -> tuple[str, list[Attachment]]:
    """payload is a msg dict from Cloud API value.messages[]"""
    msg_type = payload.get("type", "text")
    atts: list[Attachment] = []

    if msg_type == "text":
        text = payload.get("text", {}).get("body", "")
        return text, atts

    caption = ""
    if msg_type == "image":
        img = payload.get("image", {})
        caption = img.get("caption", "")
        atts.append(Attachment(
            kind=MessageKind.image,
            provider_ref=img.get("id", ""),
            mime=img.get("mime_type", "image/jpeg"),
            caption=caption,
        ))
    elif msg_type in ("audio", "voice"):
        aud = payload.get("audio", payload.get("voice", {}))
        atts.append(Attachment(
            kind=MessageKind.audio,
            provider_ref=aud.get("id", ""),
            mime=aud.get("mime_type", "audio/ogg"),
        ))
    elif msg_type == "video":
        vid = payload.get("video", {})
        caption = vid.get("caption", "")
        atts.append(Attachment(
            kind=MessageKind.video,
            provider_ref=vid.get("id", ""),
            mime=vid.get("mime_type", "video/mp4"),
            caption=caption,
        ))
    elif msg_type == "document":
        doc = payload.get("document", {})
        atts.append(Attachment(
            kind=MessageKind.document,
            provider_ref=doc.get("id", ""),
            mime=doc.get("mime_type", "application/octet-stream"),
            filename=doc.get("filename", ""),
            caption=doc.get("caption", ""),
        ))
    elif msg_type == "location":
        loc = payload.get("location", {})
        atts.append(Attachment(
            kind=MessageKind.location,
            provider_ref=f"{loc.get('latitude', 0)},{loc.get('longitude', 0)}",
            caption=loc.get("name", ""),
        ))
    elif msg_type == "sticker":
        stk = payload.get("sticker", {})
        atts.append(Attachment(kind=MessageKind.sticker, provider_ref=stk.get("id", "")))
    elif msg_type == "contacts":
        contacts = payload.get("contacts", [{}])
        c = contacts[0] if contacts else {}
        name = c.get("name", {}).get("formatted_name", "")
        phone = c.get("phones", [{}])[0].get("phone", "") if c.get("phones") else ""
        atts.append(Attachment(kind=MessageKind.contact, provider_ref=phone, caption=name))
    else:
        atts.append(Attachment(kind=MessageKind.other, provider_ref=msg_type))

    return caption, atts


def _instagram_attachments(payload: dict[str, Any]) -> tuple[str, list[Attachment]]:
    """payload is a messaging-event or change-value dict."""
    msg = payload.get("message", payload)
    text = msg.get("text", "")
    atts: list[Attachment] = []

    for att in msg.get("attachments", []):
        att_type = att.get("type", "")
        att_payload = att.get("payload", {})
        url = att_payload.get("url", "")
        kind = _att_type_to_kind(att_type)
        atts.append(Attachment(kind=kind, provider_ref=url))

    return text, atts


def _mime_to_kind(mime: str) -> MessageKind:
    if mime.startswith("image/"):
        return MessageKind.image
    if mime.startswith("audio/"):
        return MessageKind.audio
    if mime.startswith("video/"):
        return MessageKind.video
    return MessageKind.document


def _att_type_to_kind(att_type: str) -> MessageKind:
    mapping = {
        "image": MessageKind.image,
        "audio": MessageKind.audio,
        "video": MessageKind.video,
        "file": MessageKind.document,
        "location": MessageKind.location,
        "sticker": MessageKind.sticker,
        "fallback": MessageKind.other,
    }
    return mapping.get(att_type, MessageKind.other)


# ── record_inbound ────────────────────────────────────────────────────────────


async def record_inbound(
    db: AsyncSession,
    connector: Connector,
    *,
    external_id: str,
    text: str,
    sender: str,
    payload: dict[str, Any],
    peer: Peer | None = None,
    attachments: list[Attachment] | None = None,
) -> tuple[bool, UUID | None, UUID | None]:
    """Record an inbound event and link it to a Conversation + ConversationMessage.

    Returns (is_new, conversation_id, inbound_event_id).
    `is_new` is False when the external_id was already recorded (duplicate delivery).

    `peer` and `attachments` are derived here from the payload if not supplied by the caller.
    """
    if peer is None:
        peer = resolve_peer(connector.type, payload)
    if not peer.peer_id:
        peer = Peer(peer_id=sender or "unknown", peer_name=sender)

    if attachments is None:
        _, attachments = extract_attachments(connector.type, payload)

    # Determine the primary kind for the ConversationMessage
    if attachments:
        kind = attachments[0].kind
    else:
        kind = MessageKind.text

    # ── Upsert Conversation ────────────────────────────────────────────────────
    conv_stmt = (
        pg_insert(Conversation)
        .values(
            id=uuid4(),
            org_id=connector.org_id,
            connector_id=connector.id,
            channel=connector.type.value,
            peer_id=peer.peer_id,
            peer_name=peer.peer_name,
            thread_key=peer.thread_key,
            status=ConversationStatus.open,
            summary="",
            last_inbound_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            constraint="uq_conversation",
            set_={
                "last_inbound_at": datetime.now(UTC),
                # Update peer_name if we now have a better one
                "peer_name": pg_insert(Conversation)
                .excluded.peer_name,
            },
        )
        .returning(Conversation.id)
    )
    conv_result = await db.exec(conv_stmt)
    conversation_id: UUID = conv_result.scalar_one()

    # ── Record ConversationMessage ─────────────────────────────────────────────
    msg_id = uuid4()
    cm_stmt = (
        pg_insert(ConversationMessage)
        .values(
            id=msg_id,
            conversation_id=conversation_id,
            org_id=connector.org_id,
            direction=MessageDirection.inbound,
            author=MessageAuthor.peer,
            kind=kind,
            text=text[:MAX_TEXT],
            attachments=[a.to_dict() for a in attachments],
            external_id=external_id,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing()
        .returning(ConversationMessage.id)
    )
    cm_result = await db.exec(cm_stmt)
    conversation_message_id: UUID | None = cm_result.scalar_one_or_none()

    # ── Record InboundEvent ────────────────────────────────────────────────────
    ev_stmt = (
        pg_insert(InboundEvent)
        .values(
            connector_id=connector.id,
            org_id=connector.org_id,
            external_id=external_id,
            text=text[:MAX_TEXT],
            sender=peer.peer_id,
            payload=payload,
            conversation_id=conversation_id,
            conversation_message_id=conversation_message_id,
        )
        .on_conflict_do_nothing(constraint="uq_inbound_event")
        .returning(InboundEvent.id)
    )
    ev_result = await db.exec(ev_stmt)
    event_id: UUID | None = ev_result.scalar_one_or_none()

    await db.commit()

    if event_id is None:
        log.debug("record_inbound: duplicate external_id=%s — skipped", external_id)
        return False, None, None

    return True, conversation_id, event_id


# ── record_outbound ───────────────────────────────────────────────────────────


async def record_outbound(
    db: AsyncSession,
    *,
    connector: Connector,
    conversation_id: UUID,
    peer_id: str,
    text: str,
    session_id: UUID | None = None,
    author: MessageAuthor = MessageAuthor.agent,
    dry_run: bool = False,
) -> UUID | None:
    """Record an outbound message from the agent (or human) into the conversation thread.

    Returns the ConversationMessage id, or None when dry_run is True (nothing is persisted).
    """
    if dry_run:
        return None

    now = datetime.now(UTC)
    msg_id = uuid4()
    stmt = (
        pg_insert(ConversationMessage)
        .values(
            id=msg_id,
            conversation_id=conversation_id,
            org_id=connector.org_id,
            direction=MessageDirection.outbound,
            author=author,
            kind=MessageKind.text,
            text=text,
            attachments=[],
            external_id="",
            session_id=session_id,
            created_at=now,
        )
        .returning(ConversationMessage.id)
    )
    result = await db.exec(stmt)

    # Update conversation last_outbound_at
    await db.exec(
        pg_insert(Conversation)
        .values(id=conversation_id, last_outbound_at=now)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"last_outbound_at": now},
        )
    )
    await db.commit()

    return result.scalar_one_or_none()


# ── get_open_conversation ─────────────────────────────────────────────────────


# ── render_transcript ─────────────────────────────────────────────────────────

# Maximum characters injected as history context into the agent opening
TRANSCRIPT_MAX_CHARS = 6_000
# Number of recent turns to always include verbatim (newest-first)
TRANSCRIPT_RECENT_TURNS = 30
# Character budget for the rolling summary block
SUMMARY_MAX_CHARS = 800
# Fold turns older than this many into the rolling summary after a run succeeds
SUMMARY_FOLD_AFTER_TURNS = 30


async def render_transcript(
    db: AsyncSession,
    conversation: Conversation,
    exclude_message_ids: list[UUID] | None = None,
) -> str:
    """Build a history block to prepend to the agent opening.

    Layout::

        [Conversation history — {n} earlier turns]
        {rolling_summary}          ← if present
        ---
        {peer_name} [{timestamp}]: {text_or_media_marker}
        You [{timestamp}]: {text}
        ...

    The most recent TRANSCRIPT_RECENT_TURNS turns are included verbatim.
    Older turns are represented by the rolling summary when available.
    """
    exclude = set(exclude_message_ids or [])

    rows = await db.exec(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.id.not_in(exclude) if exclude else True,
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(TRANSCRIPT_RECENT_TURNS)
    )
    recent = list(reversed(rows.all()))

    if not recent and not conversation.summary:
        return ""

    peer_label = conversation.peer_name or conversation.peer_id or "Customer"

    lines: list[str] = []
    for msg in recent:
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
        label = "You" if msg.direction.value == "outbound" else peer_label

        # Build message text
        body_parts: list[str] = []
        if msg.text:
            body_parts.append(msg.text)
        for att in msg.attachments or []:
            att_text = att.get("text", "")
            if att_text:
                body_parts.append(att_text)
            elif att.get("status") == "pending":
                body_parts.append(f"[{att.get('kind', 'media')} — processing…]")
            else:
                body_parts.append(f"[{att.get('kind', 'media')}]")

        body = " ".join(body_parts) if body_parts else "[no content]"
        lines.append(f"{label} [{ts}]: {body}")

    history_block = "\n".join(lines)

    parts: list[str] = []
    if conversation.summary:
        parts.append(f"[Earlier conversation summary]\n{conversation.summary}\n---")
    if lines:
        turn_word = "turn" if len(recent) == 1 else "turns"
        parts.append(f"[Last {len(recent)} {turn_word}]\n{history_block}")

    result = "\n\n".join(parts)
    # Hard cap to avoid prompt overflows
    if len(result) > TRANSCRIPT_MAX_CHARS:
        result = result[-TRANSCRIPT_MAX_CHARS:]
        result = "[…]\n" + result[result.find("\n") + 1:]

    return result


async def fold_to_summary(
    db: AsyncSession,
    conversation: Conversation,
    *,
    openai_key: str,
) -> None:
    """Compress turns older than SUMMARY_FOLD_AFTER_TURNS into a rolling summary.

    Called after a successful agent run.  Uses a cheap GPT-4o-mini completion.
    No-op when there are fewer turns than the fold threshold.
    """
    total_rows = await db.exec(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.created_at)
    )
    all_msgs = total_rows.all()

    if len(all_msgs) <= SUMMARY_FOLD_AFTER_TURNS:
        return  # not enough turns yet

    old_msgs = all_msgs[:-SUMMARY_FOLD_AFTER_TURNS]
    if not old_msgs:
        return

    # Build a condensed transcript of the old turns
    peer_label = conversation.peer_name or conversation.peer_id or "Customer"
    old_lines: list[str] = []
    for msg in old_msgs:
        label = "Agent" if msg.direction.value == "outbound" else peer_label
        body = msg.text or "[media]"
        old_lines.append(f"{label}: {body}")

    existing_summary = conversation.summary or ""
    prompt_parts = []
    if existing_summary:
        prompt_parts.append(f"Previous summary:\n{existing_summary}\n")
    prompt_parts.append("New turns to fold in:\n" + "\n".join(old_lines))
    prompt_parts.append(
        f"\nWrite an updated summary of the conversation so far in at most "
        f"{SUMMARY_MAX_CHARS} characters. Focus on: what the customer wants, "
        "key facts shared, and current status. Use past tense."
    )

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
            max_tokens=300,
        )
        new_summary = resp.choices[0].message.content or ""

        conversation.summary = new_summary[:SUMMARY_MAX_CHARS]
        conversation.summary_through_at = old_msgs[-1].created_at
        db.add(conversation)
        await db.commit()
    except Exception as exc:
        log.warning("fold_to_summary failed for conv %s: %s", conversation.id, exc)


# ── get_open_conversation ─────────────────────────────────────────────────────


async def get_or_create_conversation(
    db: AsyncSession,
    connector: Connector,
    peer: Peer,
) -> UUID:
    """Look up or create the conversation for (connector, peer). Returns its id."""
    row = await db.exec(
        select(Conversation.id).where(
            Conversation.connector_id == connector.id,
            Conversation.peer_id == peer.peer_id,
            Conversation.thread_key == peer.thread_key,
        )
    )
    existing = row.first()
    if existing:
        return existing

    conv = Conversation(
        org_id=connector.org_id,
        connector_id=connector.id,
        channel=connector.type.value,
        peer_id=peer.peer_id,
        peer_name=peer.peer_name,
        thread_key=peer.thread_key,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv.id
