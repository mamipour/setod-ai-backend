"""
Inbound webhooks
================
Public, unauthenticated endpoints that providers call. Two rules govern everything here.

**Verify before trusting.** These URLs are reachable by anyone who guesses them, and a forged
request would make someone else's agent act on attacker-supplied text. Telegram is verified
with a per-connector secret header registered at `setWebhook`; Twilio with the HMAC signature
it sends over the request. Both comparisons are constant time.

**Persist, then acknowledge.** Providers retry anything slow — Telegram within seconds — and
an agent run takes far longer than that. So a webhook writes the event and returns 200
immediately; the worker runs the agent. Retries collapse onto the same row via the unique
constraint on the provider's own id, so a redelivery cannot produce a second run.
"""

import hmac
import logging
import secrets
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from twilio.request_validator import RequestValidator

from app.api.auth.dependencies import get_current_user
from app.config import settings
from app.core.crypto import decrypt_json, encrypt_json
from app.db.models import (
    Connector,
    ConnectorType,
    InboundEvent,
    OrganizationMember,
    User,
)
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Telegram truncates messages it forwards; agents get the text, not an essay.
MAX_TEXT = 4000


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _connector_of_type(
    db: AsyncSession, connector_id: UUID, kind: ConnectorType
) -> Connector:
    connector = await db.get(Connector, connector_id)
    if connector is None or connector.type != kind:
        # Deliberately identical to the signature failure below: an unauthenticated caller
        # should not be able to probe which connector ids exist.
        raise HTTPException(status_code=404, detail="Unknown webhook")
    return connector


async def _record(
    db: AsyncSession,
    connector: Connector,
    external_id: str,
    text: str,
    sender: str,
    payload: dict,
) -> bool:
    """Store an inbound event. False means it was a duplicate and is already queued."""
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


def _webhook_base_url() -> str:
    if not settings.public_base_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "PUBLIC_BASE_URL is not set. Webhook providers need a URL they can reach — "
                "in development, expose the API with a tunnel and set it to that address."
            ),
        )
    return settings.public_base_url.rstrip("/")


# ── Telegram ───────────────────────────────────────────────────────────────────

class RegisterResult(BaseModel):
    ok: bool
    detail: str
    webhook_url: str = ""


@router.post("/telegram/{connector_id}/register", response_model=RegisterResult)
async def register_telegram_webhook(
    connector_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Point a Telegram bot at this server.

    Generates a per-connector secret that Telegram echoes back on every delivery, which is how
    the receiving endpoint knows a request really came from Telegram.
    """
    connector = await _connector_of_type(db, connector_id, ConnectorType.telegram_bot)

    member = await db.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == connector.org_id,
            OrganizationMember.user_id == current_user.id,
        )
    )
    if member.first() is None:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")

    config = decrypt_json(connector.config)
    secret = config.get("webhook_secret") or secrets.token_urlsafe(32)
    url = f"{_webhook_base_url()}/webhooks/telegram/{connector.id}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{config['bot_token']}/setWebhook",
            json={
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                # Old updates queued while the bot was unregistered are stale by definition;
                # replaying them would have agents answering hours-old messages on setup.
                "drop_pending_updates": True,
            },
            timeout=15,
        )

    data = resp.json()
    if not data.get("ok"):
        return RegisterResult(ok=False, detail=data.get("description", "Telegram refused the URL"))

    config["webhook_secret"] = secret
    connector.config = encrypt_json(config)
    db.add(connector)
    await db.commit()
    return RegisterResult(ok=True, detail="Telegram will now deliver messages here", webhook_url=url)


@router.post("/telegram/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_telegram(
    connector_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
):
    connector = await _connector_of_type(db, connector_id, ConnectorType.telegram_bot)
    config = decrypt_json(connector.config)

    expected = config.get("webhook_secret")
    if not expected or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected):
        log.warning("rejected unsigned Telegram delivery for connector %s", connector_id)
        raise HTTPException(status_code=404, detail="Unknown webhook")

    update = await request.json()
    message = update.get("message") or {}
    text = message.get("text") or message.get("caption") or ""
    if not text:
        # Stickers, joins, edits. Acknowledged so Telegram stops retrying, but not queued.
        return

    chat = message.get("chat") or {}
    sender = chat.get("username") or str(chat.get("id", ""))
    await _record(db, connector, str(update["update_id"]), text, sender, update)


# ── Twilio ─────────────────────────────────────────────────────────────────────

@router.post("/twilio/{connector_id}")
async def receive_twilio(
    connector_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    x_twilio_signature: Annotated[str | None, Header()] = None,
):
    """Inbound SMS. Configure this URL on the number's Messaging webhook in the Twilio console.

    Returns empty TwiML: Twilio treats the response body as instructions to the caller, and
    anything non-empty would send an automatic reply before the agent has even run.
    """
    connector = await _connector_of_type(db, connector_id, ConnectorType.twilio)
    config = decrypt_json(connector.config)

    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    validator = RequestValidator(config["auth_token"])
    url = f"{_webhook_base_url()}/webhooks/twilio/{connector.id}"
    if not validator.validate(url, params, x_twilio_signature or ""):
        log.warning("rejected unsigned Twilio delivery for connector %s", connector_id)
        raise HTTPException(status_code=404, detail="Unknown webhook")

    text = params.get("Body", "")
    if text:
        await _record(
            db, connector, params["MessageSid"], text, params.get("From", ""), params
        )

    return Response(content="<Response></Response>", media_type="application/xml")
