"""
Media ingest pipeline
=====================
Fetches provider-hosted media bytes (before expiry), processes them by kind, and updates
the ConversationMessage attachment entry with the result.

Processors per kind
-------------------
audio   → Whisper transcription via org's OpenAI key (`whisper-1`).
          Falls back to marker when no OpenAI key or fetch fails.
image   → GPT-4o-mini vision one-paragraph description via org's OpenAI key.
          Falls back to marker.
document → `knowledge.extract_text` for .pdf/.txt/.md/.csv, truncated to 4 000 chars.
location → `lat, lng — https://maps.google.com/...` text.
sticker, video, contact, other → marker only.

Cost tracking
-------------
`cost_usd` is stored on each attachment dict and added to the session's cost total by the
caller if a session is active.

Worker integration
------------------
`process_pending_media(db)` is called once per tick before `claim_inbound`.  It selects
up to `BATCH` ConversationMessages that have at least one attachment with status="pending",
processes each, and commits.  Hard cap: 30 seconds per call.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import media_store
from app.db.models import (
    Connector,
    ConnectorType,
    ConversationMessage,
    MessageKind,
    Organization,
)

log = logging.getLogger(__name__)

PROCESS_BATCH = 5         # messages per tick
PROCESS_TIMEOUT = 30.0    # hard cap per `process_pending_media` call
FETCH_TIMEOUT = 15.0      # per-file HTTP fetch
AUDIO_MAX_BYTES = 25 * 1024 * 1024   # Whisper 25 MB limit
DOCUMENT_MAX_CHARS = 4_000

# Whisper pricing: $0.006 per minute (https://openai.com/api/pricing)
WHISPER_COST_PER_SECOND = 0.006 / 60


# ── Channel-specific fetchers ─────────────────────────────────────────────────

async def _fetch_telegram_file(provider_ref: str, bot_token: str) -> bytes:
    """Resolve a Telegram file_id to a URL and download it."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        info = await client.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": provider_ref},
        )
        info.raise_for_status()
        data = info.json()
        file_path = data.get("result", {}).get("file_path", "")
        if not file_path:
            raise ValueError("Telegram: empty file_path in getFile response")
        url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        resp = await client.get(url, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.content


async def _fetch_whatsapp_file(provider_ref: str, token: str) -> bytes:
    """Download a WhatsApp Cloud API media file by its id."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        # Step 1: get the download URL from the media id
        meta = await client.get(
            f"https://graph.facebook.com/v20.0/{provider_ref}",
            headers={"Authorization": f"Bearer {token}"},
        )
        meta.raise_for_status()
        url = meta.json().get("url", "")
        if not url:
            raise ValueError("WhatsApp: no download URL in media response")
        # Step 2: download
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=FETCH_TIMEOUT
        )
        resp.raise_for_status()
        return resp.content


async def _fetch_instagram_file(provider_ref: str) -> bytes:
    """Download an Instagram media file (URL is already resolved by Meta webhook)."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        resp = await client.get(provider_ref, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.content


async def _fetch_twilio_file(provider_ref: str, sid: str, token: str) -> bytes:
    """Download a Twilio MMS media URL (basic auth with account SID + token)."""
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, auth=(sid, token)) as client:
        resp = await client.get(provider_ref, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.content


async def _fetch_bytes(
    provider_ref: str,
    channel: str,
    connector_config: dict[str, Any],
) -> bytes:
    """Dispatch to the right channel fetcher based on `channel`."""
    match channel:
        case "telegram_bot":
            token = connector_config.get("bot_token", "")
            return await _fetch_telegram_file(provider_ref, token)
        case "telegram_client":
            # Client-mode files come with a direct URL from Telethon download
            return await _fetch_instagram_file(provider_ref)
        case "whatsapp":
            token = connector_config.get("access_token", "")
            return await _fetch_whatsapp_file(provider_ref, token)
        case "instagram":
            return await _fetch_instagram_file(provider_ref)
        case "twilio":
            sid = connector_config.get("account_sid", "")
            token = connector_config.get("auth_token", "")
            return await _fetch_twilio_file(provider_ref, sid, token)
        case _:
            raise ValueError(f"No fetcher for channel {channel!r}")


# ── Processors ────────────────────────────────────────────────────────────────

async def _transcribe_audio(
    data: bytes,
    openai_key: str,
    mime: str = "audio/ogg",
    duration: float = 0,
) -> tuple[str, float]:
    """Transcribe audio bytes via Whisper.  Returns (transcript, cost_usd)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=openai_key)
    ext = media_store._ext_for_mime(mime, "ogg")
    filename = f"audio.{ext}"
    audio_io = io.BytesIO(data)
    audio_io.name = filename

    result = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_io,
        response_format="text",
    )
    transcript = str(result).strip()
    cost = WHISPER_COST_PER_SECOND * max(duration, len(data) / 16_000)
    return transcript, cost


async def _describe_image(data: bytes, openai_key: str, mime: str) -> str:
    """Describe an image via GPT-4o-mini vision."""
    import base64

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=openai_key)
    b64 = base64.b64encode(data).decode()
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "low"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe this image in one concise paragraph. "
                            "Focus on the subject, any text visible, and relevant context. "
                            "Do not start with 'The image shows' or similar phrases."
                        ),
                    },
                ],
            }
        ],
        max_tokens=300,
    )
    return resp.choices[0].message.content or ""


async def _extract_document(data: bytes, filename: str, mime: str) -> str:
    """Extract text from a supported document type."""
    from app.core.knowledge import extract_text

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in {"pdf", "txt", "md", "csv"}:
        # Unsupported format — return a marker
        return f"[document: {filename} — format not supported for text extraction]"
    text = extract_text(data, filename)
    return text[:DOCUMENT_MAX_CHARS]


def _format_location(provider_ref: str, caption: str = "") -> str:
    parts = provider_ref.split(",")
    try:
        lat, lng = parts[0].strip(), parts[1].strip()
        maps_url = f"https://www.google.com/maps?q={lat},{lng}"
        label = caption or f"{lat}, {lng}"
        return f"[location] {label} — {maps_url}"
    except (IndexError, ValueError):
        return f"[location] {provider_ref}"


# ── process_attachment ─────────────────────────────────────────────────────────

async def process_attachment(
    att: dict[str, Any],
    channel: str,
    connector_config: dict[str, Any],
    org_id: UUID,
    conv_id: UUID,
    msg_id: UUID,
    idx: int,
    openai_key: str | None,
) -> dict[str, Any]:
    """Process one attachment dict in-place.  Returns the updated dict."""
    att = dict(att)  # don't mutate the original
    kind_str = att.get("kind", "other")
    kind = MessageKind(kind_str) if kind_str in MessageKind.__members__ else MessageKind.other
    provider_ref = att.get("provider_ref", "")
    mime = att.get("mime", "")
    filename = att.get("filename", f"attachment.{media_store._ext_for_mime(mime)}")
    duration = float(att.get("duration", 0))

    # Location: no fetch needed
    if kind == MessageKind.location:
        att["text"] = _format_location(provider_ref, att.get("caption", ""))
        att["status"] = "ready"
        return att

    # Sticker / contact / other: marker only
    if kind in (MessageKind.sticker, MessageKind.contact, MessageKind.other, MessageKind.video):
        kind_label = kind.value
        att["text"] = f"[{kind_label}]"
        att["status"] = "ready"
        return att

    # Fetch bytes
    try:
        data = await _fetch_bytes(provider_ref, channel, connector_config)
    except Exception as exc:
        log.warning("media fetch failed for %s/%s: %s", channel, provider_ref, exc)
        att["status"] = "failed"
        att["error"] = str(exc)
        att["text"] = _failure_marker(kind)
        return att

    # Size guard
    if len(data) > media_store.MAX_BYTES:
        att["status"] = "failed"
        att["error"] = f"File too large: {len(data)} bytes"
        att["text"] = _failure_marker(kind)
        return att

    # Store bytes
    try:
        stored_path = media_store.put(org_id, conv_id, msg_id, idx, data, mime)
        att["stored_path"] = stored_path
        att["size"] = len(data)
    except Exception as exc:
        att["stored_path"] = ""
        log.warning("media store failed: %s", exc)

    # Process by kind
    try:
        if kind == MessageKind.audio:
            if not openai_key:
                att["text"] = "[voice message — transcription unavailable: no OpenAI key configured]"
                att["status"] = "unavailable"
                return att
            if len(data) > AUDIO_MAX_BYTES:
                att["text"] = "[voice message — too long to transcribe]"
                att["status"] = "failed"
                return att
            transcript, cost = await _transcribe_audio(data, openai_key, mime, duration)
            att["text"] = f'[voice] "{transcript}"'
            att["cost_usd"] = cost
            att["status"] = "ready"

        elif kind == MessageKind.image:
            if not openai_key:
                att["text"] = "[image — description unavailable: no OpenAI key configured]"
                att["status"] = "unavailable"
                return att
            description = await _describe_image(data, openai_key, mime or "image/jpeg")
            att["text"] = f"[image] {description}"
            att["status"] = "ready"

        elif kind == MessageKind.document:
            text = await _extract_document(data, filename, mime)
            att["text"] = f"[document: {filename}]\n{text}"
            att["status"] = "ready"

        else:
            att["text"] = _failure_marker(kind)
            att["status"] = "ready"

    except Exception as exc:
        log.warning("media processing failed kind=%s: %s", kind, exc)
        att["status"] = "failed"
        att["error"] = str(exc)
        att["text"] = _failure_marker(kind)

    return att


def _failure_marker(kind: MessageKind) -> str:
    labels = {
        MessageKind.audio: "voice message — could not transcribe",
        MessageKind.image: "image — could not describe",
        MessageKind.document: "document — could not extract text",
    }
    return f"[{labels.get(kind, kind.value)} — please describe in text]"


# ── process_pending_media ──────────────────────────────────────────────────────

async def process_pending_media(db: AsyncSession, *, batch: int = PROCESS_BATCH) -> int:
    """Process pending attachments from ConversationMessage rows.

    Returns the number of attachments processed.
    Called once per worker tick before claim_inbound.
    Hard-capped at PROCESS_TIMEOUT seconds total.
    """
    # Find messages with at least one pending attachment
    rows = await db.exec(
        select(ConversationMessage)
        .where(ConversationMessage.attachments.contains([{"status": "pending"}]))
        .limit(batch)
    )
    messages = rows.all()
    if not messages:
        return 0

    processed = 0
    deadline = asyncio.get_event_loop().time() + PROCESS_TIMEOUT

    for msg in messages:
        if asyncio.get_event_loop().time() >= deadline:
            log.warning("process_pending_media: time cap reached, deferring remaining")
            break

        # Load the connector for this conversation's channel
        from app.db.models import Conversation
        from app.core.crypto import decrypt_json

        conv = await db.get(Conversation, msg.conversation_id)
        if conv is None:
            continue

        connector = await db.get(Connector, conv.connector_id)
        if connector is None:
            continue

        # Look up org's OpenAI key
        org = await db.get(Organization, msg.org_id)
        openai_key: str | None = None
        if org is not None:
            # Try to find an openai connector for this org
            from sqlmodel import select as _select
            from app.db.models import Connector as _Conn, ConnectorType as _CT
            openai_rows = await db.exec(
                _select(_Conn).where(
                    _Conn.org_id == org.id,
                    _Conn.type == _CT.openai,
                )
            )
            oc = openai_rows.first()
            if oc is not None:
                try:
                    oc_cfg = decrypt_json(oc.config)
                    openai_key = oc_cfg.get("api_key") or oc_cfg.get("key")
                except Exception:
                    pass

        connector_config = {}
        try:
            connector_config = decrypt_json(connector.config)
        except Exception:
            pass

        updated_attachments = []
        for idx, att in enumerate(msg.attachments or []):
            if att.get("status") != "pending":
                updated_attachments.append(att)
                continue
            try:
                updated = await process_attachment(
                    att,
                    channel=conv.channel,
                    connector_config=connector_config,
                    org_id=msg.org_id,
                    conv_id=msg.conversation_id,
                    msg_id=msg.id,
                    idx=idx,
                    openai_key=openai_key,
                )
                updated_attachments.append(updated)
                processed += 1
            except Exception as exc:
                att_copy = dict(att)
                att_copy["status"] = "failed"
                att_copy["error"] = str(exc)
                att_copy["text"] = _failure_marker(
                    MessageKind(att.get("kind", "other"))
                )
                updated_attachments.append(att_copy)
                processed += 1

        msg.attachments = updated_attachments
        db.add(msg)

    if processed:
        await db.commit()
        log.info("process_pending_media: processed %d attachment(s)", processed)

    return processed
