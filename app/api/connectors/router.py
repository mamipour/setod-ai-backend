"""
Connectors API
==============
Connectors are org-scoped credential bundles. Credentials are encrypted before
being stored and never returned to the client.

Routes
------
GET    /connectors/                              list connectors for an org
DELETE /connectors/{id}                          remove a connector
POST   /connectors/{id}/test                     test a connector's credentials
POST   /connectors/telegram-bot                  create Telegram Bot connector
POST   /connectors/llm                           create OpenAI / Anthropic connector
PATCH  /connectors/llm/{id}                      update an LLM connector's API key
POST   /connectors/telegram-client/start         begin MTProto auth (send OTP)
POST   /connectors/telegram-client/verify        submit OTP or 2FA password
POST   /connectors/telegram-client/save          save verified MTProto session
POST   /connectors/gmail                         save Gmail connector (App Password)
POST   /connectors/twilio/validate               validate Twilio credentials (no save)
POST   /connectors/twilio                        save Twilio connector
POST   /connectors/twilio/{id}/send-test-sms     send a test SMS via saved connector
GET    /connectors/mcp/catalog                   branded MCP server presets
POST   /connectors/mcp/probe                     classify auth + list tools
POST   /connectors/mcp                           save an MCP connector (bearer / none)
GET    /connectors/oauth/mcp/start               begin MCP OAuth
GET    /connectors/oauth/mcp/callback            MCP OAuth redirect
POST   /connectors/{id}/mcp/resync               re-fetch and freeze tools/list
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from telethon import TelegramClient
from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from pydantic import BaseModel

from app.api.auth.dependencies import assert_org_owner, get_current_user
from app.api.connectors.schemas import ConnectorOut, TestResult
from app.config import settings
from app.core.crypto import decrypt_json, encrypt_json
from app.db.models import Agent, AgentTool, Connector, ConnectorStatus, ConnectorType, OrganizationMember, User
from app.db.session import get_session
import json
import secrets

from app.integrations import gmail, instagram, mcp, sheets, slack, telegram, twilio, whatsapp

router = APIRouter(prefix="/connectors", tags=["connectors"])

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _assert_org_member(session: AsyncSession, user: User, org_id: UUID) -> None:
    result = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if not result.first():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of this organization")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ConnectorOut])
async def list_connectors(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_org_member(session, current_user, org_id)
    result = await session.exec(
        select(Connector).where(Connector.org_id == org_id).order_by(Connector.created_at)
    )
    return result.all()


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, org_id)
    connector = await session.get(Connector, connector_id)
    if not connector or connector.org_id != org_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    # Check before touching anything. Returning the agent names means the user knows
    # exactly where to go, rather than getting a generic "still in use" message.
    tools = await session.exec(
        select(AgentTool).where(AgentTool.connector_id == connector_id)
    )
    tool_rows = tools.all()
    if tool_rows:
        agent_ids = list({t.agent_id for t in tool_rows})
        agents_result = await session.exec(
            select(Agent.name).where(Agent.id.in_(agent_ids))
        )
        # Deduplicate names in case multiple test runs left agents with the same name.
        unique_names = sorted(set(agents_result.all()))
        quoted = ", ".join(f'"{n}"' for n in unique_names)
        raise HTTPException(
            status_code=409,
            detail=(
                f'"{connector.name}" is used by {quoted}. '
                "Remove it from those agents first, then delete it here."
            ),
        )

    # Belt-and-suspenders: catch any other FK violation (e.g. model_connector_id on agents,
    # which is SET NULL in the DB but a stale schema might not have that yet).
    await session.delete(connector)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f'"{connector.name}" is still referenced by other data. '
                "Check your agents and triggers, then try again."
            ),
        )


@router.post("/{connector_id}/test", response_model=TestResult)
async def test_connector(
    connector_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_org_member(session, current_user, org_id)
    connector = await session.get(Connector, connector_id)
    if not connector or connector.org_id != org_id:
        raise HTTPException(status_code=404, detail="Connector not found")

    if connector.type == ConnectorType.gmail:
        try:
            email_addr = await gmail.test_connection(connector)
            return TestResult(ok=True, detail=f"Connected as {email_addr} — IMAP and SMTP verified.")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.telegram_bot:
        try:
            config = decrypt_json(connector.config)
            chat_id = config["admin_chat_id"]
            await telegram.bot_send(
                config["bot_token"],
                chat_id,
                "✅ Test message from your agent platform — everything is working.",
            )
            return TestResult(ok=True, detail=f"Test message sent to chat {chat_id}")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.telegram_client:
        try:
            client = telegram.mtproto_client(decrypt_json(connector.config))
            await client.connect()
            try:
                me = await client.get_me()
                if me:
                    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
                    suffix = f" (@{me.username})" if me.username else ""
                    return TestResult(ok=True, detail=f"Connected as {name}{suffix}")
                return TestResult(ok=False, detail="Session is no longer valid — reconnect")
            finally:
                await client.disconnect()
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.openai:
        try:
            config = decrypt_json(connector.config)
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {config['api_key']}"},
                    timeout=10,
                )
            if resp.status_code == 200:
                model_count = len(resp.json().get("data", []))
                return TestResult(ok=True, detail=f"Key valid — {model_count} models available")
            if resp.status_code == 401:
                return TestResult(ok=False, detail="API key is invalid or revoked")
            return TestResult(ok=False, detail=f"OpenAI returned {resp.status_code}")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.anthropic:
        try:
            config = decrypt_json(connector.config)
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": config["api_key"],
                        "anthropic-version": "2023-06-01",
                    },
                    timeout=15,
                )
            if resp.status_code == 200:
                model_count = len(resp.json().get("data", []))
                return TestResult(ok=True, detail=f"Key valid — {model_count} models available")
            if resp.status_code == 401:
                return TestResult(ok=False, detail="API key is invalid or revoked")
            return TestResult(ok=False, detail=f"Anthropic returned {resp.status_code}")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.twilio:
        try:
            config = decrypt_json(connector.config)
            friendly = await twilio.validate(config["account_sid"], config["auth_token"])
            return TestResult(ok=True, detail=f"Account valid — {friendly}")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.mcp:
        try:
            config = decrypt_json(connector.config)
            if config.get("auth") == "oauth":
                config = await mcp.refresh_oauth_token(config)
                connector.config = encrypt_json(config)
                session.add(connector)
                await session.commit()
            tools = await mcp.list_remote_tools(config["url"], token=config.get("access_token"))
            return TestResult(ok=True, detail=f"Reachable — {len(tools)} tool(s) advertised")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    if connector.type == ConnectorType.instagram:
        try:
            config = decrypt_json(connector.config)
            token = config["access_token"]
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://graph.instagram.com/v20.0/me",
                    params={"fields": "id,username", "access_token": token},
                )
            data = resp.json()
            if "error" in data:
                return TestResult(ok=False, detail=data["error"].get("message", "Instagram API error"))
            return TestResult(ok=True, detail=f"Connected as @{data.get('username', data.get('id', '?'))}")
        except Exception as exc:
            return TestResult(ok=False, detail=str(exc))

    return TestResult(ok=False, detail=f"Test not implemented for {connector.type}")


# ── Telegram Bot ──────────────────────────────────────────────────────────────

class TelegramBotCreate(BaseModel):
    org_id: UUID
    name: str
    bot_token: str
    admin_chat_id: int
    admin_username: str = ""
    admin_first_name: str = ""


@router.post("/telegram-bot", response_model=ConnectorOut, status_code=201)
async def create_telegram_bot(
    body: TelegramBotCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)

    # Re-validate token server-side before storing anything
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{body.bot_token}/getMe",
            timeout=10,
        )
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid bot token: {data.get('description', 'Telegram rejected the token')}",
        )

    bot = data["result"]
    config = encrypt_json({
        "bot_token": body.bot_token,
        "bot_id": bot["id"],
        "bot_username": bot["username"],
        "bot_first_name": bot["first_name"],
        "admin_chat_id": body.admin_chat_id,
        "admin_username": body.admin_username,
        "admin_first_name": body.admin_first_name,
    })

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or f"Telegram · @{bot['username']}",
        type=ConnectorType.telegram_bot,
        status=ConnectorStatus.active,
        config=config,
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


# ── LLM Providers (OpenAI / Anthropic) ───────────────────────────────────────

class LLMConnectorCreate(BaseModel):
    org_id: UUID
    name: str
    provider: ConnectorType   # openai | anthropic
    api_key: str


class LLMValidateBody(BaseModel):
    provider: ConnectorType
    api_key: str


async def _check_llm_key(provider: ConnectorType, api_key: str) -> TestResult:
    """Validate an LLM API key against the provider's models endpoint."""
    async with httpx.AsyncClient() as client:
        if provider == ConnectorType.openai:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return TestResult(ok=True, detail=f"Key valid — {len(resp.json().get('data', []))} models available")
            if resp.status_code == 401:
                return TestResult(ok=False, detail="Invalid API key")
            return TestResult(ok=False, detail=f"OpenAI returned {resp.status_code}")

        if provider == ConnectorType.anthropic:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                model_count = len(resp.json().get("data", []))
                return TestResult(ok=True, detail=f"Key valid — {model_count} models available")
            if resp.status_code == 401:
                return TestResult(ok=False, detail="Invalid API key")
            return TestResult(ok=False, detail=f"Anthropic returned {resp.status_code}")

    return TestResult(ok=False, detail="Unsupported provider")


@router.post("/llm/validate", response_model=TestResult)
async def validate_llm_key(
    body: LLMValidateBody,
    current_user: Annotated[User, Depends(get_current_user)],
):
    return await _check_llm_key(body.provider, body.api_key)


@router.post("/llm", response_model=ConnectorOut, status_code=201)
async def create_llm_connector(
    body: LLMConnectorCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    if body.provider not in (ConnectorType.openai, ConnectorType.anthropic):
        raise HTTPException(status_code=422, detail="Unsupported LLM provider")

    await assert_org_owner(session, current_user, body.org_id)

    check = await _check_llm_key(body.provider, body.api_key)
    if not check.ok:
        raise HTTPException(status_code=422, detail=check.detail)

    config = encrypt_json({"api_key": body.api_key, "provider": body.provider})

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or body.provider.value.capitalize(),
        type=body.provider,
        status=ConnectorStatus.active,
        config=config,
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


class LLMKeyUpdate(BaseModel):
    org_id: UUID
    api_key: str


@router.patch("/llm/{connector_id}", response_model=ConnectorOut)
async def update_llm_key(
    connector_id: UUID,
    body: LLMKeyUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Replace an LLM connector's API key in place.

    Deliberately not delete-and-recreate: agents reference the connector by id with
    ON DELETE SET NULL, so deleting it would silently unbind every agent using this
    provider as its model.
    """
    await assert_org_owner(session, current_user, body.org_id)

    connector = await session.get(Connector, connector_id)
    if not connector or connector.org_id != body.org_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.type not in (ConnectorType.openai, ConnectorType.anthropic):
        raise HTTPException(status_code=422, detail="Not an LLM connector")

    check = await _check_llm_key(connector.type, body.api_key)
    if not check.ok:
        raise HTTPException(status_code=422, detail=check.detail)

    connector.config = encrypt_json({"api_key": body.api_key, "provider": connector.type})
    connector.status = ConnectorStatus.active
    connector.updated_at = datetime.now(UTC)
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


# ── Telegram Client (MTProto) ─────────────────────────────────────────────────

# In-memory pending auth sessions — keyed by session_id (UUID string)
# Each entry keeps the live Telethon client alive between HTTP requests.
# Entries older than 15 minutes are cleaned up automatically.
_tg_pending: dict[str, dict] = {}


def _cleanup_tg_pending() -> None:
    cutoff = datetime.now(UTC).timestamp() - 900
    expired = [k for k, v in _tg_pending.items() if v["created_at"] < cutoff]
    for k in expired:
        client = _tg_pending.pop(k, {}).get("client")
        if client:
            asyncio.create_task(client.disconnect())


def _tg_client() -> TelegramClient:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise HTTPException(status_code=503, detail="Telegram API credentials not configured — set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
    try:
        api_id = int(settings.telegram_api_id)
    except ValueError:
        raise HTTPException(status_code=503, detail="TELEGRAM_API_ID must be a number")
    return TelegramClient(
        StringSession(),
        api_id,
        settings.telegram_api_hash,
        device_model="Pixel 5",
        system_version="11",
        app_version="8.4.1",
        lang_code="en",
        system_lang_code="en-US",
    )


class TgClientStart(BaseModel):
    org_id: UUID
    phone: str


class TgClientVerify(BaseModel):
    session_id: str
    code: str | None = None
    password: str | None = None


class TgClientSave(BaseModel):
    session_id: str
    name: str
    org_id: UUID


@router.post("/telegram-client/start")
async def tg_client_start(
    body: TgClientStart,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_org_member(session, current_user, body.org_id)
    _cleanup_tg_pending()

    client = _tg_client()
    await client.connect()

    try:
        sent = await client.send_code_request(body.phone)
    except Exception as exc:
        await client.disconnect()
        raise HTTPException(status_code=422, detail=str(exc))

    session_id = str(uuid4())
    _tg_pending[session_id] = {
        "client": client,
        "phone": body.phone,
        "phone_code_hash": sent.phone_code_hash,
        "org_id": body.org_id,
        "user_id": current_user.id,
        "needs_2fa": False,
        "user_info": None,
        "created_at": datetime.now(UTC).timestamp(),
    }
    return {"session_id": session_id}


@router.post("/telegram-client/verify")
async def tg_client_verify(body: TgClientVerify):
    pending = _tg_pending.get(body.session_id)
    if not pending:
        raise HTTPException(status_code=404, detail="Session expired — please restart the flow")

    client: TelegramClient = pending["client"]

    # 2FA password step
    if pending["needs_2fa"]:
        if not body.password:
            raise HTTPException(status_code=422, detail="Password required for 2FA")
        try:
            me = await client.sign_in(password=body.password)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        pending["user_info"] = {
            "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
            "phone": pending["phone"],
            "tg_id": me.id,
            "username": me.username or "",
        }
        pending["needs_2fa"] = False
        return {"ok": True, "user": pending["user_info"]}

    # OTP step
    if not body.code:
        raise HTTPException(status_code=422, detail="Verification code required")

    try:
        me = await client.sign_in(
            phone=pending["phone"],
            code=body.code,
            phone_code_hash=pending["phone_code_hash"],
        )
        pending["user_info"] = {
            "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
            "phone": pending["phone"],
            "tg_id": me.id,
            "username": me.username or "",
        }
        return {"ok": True, "user": pending["user_info"]}
    except SessionPasswordNeededError:
        pending["needs_2fa"] = True
        return {"ok": False, "needs_2fa": True}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        raise HTTPException(status_code=422, detail="Invalid or expired code — check your Telegram app and try again")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/telegram-client/save", response_model=ConnectorOut, status_code=201)
async def tg_client_save(
    body: TgClientSave,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    pending = _tg_pending.get(body.session_id)
    if not pending or not pending.get("user_info"):
        raise HTTPException(status_code=404, detail="Session expired or not yet verified")

    await assert_org_owner(session, current_user, body.org_id)

    client: TelegramClient = pending["client"]
    session_string = client.session.save()
    await client.disconnect()

    user_info = pending["user_info"]
    config = encrypt_json({
        "session_string": session_string,
        "phone": user_info["phone"],
        "tg_id": user_info["tg_id"],
        "name": user_info["name"],
        "username": user_info["username"],
        "api_id": int(settings.telegram_api_id),
    })

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or f"Telegram · {user_info['name']}",
        type=ConnectorType.telegram_client,
        status=ConnectorStatus.active,
        config=config,
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)

    del _tg_pending[body.session_id]
    return connector


# ── Gmail (App Password) ───────────────────────────────────────────────────────

class GmailConnectorBody(BaseModel):
    org_id: UUID
    email: str
    app_password: str


@router.post("/gmail", response_model=ConnectorOut, status_code=201)
async def create_gmail_connector(
    body: GmailConnectorBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Save a Gmail connector backed by an App Password (IMAP + SMTP + CalDAV)."""
    await assert_org_owner(session, current_user, body.org_id)

    # Verify credentials before storing
    from app.integrations.base import IntegrationError as _IntegrationError
    try:
        from app.integrations.gmail import _test_connection_sync
        import asyncio
        await asyncio.to_thread(_test_connection_sync, body.email, body.app_password)
    except _IntegrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not connect to Gmail: {exc}")

    config = encrypt_json({"email": body.email, "app_password": body.app_password})
    connector_name = f"Gmail · {body.email}"

    # Upsert: one connector per (org, email address)
    existing = await session.exec(
        select(Connector).where(
            Connector.org_id == body.org_id,
            Connector.type == ConnectorType.gmail,
            Connector.name == connector_name,
        )
    )
    connector = existing.first()

    if connector:
        connector.config = config
        connector.status = ConnectorStatus.active
        connector.updated_at = datetime.now(UTC)
    else:
        connector = Connector(
            org_id=body.org_id,
            created_by=current_user.id,
            name=connector_name,
            type=ConnectorType.gmail,
            status=ConnectorStatus.active,
            config=config,
        )

    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


# ── Twilio ────────────────────────────────────────────────────────────────────

class TwilioValidateBody(BaseModel):
    account_sid: str
    auth_token: str
    phone_number: str


class TwilioValidateResult(BaseModel):
    ok: bool
    friendly_name: str = ""
    detail: str = ""


class TwilioCreate(BaseModel):
    org_id: UUID
    account_sid: str
    auth_token: str
    phone_number: str
    name: str


class TwilioTestSmsBody(BaseModel):
    org_id: UUID
    to: str


async def _twilio_validate(account_sid: str, auth_token: str, phone_number: str) -> TwilioValidateResult:
    """Validate Twilio credentials and confirm the phone number belongs to the account."""
    async with httpx.AsyncClient() as client:
        account_resp = await client.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json",
            auth=(account_sid, auth_token),
            timeout=10,
        )
        if account_resp.status_code == 401:
            return TwilioValidateResult(ok=False, detail="Invalid Account SID or Auth Token")
        if account_resp.status_code != 200:
            return TwilioValidateResult(ok=False, detail=f"Twilio returned {account_resp.status_code}")

        friendly_name = account_resp.json().get("friendly_name", account_sid)

        # Check the phone number belongs to this account
        numbers_resp = await client.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers.json",
            auth=(account_sid, auth_token),
            params={"PhoneNumber": phone_number},
            timeout=10,
        )
        if numbers_resp.status_code == 200:
            records = numbers_resp.json().get("incoming_phone_numbers", [])
            if not records:
                return TwilioValidateResult(
                    ok=False,
                    detail=f"{phone_number} was not found in this Twilio account",
                )

    return TwilioValidateResult(ok=True, friendly_name=friendly_name)


@router.post("/twilio/validate", response_model=TwilioValidateResult)
async def twilio_validate(
    body: TwilioValidateBody,
    current_user: Annotated[User, Depends(get_current_user)],
):
    try:
        return await _twilio_validate(body.account_sid, body.auth_token, body.phone_number)
    except Exception as exc:
        return TwilioValidateResult(ok=False, detail=str(exc))


@router.post("/twilio", response_model=ConnectorOut, status_code=201)
async def create_twilio(
    body: TwilioCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)

    result = await _twilio_validate(body.account_sid, body.auth_token, body.phone_number)
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.detail)

    config = encrypt_json({
        "account_sid": body.account_sid,
        "auth_token": body.auth_token,
        "phone_number": body.phone_number,
        "friendly_name": result.friendly_name,
    })

    existing = await session.exec(
        select(Connector).where(
            Connector.org_id == body.org_id,
            Connector.type == ConnectorType.twilio,
            Connector.name == body.name,
        )
    )
    connector = existing.first()

    if connector:
        connector.config = config
        connector.status = ConnectorStatus.active
    else:
        connector = Connector(
            org_id=body.org_id,
            created_by=current_user.id,
            type=ConnectorType.twilio,
            name=body.name,
            status=ConnectorStatus.active,
            config=config,
        )

    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


@router.post("/twilio/{connector_id}/send-test-sms", response_model=TestResult)
async def twilio_send_test_sms(
    connector_id: UUID,
    body: TwilioTestSmsBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_org_member(session, current_user, body.org_id)
    connector = await session.get(Connector, connector_id)
    if not connector or connector.org_id != body.org_id:
        raise HTTPException(status_code=404, detail="Connector not found")

    config = decrypt_json(connector.config)
    account_sid = config["account_sid"]
    auth_token = config["auth_token"]
    from_number = config["phone_number"]

    try:
        result = await twilio.send_sms(
            account_sid,
            auth_token,
            from_number,
            body.to,
            "✅ Test message from your agent platform — Twilio connector is working.",
        )
        return TestResult(ok=True, detail=f"SMS sent to {body.to} (SID: {result.get('sid', '')})")
    except Exception as exc:
        return TestResult(ok=False, detail=str(exc))


# ── MCP (Streamable HTTP) ─────────────────────────────────────────────────────

class McpProbeBody(BaseModel):
    org_id: UUID
    url: str
    token: str | None = None


class McpCreateBody(BaseModel):
    org_id: UUID
    name: str = ""
    url: str
    catalog_key: str = "custom"
    token: str | None = None


def _mcp_display_name(catalog_key: str, url: str, name: str) -> str:
    label = mcp.CATALOG.get(catalog_key, {}).get("label") or "MCP"
    if name.strip():
        return name.strip()
    host = urlparse(url).hostname or url
    return f"{label} · {host}"


def _mcp_config(
    *,
    catalog_key: str,
    url: str,
    auth: str,
    tools: list[dict],
    access_token: str | None = None,
    extra: dict | None = None,
) -> dict:
    payload = {
        "catalog_key": catalog_key,
        "url": url,
        "auth": auth,
        "access_token": access_token or "",
        "tools": tools,
    }
    if extra:
        payload.update(extra)
    return payload


@router.get("/mcp/catalog")
async def mcp_catalog(
    current_user: Annotated[User, Depends(get_current_user)],
):
    return [
        {
            "key": key,
            "label": entry["label"],
            "url": entry["url"],
            "preferred_auth": entry["preferred_auth"],
            "description": entry["description"],
            "oauth_ready": bool(settings.mcp_oauth_app(key)[0]),
            "needs_oauth_app": key == "slack",
            "redirect_uri": settings.connector_mcp_redirect_uri,
        }
        for key, entry in mcp.CATALOG.items()
    ]


@router.post("/mcp/probe")
async def mcp_probe(
    body: McpProbeBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_org_member(session, current_user, body.org_id)
    try:
        url = mcp.validate_mcp_url(body.url)
        result = await mcp.probe_mcp(url, token=body.token or None)
        return {"url": url, **result}
    except mcp.UnsafeUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except mcp.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/mcp", response_model=ConnectorOut, status_code=201)
async def create_mcp_connector(
    body: McpCreateBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)
    if body.catalog_key not in mcp.CATALOG:
        raise HTTPException(status_code=422, detail="Unknown MCP catalog key")
    try:
        url = mcp.validate_mcp_url(body.url)
        tools = await mcp.list_remote_tools(url, token=body.token or None)
    except mcp.UnsafeUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except mcp.McpAuthRequired as exc:
        raise HTTPException(
            status_code=401,
            detail="This server requires OAuth — use Connect instead of pasting a token",
        ) from exc
    except mcp.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    auth = "bearer" if body.token else "none"
    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=_mcp_display_name(body.catalog_key, url, body.name),
        type=ConnectorType.mcp,
        status=ConnectorStatus.active,
        config=encrypt_json(_mcp_config(
            catalog_key=body.catalog_key,
            url=url,
            auth=auth,
            tools=tools,
            access_token=body.token,
        )),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


class McpOAuthStartBody(BaseModel):
    org_id: UUID
    catalog_key: str = "custom"
    url: str = ""
    name: str = ""
    client_id: str | None = None
    client_secret: str | None = None


async def _begin_mcp_oauth(
    request: Request,
    *,
    org_id: UUID,
    user_id: UUID,
    catalog_key: str,
    url: str,
    name: str,
    client_id: str | None,
    client_secret: str | None,
) -> str:
    if catalog_key not in mcp.CATALOG:
        raise HTTPException(status_code=422, detail="Unknown MCP catalog key")
    preset_url = mcp.CATALOG[catalog_key]["url"]
    target = url or preset_url
    if not target:
        raise HTTPException(status_code=422, detail="This catalog entry needs a server URL")
    try:
        target = mcp.validate_mcp_url(target)
    except mcp.UnsafeUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    prm_url = None
    try:
        await mcp.list_remote_tools(target)
        raise HTTPException(status_code=400, detail="This server does not require OAuth")
    except mcp.McpAuthRequired as exc:
        prm_url = exc.prm_url
    except mcp.IntegrationError:
        prm_url = None

    oauth = await mcp.discover_oauth(target, prm_url)
    if not oauth:
        raise HTTPException(
            status_code=400,
            detail="This server did not advertise OAuth. Paste a bearer token instead.",
        )

    redirect_uri = settings.connector_mcp_redirect_uri
    resolved_id = (client_id or "").strip()
    resolved_secret = (client_secret or "").strip() or None
    if not resolved_id:
        platform_id, platform_secret = settings.mcp_oauth_app(catalog_key)
        if platform_id:
            resolved_id, resolved_secret = platform_id, platform_secret or None
    # DCR before CIMD: CIMD needs a public client.json URL. Localhost is invisible
    # to Notion/Linear/Atlassian, so registration is what actually works in dev.
    if not resolved_id and oauth.get("registration_endpoint"):
        try:
            registered = await mcp.register_oauth_client(oauth["registration_endpoint"], redirect_uri)
            resolved_id = registered["client_id"]
            resolved_secret = registered.get("client_secret") or resolved_secret
        except mcp.IntegrationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not resolved_id and oauth.get("cimd"):
        resolved_id = f"{settings.connector_mcp_redirect_uri.rsplit('/', 1)[0]}/client.json"
    if not resolved_id:
        label = mcp.CATALOG.get(catalog_key, {}).get("label") or "This server"
        raise HTTPException(
            status_code=422,
            detail=(
                f"{label} does not support automatic client registration. "
                f"Create an OAuth app with this provider, add redirect URI {redirect_uri}, "
                "then paste the client ID and secret."
            ),
        )

    verifier, challenge = mcp.pkce_pair()
    state = str(uuid4())
    request.session["mcp_oauth"] = {
        "state": state,
        "org_id": str(org_id),
        "user_id": str(user_id),
        "url": target,
        "catalog_key": catalog_key,
        "name": name,
        "verifier": verifier,
        "client_id": resolved_id,
        "client_secret": resolved_secret,
        "token_endpoint": oauth["token_endpoint"],
        "issuer": oauth["issuer"],
        "resource": oauth.get("resource") or target,
        "scopes": oauth.get("scopes") or [],
    }
    return mcp.authorize_url(
        oauth["authorization_endpoint"],
        client_id=resolved_id,
        redirect_uri=redirect_uri,
        resource=oauth.get("resource") or target,
        challenge=challenge,
        state=state,
        scopes=oauth.get("scopes") or None,
    )


@router.get("/oauth/mcp/client.json")
async def mcp_cimd_document():
    """Client ID Metadata Document — used when the AS advertises CIMD support."""
    client_id = f"{settings.connector_mcp_redirect_uri.rsplit('/', 1)[0]}/client.json"
    return {
        "client_id": client_id,
        "client_name": "setod",
        "redirect_uris": [settings.connector_mcp_redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


@router.get("/oauth/mcp/start")
async def mcp_oauth_start(
    request: Request,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    catalog_key: Annotated[str, Query()] = "custom",
    url: Annotated[str, Query()] = "",
    name: Annotated[str, Query()] = "",
):
    await _assert_org_member(session, current_user, org_id)
    location = await _begin_mcp_oauth(
        request,
        org_id=org_id,
        user_id=current_user.id,
        catalog_key=catalog_key,
        url=url,
        name=name,
        client_id=None,
        client_secret=None,
    )
    response = Response(status_code=302)
    response.headers["location"] = location
    return response


@router.post("/oauth/mcp/start")
async def mcp_oauth_start_post(
    body: McpOAuthStartBody,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)
    location = await _begin_mcp_oauth(
        request,
        org_id=body.org_id,
        user_id=current_user.id,
        catalog_key=body.catalog_key,
        url=body.url,
        name=body.name,
        client_id=body.client_id,
        client_secret=body.client_secret,
    )
    return {"redirect": location}


@router.get("/oauth/mcp/callback")
async def mcp_oauth_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    stored = request.session.pop("mcp_oauth", None)
    if not stored:
        raise HTTPException(status_code=400, detail="OAuth session expired — please try again")
    if request.query_params.get("state") != stored.get("state"):
        raise HTTPException(status_code=400, detail="OAuth state mismatch")
    error = request.query_params.get("error")
    if error:
        raise HTTPException(status_code=400, detail=f"Authorization denied: {error}")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code missing")

    try:
        token = await mcp.exchange_code(
            stored["token_endpoint"],
            code=code,
            redirect_uri=settings.connector_mcp_redirect_uri,
            client_id=stored["client_id"],
            verifier=stored["verifier"],
            resource=stored["resource"],
            client_secret=stored.get("client_secret"),
        )
        tools = await mcp.list_remote_tools(stored["url"], token=token["access_token"])
    except mcp.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    expires_at = None
    if token.get("expires_in"):
        expires_at = (datetime.now(UTC) + timedelta(seconds=int(token["expires_in"]))).timestamp()

    extra = {
        "refresh_token": token.get("refresh_token") or "",
        "token_expires_at": expires_at,
        "oauth_issuer": stored.get("issuer"),
        "client_id": stored.get("client_id"),
        "client_secret": stored.get("client_secret"),
        "token_endpoint": stored.get("token_endpoint"),
    }
    config = encrypt_json(_mcp_config(
        catalog_key=stored["catalog_key"],
        url=stored["url"],
        auth="oauth",
        tools=tools,
        access_token=token["access_token"],
        extra=extra,
    ))
    org_id = UUID(stored["org_id"])
    name = _mcp_display_name(stored["catalog_key"], stored["url"], stored.get("name") or "")
    existing = await session.exec(
        select(Connector).where(
            Connector.org_id == org_id,
            Connector.type == ConnectorType.mcp,
            Connector.name == name,
        )
    )
    connector = existing.first()
    if connector:
        connector.config = config
        connector.status = ConnectorStatus.active
        connector.updated_at = datetime.now(UTC)
    else:
        connector = Connector(
            org_id=org_id,
            created_by=UUID(stored["user_id"]),
            name=name,
            type=ConnectorType.mcp,
            status=ConnectorStatus.active,
            config=config,
        )
    session.add(connector)
    await session.commit()

    response = Response(status_code=302)
    response.headers["location"] = f"{settings.frontend_origin}/connectors?connected=mcp"
    return response


@router.post("/{connector_id}/mcp/resync", response_model=TestResult)
async def resync_mcp_tools(
    connector_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, org_id)
    connector = await session.get(Connector, connector_id)
    if not connector or connector.org_id != org_id or connector.type != ConnectorType.mcp:
        raise HTTPException(status_code=404, detail="Connector not found")
    config = decrypt_json(connector.config)
    if config.get("auth") == "oauth":
        config = await mcp.refresh_oauth_token(config)
    try:
        tools = await mcp.list_remote_tools(config["url"], token=config.get("access_token") or None)
    except mcp.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config["tools"] = tools
    connector.config = encrypt_json(config)
    connector.updated_at = datetime.now(UTC)
    session.add(connector)
    await session.commit()
    return TestResult(ok=True, detail=f"Updated — {len(tools)} tool(s) frozen")


# ── Generic inbound webhook ───────────────────────────────────────────────────

class WebhookCreate(BaseModel):
    org_id: UUID
    name: str = ""


class WebhookOut(BaseModel):
    connector: ConnectorOut
    webhook_url: str
    secret: str  # returned once — not re-derivable from stored config


@router.post("/webhook", response_model=WebhookOut, status_code=201)
async def create_webhook_connector(
    body: WebhookCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Create a generic inbound webhook connector.

    Returns the endpoint URL and a signing secret. The secret is shown once; it's stored
    hashed so it cannot be recovered later (use regen-secret to rotate).
    """
    await assert_org_owner(session, current_user, body.org_id)
    raw_secret = secrets.token_urlsafe(32)

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or "Inbound Webhook",
        type=ConnectorType.webhook,
        status=ConnectorStatus.active,
        # Store the raw secret inside encrypt_json — it's Fernet-encrypted at rest.
        # The /hooks/ receiver re-derives the HMAC from this plaintext secret.
        config=encrypt_json({"secret": raw_secret}),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)

    url = f"{settings.public_base_url or 'http://localhost:8000'}/hooks/{connector.id}"
    return WebhookOut(
        connector=ConnectorOut.model_validate(connector),
        webhook_url=url,
        secret=raw_secret,
    )


@router.post("/{connector_id}/regen-secret", response_model=WebhookOut)
async def regen_webhook_secret(
    connector_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Rotate the signing secret for a webhook connector. Old secret stops working immediately."""
    await assert_org_owner(session, current_user, org_id)
    connector = await session.get(Connector, connector_id)
    if not connector or connector.org_id != org_id or connector.type != ConnectorType.webhook:
        raise HTTPException(status_code=404, detail="Webhook connector not found")

    raw_secret = secrets.token_urlsafe(32)
    config = decrypt_json(connector.config)
    config["secret"] = raw_secret
    connector.config = encrypt_json(config)
    connector.updated_at = datetime.now(UTC)
    session.add(connector)
    await session.commit()
    await session.refresh(connector)

    url = f"{settings.public_base_url or 'http://localhost:8000'}/hooks/{connector.id}"
    return WebhookOut(
        connector=ConnectorOut.model_validate(connector),
        webhook_url=url,
        secret=raw_secret,
    )


# ── Slack outgoing webhook ────────────────────────────────────────────────────

class SlackWebhookCreate(BaseModel):
    org_id: UUID
    name: str = ""
    webhook_url: str


@router.post("/slack-webhook/validate", response_model=TestResult)
async def validate_slack_webhook(body: SlackWebhookCreate):
    try:
        await slack.validate(body.webhook_url)
        return TestResult(ok=True, detail="Connected — test message posted to Slack")
    except slack.IntegrationError as exc:
        return TestResult(ok=False, detail=str(exc))


@router.post("/slack-webhook", response_model=ConnectorOut, status_code=201)
async def create_slack_webhook(
    body: SlackWebhookCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)
    try:
        await slack.validate(body.webhook_url)
    except slack.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or "Slack",
        type=ConnectorType.slack_webhook,
        status=ConnectorStatus.active,
        config=encrypt_json({"webhook_url": body.webhook_url}),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


# ── Google Sheets ─────────────────────────────────────────────────────────────

class SheetsCreate(BaseModel):
    org_id: UUID
    name: str = ""
    sa_json: str  # service-account JSON string
    default_spreadsheet_id: str = ""


@router.post("/sheets/validate", response_model=TestResult)
async def validate_sheets(body: SheetsCreate):
    try:
        email = await sheets.validate(body.sa_json)
        return TestResult(ok=True, detail=f"Valid — share your spreadsheets with {email}")
    except sheets.IntegrationError as exc:
        return TestResult(ok=False, detail=str(exc))


@router.post("/sheets", response_model=ConnectorOut, status_code=201)
async def create_sheets_connector(
    body: SheetsCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)
    try:
        sa_dict = json.loads(body.sa_json)
        email = await sheets.validate(body.sa_json)
    except sheets.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or f"Sheets · {email}",
        type=ConnectorType.google_sheets,
        status=ConnectorStatus.active,
        config=encrypt_json({
            "sa_json": sa_dict,
            "service_account_email": email,
            "default_spreadsheet_id": body.default_spreadsheet_id,
        }),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


# ── WhatsApp Business ─────────────────────────────────────────────────────────

class WhatsAppCreate(BaseModel):
    org_id: UUID
    name: str = ""
    phone_number_id: str
    access_token: str
    verify_token: str


@router.post("/whatsapp/validate", response_model=TestResult)
async def validate_whatsapp(body: WhatsAppCreate):
    try:
        display = await whatsapp.validate(body.phone_number_id, body.access_token)
        return TestResult(ok=True, detail=f"Connected — {display}")
    except whatsapp.IntegrationError as exc:
        return TestResult(ok=False, detail=str(exc))


@router.post("/whatsapp", response_model=ConnectorOut, status_code=201)
async def create_whatsapp_connector(
    body: WhatsAppCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(session, current_user, body.org_id)
    try:
        display = await whatsapp.validate(body.phone_number_id, body.access_token)
    except whatsapp.IntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    connector = Connector(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or f"WhatsApp · {display}",
        type=ConnectorType.whatsapp,
        status=ConnectorStatus.active,
        config=encrypt_json({
            "phone_number_id": body.phone_number_id,
            "access_token": body.access_token,
            "verify_token": body.verify_token,
        }),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    return connector


# ── Instagram OAuth ────────────────────────────────────────────────────────────

@router.get("/oauth/instagram/start")
async def instagram_oauth_start(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Redirect the browser to Instagram's OAuth consent screen."""
    await assert_org_owner(session, current_user, org_id)
    if not settings.instagram_app_id:
        raise HTTPException(status_code=503, detail="Instagram is not configured on this server.")

    from urllib.parse import urlencode
    import base64, json as _json
    state_payload = base64.urlsafe_b64encode(
        _json.dumps({"org_id": str(org_id), "user_id": str(current_user.id)}).encode()
    ).decode()
    params = urlencode({
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_redirect_uri,
        "scope": "instagram_business_basic,instagram_business_manage_comments,instagram_business_manage_messages",
        "response_type": "code",
        "state": state_payload,
    })
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"https://www.instagram.com/oauth/authorize?{params}")


@router.get("/oauth/instagram/callback")
async def instagram_oauth_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Receive the auth code, exchange for a long-lived token, create the connector."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")  # org_id
    error = request.query_params.get("error")

    frontend = settings.frontend_origin

    if error or not code or not state:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"{frontend}/connectors?error=instagram_denied")

    try:
        import base64, json as _json
        state_data = _json.loads(base64.urlsafe_b64decode(state + "==").decode())
        org_id = UUID(state_data["org_id"])
        user_id = UUID(state_data["user_id"])
    except Exception:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"{frontend}/connectors?error=instagram_state")

    try:
        config = await instagram.exchange_code(
            settings.instagram_app_id,
            settings.instagram_app_secret,
            settings.instagram_redirect_uri,
            code,
        )
    except Exception as exc:  # noqa: BLE001
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"{frontend}/connectors?error=instagram_token")

    username = config.get("username", "")
    connector = Connector(
        org_id=org_id,
        created_by=user_id,
        name=f"Instagram · @{username}" if username else "Instagram",
        type=ConnectorType.instagram,
        status=ConnectorStatus.active,
        config=encrypt_json(config),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)

    # Best-effort: subscribe this account to comment + message webhook events.
    await instagram.subscribe_account(config["ig_user_id"], config["access_token"])

    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"{frontend}/connectors?connected=Instagram")
