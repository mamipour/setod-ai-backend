"""
Generic inbound hooks
=====================
Public, unauthenticated routes that external systems call to push events.

POST /hooks/{connector_id}          — Generic webhook receiver (HMAC-SHA256 optional)
GET  /hooks/whatsapp/{connector_id} — Meta webhook verification handshake
POST /hooks/whatsapp/{connector_id} — Meta WhatsApp inbound message delivery

Design rules (same as /webhooks/):
  - Verify before trusting: HMAC for generic webhooks, verify_token for WhatsApp.
  - Persist then acknowledge: record the event, return 200/202 immediately.
  - Deduplication via on_conflict_do_nothing keeps replays safe.
"""

import hashlib
import hmac
import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.dialects.postgresql import insert
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crypto import decrypt_json
from app.db.models import Connector, ConnectorType, InboundEvent
from app.db.session import get_session

log = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks", tags=["hooks"])

MAX_TEXT = 4000


async def _get_connector(db: AsyncSession, connector_id: UUID, kind: ConnectorType) -> Connector:
    connector = await db.get(Connector, connector_id)
    if connector is None or connector.type != kind:
        raise HTTPException(status_code=404, detail="Unknown endpoint")
    return connector


async def _record(
    db: AsyncSession,
    connector: Connector,
    external_id: str,
    text: str,
    sender: str,
    payload: dict,
) -> bool:
    stmt = (
        insert(InboundEvent)
        .values(
            connector_id=connector.id,
            org_id=connector.org_id,
            external_id=external_id,
            text=text[:MAX_TEXT],
            sender=sender,
            payload=payload,
        )
        .on_conflict_do_nothing(constraint="uq_inbound_event")
        .returning(InboundEvent.id)
    )
    result = await db.exec(stmt)
    await db.commit()
    return result.first() is not None


# ── Generic webhook ────────────────────────────────────────────────────────────

def _external_id_from(request: Request, body: bytes) -> str:
    """Best-effort unique id for this delivery.

    Priority:
      1. X-Webhook-Id, X-Request-Id, or Idempotency-Key header (provider-supplied)
      2. SHA-256 of the first 512 bytes of the body (deterministic, safe for replays)
    """
    for header in ("x-webhook-id", "x-request-id", "idempotency-key"):
        v = request.headers.get(header)
        if v:
            return v[:255]
    return hashlib.sha256(body[:512]).hexdigest()


@router.post("/{connector_id}", status_code=status.HTTP_202_ACCEPTED)
async def receive_generic_webhook(
    connector_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
):
    """Accept a payload from any external service. If the connector has a signing secret,
    the request must carry a valid X-Hub-Signature-256 header (GitHub-style HMAC-SHA256).
    Returns 202 Accepted immediately; agents are triggered asynchronously by the worker.
    """
    connector = await db.get(Connector, connector_id)
    if connector is None or connector.type != ConnectorType.webhook:
        # Identical error for unknown id and wrong type — no enumeration oracle.
        raise HTTPException(status_code=404, detail="Unknown endpoint")

    body = await request.body()
    config = decrypt_json(connector.config)
    secret_hash = config.get("secret_hash")

    raw_secret = config.get("secret")  # stored encrypted via encrypt_json
    if raw_secret:
        # Validate GitHub-style X-Hub-Signature-256: "sha256=<hex>"
        if not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")
        _, _, sig_hex = x_hub_signature_256.partition("=")
        expected = hmac.new(raw_secret.encode(), body, "sha256").hexdigest()
        if not hmac.compare_digest(sig_hex, expected):
            log.warning("invalid HMAC signature for webhook connector %s", connector_id)
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body.decode(errors="replace")[:2000]}

    text = json.dumps(payload, ensure_ascii=False, indent=2)[:MAX_TEXT]
    external_id = _external_id_from(request, body)
    sender = request.headers.get("x-sender") or str(request.client.host if request.client else "")

    await _record(db, connector, external_id, text, sender, payload)
    return Response(status_code=status.HTTP_202_ACCEPTED)


# ── WhatsApp ───────────────────────────────────────────────────────────────────

@router.get("/whatsapp/{connector_id}")
async def whatsapp_verify(
    connector_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
):
    """Meta webhook verification handshake. Meta sends hub.challenge; we echo it back."""
    connector = await db.get(Connector, connector_id)
    if connector is None or connector.type != ConnectorType.whatsapp:
        raise HTTPException(status_code=404, detail="Unknown endpoint")

    config = decrypt_json(connector.config)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode != "subscribe" or token != config.get("verify_token"):
        raise HTTPException(status_code=403, detail="Verification failed")

    return Response(content=challenge, media_type="text/plain")


@router.post("/whatsapp/{connector_id}", status_code=status.HTTP_200_OK)
async def receive_whatsapp(
    connector_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Receive inbound WhatsApp messages from Meta's Cloud API webhook."""
    connector = await db.get(Connector, connector_id)
    if connector is None or connector.type != ConnectorType.whatsapp:
        raise HTTPException(status_code=404, detail="Unknown endpoint")

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)  # Malformed — ack so Meta stops retrying

    # Meta wraps messages inside entry → changes → value → messages
    try:
        entry = body.get("entry", [{}])[0]
        change = entry.get("changes", [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
    except (IndexError, AttributeError):
        return Response(status_code=200)

    for msg in messages:
        if msg.get("type") != "text":
            continue
        text = msg.get("text", {}).get("body", "")
        if not text:
            continue
        msg_id = msg.get("id", "")
        sender = msg.get("from", "")
        await _record(db, connector, msg_id, text, sender, msg)
        log.info("WhatsApp message %s from %s queued", msg_id, sender)

    # Meta requires a 200 response, not 202
    return Response(status_code=200)
