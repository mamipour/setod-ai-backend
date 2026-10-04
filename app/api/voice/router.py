"""Voice router — Twilio ConversationRelay integration.

Endpoints:
  POST /voice/twiml/{trigger_id}   Twilio calls this on inbound call; returns TwiML.
  WS   /voice/ws/{trigger_id}      ConversationRelay WebSocket session.

Every endpoint resolves: trigger_id → AgentTrigger → Agent → Org → Twilio connector.
Entitlement check (requires "voice" feature) happens at TwiML time so calls that arrive
when the org is over quota get a spoken "unable to take calls" message rather than an error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from twilio.request_validator import RequestValidator

from app.config import settings as cfg
from app.core.agents.base import (
    MASTER_PREAMBLE,
    _resolve_config,
    stream_turn,
)
from app.core.llm.client import (
    system_message,
    user_message,
    assistant_message,
    tool_message,
)
from app.db.models import TriggerType
from app.core.billing.entitlements import EntitlementError, resolve as resolve_ent
from app.core.billing.usage import record_voice_minutes
from app.core import notes, kv
from app.db.models import (
    Agent,
    AgentSession,
    AgentTrigger,
    Connector,
    ConnectorType,
    Conversation,
    ConversationStatus,
    MessageRole,
    SessionStatus,
)
from app.db.session import get_session, AsyncSessionLocal
from app.integrations.registry import build_tools_for_agent
from app.integrations.twilio import validate as twilio_validate
from app.core.crypto import decrypt_json

log = logging.getLogger("voice")

router = APIRouter(prefix="/voice", tags=["voice"])

TWILIO_VOICE_API = "https://api.twilio.com/2010-04-01"
MAX_CALL_MINUTES_DEFAULT = 10   # org can override in trigger config
FILLER_PHRASES = [
    "One moment…",
    "Let me check that for you…",
    "Just a second…",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_trigger(db: AsyncSession, trigger_id: UUID) -> AgentTrigger | None:
    return await db.get(AgentTrigger, trigger_id)


async def _get_twilio_creds(db: AsyncSession, connector_id: UUID) -> dict:
    connector = await db.get(Connector, connector_id)
    if not connector or connector.type != ConnectorType.twilio:
        raise ValueError("No active Twilio connector")
    return decrypt_json(connector.config)


def _validate_twilio_sig(request: Request, form: dict, auth_token: str) -> bool:
    sig = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    return RequestValidator(auth_token).validate(url, form, sig)


def _twiml_busy() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Say>We are currently unable to take voice calls. Please call back later or send us a message.</Say><Hangup/></Response>'
    )


def _twiml_connect(trigger_id: UUID, greeting: str, voice: str = "UgBBYS2sOqTuMpoF3BR0", language: str = "en-US") -> str:
    wss_url = f"{cfg.api_public_origin.replace('https://', 'wss://').replace('http://', 'ws://')}/voice/ws/{trigger_id}"
    # Ensure HTTPS/WSS
    if cfg.api_public_origin.startswith("https://"):
        wss_url = f"wss://{cfg.api_public_origin.split('://',1)[1]}/voice/ws/{trigger_id}"
    else:
        wss_url = f"wss://api.setod.com/voice/ws/{trigger_id}"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay url="{wss_url}"
        welcomeGreeting="{greeting}"
        transcriptionProvider="Deepgram" speechModel="flux" eotThreshold="0.8"
        ttsProvider="ElevenLabs" voice="{voice}"
        interruptible="any" ignoreBackchannel="true"
        language="{language}" />
  </Connect>
</Response>"""


# ── TwiML endpoint ────────────────────────────────────────────────────────────

@router.post("/twiml/{trigger_id}")
async def voice_twiml(
    trigger_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Called by Twilio when an inbound call arrives on the agent's number.

    Validates Twilio signature, checks entitlements, and returns TwiML that
    connects the call to ConversationRelay pointing at our WebSocket.
    """
    form = dict(await request.form())

    async with AsyncSessionLocal() as adb:
        trigger = await _get_trigger(adb, trigger_id)
        if not trigger or not trigger.enabled:
            return Response(content=_twiml_busy(), media_type="application/xml")

        trigger_config: dict[str, Any] = trigger.config or {}
        connector_id = UUID(str(trigger_config["connector_id"]))

        try:
            creds = await _get_twilio_creds(adb, connector_id)
        except Exception:
            return Response(content=_twiml_busy(), media_type="application/xml")

        # Validate Twilio signature
        if not _validate_twilio_sig(request, form, creds["auth_token"]):
            log.warning("voice twiml: bad signature trigger=%s from=%s", trigger_id, form.get("From"))
            return Response(content=_twiml_busy(), media_type="application/xml", status_code=403)

        agent = await adb.get(Agent, trigger.agent_id)
        if not agent:
            return Response(content=_twiml_busy(), media_type="application/xml")

        # Entitlement check: feature gate (plan-level voice flag) then allowance/overage gate
        try:
            ent = await resolve_ent(adb, agent.org_id)
            ent.require("voice")
        except EntitlementError:
            log.info("voice blocked: org=%s not entitled (no voice feature)", agent.org_id)
            return Response(content=_twiml_busy(), media_type="application/xml")

        from app.core.billing.voice import voice_call_allowed
        if not await voice_call_allowed(adb, agent.org_id):
            log.info("voice blocked: org=%s over allowance or overage disabled", agent.org_id)
            return Response(content=_twiml_busy(), media_type="application/xml")

        greeting = trigger_config.get("greeting", "Thank you for calling. How can I help you?")
        voice = trigger_config.get("voice", "UgBBYS2sOqTuMpoF3BR0")
        language = trigger_config.get("language", "en-US")

    log.info("voice twiml: trigger=%s from=%s call=%s", trigger_id, form.get("From"), form.get("CallSid"))
    return Response(
        content=_twiml_connect(trigger_id, greeting, voice, language),
        media_type="application/xml",
    )


# ── WebSocket handler ─────────────────────────────────────────────────────────

@router.websocket("/ws/{trigger_id}")
async def voice_ws(trigger_id: UUID, ws: WebSocket):
    """ConversationRelay WebSocket session.

    Protocol (server→client JSON):
      { "type": "text", "token": "...", "last": false/true }

    Protocol (client→server JSON):
      { "type": "setup", "callSid": "...", "from": "...", "to": "..." }
      { "type": "prompt", "voicePrompt": "...", "lang": "...", "last": true }
      { "type": "interrupt", "utteranceUntilInterrupt": "..." }
      { "type": "error", "description": "..." }
    """
    await ws.accept()
    log.info("voice ws: connected trigger=%s", trigger_id)

    call_sid: str | None = None
    call_start = time.monotonic()
    session: AgentSession | None = None
    conversation: Conversation | None = None
    messages: list[dict[str, Any]] = []
    tools = []
    seq = 0
    cancel: asyncio.Event = asyncio.Event()
    current_task: asyncio.Task | None = None

    async def send_token(token: str) -> None:
        if cancel.is_set():
            return
        await ws.send_text(json.dumps({"type": "text", "token": token, "last": False}))

    async def send_last() -> None:
        await ws.send_text(json.dumps({"type": "text", "token": "", "last": True}))

    try:
        async with AsyncSessionLocal() as db:
            trigger = await _get_trigger(db, trigger_id)
            if not trigger or not trigger.enabled:
                await ws.close(1008)
                return

            trigger_config: dict[str, Any] = trigger.config or {}
            agent = await db.get(Agent, trigger.agent_id)
            if not agent:
                await ws.close(1008)
                return

            connector_id = UUID(str(trigger_config["connector_id"]))
            max_minutes = int(trigger_config.get("max_call_minutes", MAX_CALL_MINUTES_DEFAULT))

            config = _resolve_config(agent, use_published=True)
            tools, contexts, approval_required = await build_tools_for_agent(
                db, agent.id, session_id=None, conversation_id=None
            )
            # Add notes + kv tools
            notes_tool = await notes.build_tool(db, agent.org_id, agent.id)
            if notes_tool:
                tools = tools + [notes_tool]
            kv_tools = await kv.build_tools(db, agent, uuid4())  # temp session id
            tools = tools + kv_tools

        # Main WebSocket loop
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=max_minutes * 60)
            except asyncio.TimeoutError:
                log.info("voice ws: max call length reached trigger=%s", trigger_id)
                break

            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "setup":
                call_sid = msg.get("callSid")
                caller = msg.get("from", "unknown")
                log.info("voice ws: setup callSid=%s from=%s trigger=%s", call_sid, caller, trigger_id)

                async with AsyncSessionLocal() as db:
                    agent_obj = await db.get(Agent, trigger.agent_id)
                    # Create conversation + session
                    conversation = Conversation(
                        org_id=agent_obj.org_id,
                        connector_id=connector_id,
                        channel="phone",
                        peer_id=caller,
                        peer_name=caller,
                        thread_key=call_sid or str(uuid4()),
                        status=ConversationStatus.open,
                        last_inbound_at=datetime.now(UTC),
                    )
                    db.add(conversation)
                    await db.commit()
                    await db.refresh(conversation)

                    session = AgentSession(
                        agent_id=agent_obj.id,
                        org_id=agent_obj.org_id,
                        trigger_type=TriggerType.phone,
                        status=SessionStatus.running,
                        conversation_id=conversation.id,
                        model_slug=config.get("model", "") or "",
                    )
                    db.add(session)
                    await db.commit()
                    await db.refresh(session)

                    # Rebuild tools with real session id for kv
                    tools_list, _, _ = await build_tools_for_agent(
                        db, agent_obj.id,
                        session_id=session.id,
                        conversation_id=conversation.id,
                    )
                    notes_tool = await notes.build_tool(db, agent_obj.org_id, agent_obj.id)
                    if notes_tool:
                        tools_list = tools_list + [notes_tool]
                    kv_tools_real = await kv.build_tools(db, agent_obj, session.id)
                    tools = tools_list + kv_tools_real

                    # System prompt
                    messages = [system_message(MASTER_PREAMBLE)]
                    if config.get("instructions"):
                        messages.append(system_message(f"INSTRUCTIONS:\n{config['instructions']}"))
                    messages.append(system_message(
                        "You are on a phone call. Keep every reply to 1-2 short spoken sentences. "
                        "Do not use markdown, bullet points, or any formatting. "
                        "Read numbers and codes digit by digit with pauses. "
                        "If you need to do something that requires approval, say 'I'll have the team follow up on that.'"
                    ))

            elif msg_type == "prompt":
                if not msg.get("last", True):
                    continue  # Wait for the final chunk
                voice_text = msg.get("voicePrompt", "").strip()
                if not voice_text or session is None:
                    continue

                log.info("voice ws: prompt callSid=%s text=%r", call_sid, voice_text[:80])

                # Cancel any in-progress generation
                if current_task and not current_task.done():
                    cancel.set()
                    try:
                        await asyncio.wait_for(current_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                cancel = asyncio.Event()

                messages.append(user_message(voice_text))

                async with AsyncSessionLocal() as db:
                    agent_obj = await db.get(Agent, trigger.agent_id)
                    sess_obj = await db.get(AgentSession, session.id)
                    t0 = time.perf_counter()

                    result = await stream_turn(
                        db,
                        agent_obj,
                        messages=messages,
                        tools=tools,
                        session=sess_obj,
                        on_token=send_token,
                        cancel=cancel,
                        use_published=True,
                    )
                    latency_ms = round((time.perf_counter() - t0) * 1000)
                    log.info("voice ws: turn done callSid=%s first_token=%dms interrupted=%s",
                             call_sid, latency_ms, result.interrupted)

                    sess_obj.prompt_tokens += result.prompt_tokens
                    sess_obj.completion_tokens += result.completion_tokens
                    sess_obj.iterations += 1
                    db.add(sess_obj)
                    await db.commit()

                await send_last()
                if result.content:
                    messages.append(assistant_message(result.content, []))

            elif msg_type == "interrupt":
                heard = msg.get("utteranceUntilInterrupt", "")
                log.info("voice ws: interrupt callSid=%s heard=%r", call_sid, heard[:60])
                cancel.set()
                # Truncate last assistant message to what was actually heard
                if heard and messages and messages[-1].get("role") == "assistant":
                    messages[-1] = assistant_message(heard, [])

            elif msg_type == "error":
                log.warning("voice ws: error callSid=%s desc=%s", call_sid, msg.get("description"))

    except WebSocketDisconnect:
        log.info("voice ws: disconnected trigger=%s callSid=%s", trigger_id, call_sid)
    except Exception:
        log.exception("voice ws: unhandled error trigger=%s callSid=%s", trigger_id, call_sid)
    finally:
        # Finalize session and record voice minutes
        duration = time.monotonic() - call_start
        if session and call_sid:
            try:
                async with AsyncSessionLocal() as db:
                    sess_obj = await db.get(AgentSession, session.id)
                    if sess_obj:
                        sess_obj.status = SessionStatus.succeeded
                        sess_obj.finished_at = datetime.now(UTC)
                        db.add(sess_obj)
                        await db.commit()
                    agent_obj = await db.get(Agent, trigger.agent_id)
                    if agent_obj:
                        await record_voice_minutes(
                            db,
                            org_id=agent_obj.org_id,
                            agent_id=agent_obj.id,
                            session_id=session.id,
                            call_sid=call_sid,
                            duration_seconds=duration,
                        )
            except Exception:
                log.exception("voice ws: finalisation error callSid=%s", call_sid)


# ── Phone trigger attach / detach ─────────────────────────────────────────────

@router.post("/attach/{trigger_id}")
async def attach_voice_number(
    trigger_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Set the Twilio number's VoiceUrl to point at this trigger's TwiML endpoint.

    Called automatically when a phone trigger is created.
    """
    trigger = await db.get(AgentTrigger, trigger_id)
    if not trigger:
        from fastapi import HTTPException
        raise HTTPException(404, "Trigger not found")

    trigger_config: dict[str, Any] = trigger.config or {}
    connector_id = UUID(str(trigger_config["connector_id"]))
    number_sid = trigger_config.get("number_sid")
    if not number_sid:
        from fastapi import HTTPException
        raise HTTPException(400, "number_sid not set in trigger config")

    creds = await _get_twilio_creds(db, connector_id)
    sid, tok = creds["account_sid"], creds["auth_token"]
    twiml_url = f"{cfg.api_public_origin}/voice/twiml/{trigger_id}"

    async with httpx.AsyncClient(auth=(sid, tok), timeout=20) as cl:
        resp = await cl.post(
            f"{TWILIO_VOICE_API}/Accounts/{sid}/IncomingPhoneNumbers/{number_sid}.json",
            data={"VoiceUrl": twiml_url, "VoiceMethod": "POST"},
        )
        if resp.status_code >= 300:
            from fastapi import HTTPException
            raise HTTPException(502, f"Twilio returned {resp.status_code}: {resp.text[:200]}")

    return {"voice_url": twiml_url, "status": resp.json().get("status")}


@router.post("/detach/{trigger_id}")
async def detach_voice_number(
    trigger_id: UUID,
    db: AsyncSession = Depends(get_session),
):
    """Clear the Twilio number's VoiceUrl when the trigger is deleted."""
    trigger = await db.get(AgentTrigger, trigger_id)
    if not trigger:
        from fastapi import HTTPException
        raise HTTPException(404, "Trigger not found")

    trigger_config: dict[str, Any] = trigger.config or {}
    connector_id = UUID(str(trigger_config["connector_id"]))
    number_sid = trigger_config.get("number_sid")
    if not number_sid:
        return {"status": "no number_sid to clear"}

    creds = await _get_twilio_creds(db, connector_id)
    sid, tok = creds["account_sid"], creds["auth_token"]

    async with httpx.AsyncClient(auth=(sid, tok), timeout=20) as cl:
        resp = await cl.post(
            f"{TWILIO_VOICE_API}/Accounts/{sid}/IncomingPhoneNumbers/{number_sid}.json",
            data={"VoiceUrl": "", "VoiceMethod": "POST"},
        )

    return {"status": "cleared", "twilio_status": resp.status_code}
