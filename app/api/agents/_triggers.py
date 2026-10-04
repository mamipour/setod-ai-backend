"""Triggers and scenarios sub-router for the agents API.

Extracted from router.py (R2 refactor). Mounted at no prefix; the parent
router (/agents) provides the path prefix so routes remain identical.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.agents._helpers import get_owned_agent, session_out
from app.api.agents.schemas import SessionOut, TriggerOut, TriggerUpsert
from app.api.auth.dependencies import get_current_user
from app.core.agents.base import AgentRunError, run_agent
from app.core.triggers import schedule
from app.db.models import (
    Agent,
    AgentScenario,
    AgentSession,
    AgentTrigger,
    Connector,
    TriggerType,
    User,
)
from app.db.session import get_session

CHANNEL_TRIGGERS_ENABLED = True
triggers_router = APIRouter()

# ── Triggers ──────────────────────────────────────────────────────────────────

async def _trigger_out(session: AsyncSession, trigger: AgentTrigger) -> TriggerOut:
    out = TriggerOut.model_validate(trigger)
    if trigger.type == TriggerType.schedule:
        out.summary = schedule.describe(trigger.config)
    else:
        # Name the account being listened to — "When a message arrives" alone does not tell
        # the user which of their connectors this trigger is bound to.
        connector = await session.get(Connector, trigger.config.get("connector_id"))
        out.summary = (
            f"Listens on {connector.name}" if connector else "Listens on a deleted connector"
        )
    return out


async def _validated_trigger_config(
    session: AsyncSession, agent: Agent, body: TriggerUpsert
) -> dict:
    """Reject a trigger that could never fire, at save time rather than silently at run time."""
    if body.type == TriggerType.manual:
        raise HTTPException(
            status_code=422,
            detail="Manual runs need no trigger — every agent can already be run by hand.",
        )

    if body.type == TriggerType.schedule:
        try:
            return schedule.validate(body.config)
        except schedule.InvalidSchedule as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not CHANNEL_TRIGGERS_ENABLED:
        raise HTTPException(
            status_code=422,
            detail="Channel triggers are not available yet. Use a schedule instead.",
        )

    connector_id = body.config.get("connector_id")
    if not connector_id:
        raise HTTPException(
            status_code=422, detail="A channel trigger needs the account it listens to."
        )
    connector = await session.get(Connector, UUID(str(connector_id)))
    if connector is None or connector.org_id != agent.org_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.type not in INBOUND_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"{connector.name} cannot receive incoming messages",
        )
    return {"connector_id": str(connector.id)}


@triggers_router.get("/{agent_id}/triggers", response_model=list[TriggerOut])
async def list_agent_triggers(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    agent = await get_owned_agent(session, current_user, agent_id)
    rows = await session.exec(
        select(AgentTrigger)
        .where(AgentTrigger.agent_id == agent.id)
        .order_by(AgentTrigger.created_at)
    )
    return [await _trigger_out(session, t) for t in rows.all()]


async def _auto_configure_twilio_voice_webhook(connector: Connector, trigger_id: UUID) -> None:
    """Point the Twilio phone number's Voice webhook URL at our TwiML endpoint for this trigger.

    Idempotent — Twilio simply overwrites the existing value. Failures are logged and swallowed
    so a network hiccup never blocks the trigger from being saved.
    """
    from app.config import settings as _cfg
    try:
        config = decrypt_json(connector.config)
        account_sid = config["account_sid"]
        auth_token = config["auth_token"]
        phone_number = config["phone_number"]

        base = (_cfg.api_public_origin or "https://api.setod.com").rstrip("/")
        voice_url = f"{base}/voice/twiml/{trigger_id}"

        async with httpx.AsyncClient(timeout=15) as client:
            # 1. Find the Twilio phone number SID
            r = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": phone_number},
                auth=(account_sid, auth_token),
            )
            if r.status_code != 200:
                log.warning("Twilio voice webhook: failed to list numbers (status=%s)", r.status_code)
                return
            numbers = r.json().get("incoming_phone_numbers", [])
            if not numbers:
                log.warning("Twilio voice webhook: phone %s not found in account", phone_number)
                return
            number_sid = numbers[0]["sid"]

            # 2. Update the voice URL
            r2 = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json",
                data={"VoiceUrl": voice_url, "VoiceMethod": "POST"},
                auth=(account_sid, auth_token),
            )
            if r2.status_code == 200:
                log.info("Twilio voice webhook configured: %s → %s", phone_number, voice_url)
            else:
                log.warning("Twilio voice webhook update failed (status=%s): %s", r2.status_code, r2.text[:200])
    except Exception:
        log.exception("Unexpected error configuring Twilio voice webhook for trigger %s", trigger_id)


async def _auto_register_telegram_webhook(session: AsyncSession, connector: Connector) -> None:
    """Register (or re-register) a Telegram bot webhook if PUBLIC_BASE_URL is configured.

    Safe to call multiple times — Telegram simply updates the registered URL. Failures are
    logged and swallowed so a network hiccup never blocks the trigger from being saved.
    """
    if not settings.public_base_url:
        log.info("PUBLIC_BASE_URL not set — skipping Telegram webhook registration for %s", connector.id)
        return
    try:
        config = decrypt_json(connector.config)
        webhook_secret = config.get("webhook_secret") or secrets.token_urlsafe(32)
        url = f"{settings.public_base_url.rstrip('/')}/webhooks/telegram/{connector.id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{config['bot_token']}/setWebhook",
                json={
                    "url": url,
                    "secret_token": webhook_secret,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": True,
                },
            )
        data = resp.json()
        if data.get("ok"):
            log.info("Telegram webhook registered for connector %s → %s", connector.id, url)
            config["webhook_secret"] = webhook_secret
            connector.config = encrypt_json(config)
            session.add(connector)
        else:
            log.warning("Telegram webhook registration failed for %s: %s", connector.id, data)
    except Exception:
        log.exception("Unexpected error registering Telegram webhook for connector %s", connector.id)


@triggers_router.post("/{agent_id}/triggers", response_model=TriggerOut, status_code=201)
async def create_agent_trigger(
    agent_id: UUID,
    body: TriggerUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Add a schedule or a channel listener to an agent."""
    agent = await get_owned_agent(session, current_user, agent_id)
    config = await _validated_trigger_config(session, agent, body)

    trigger = AgentTrigger(
        agent_id=agent.id, type=body.type, config=config, enabled=body.enabled
    )
    if body.type == TriggerType.schedule and body.enabled:
        trigger.next_run_at = schedule.next_run_after(config)

    # For channel triggers on a Telegram bot connector, ensure the webhook is registered so
    # Telegram knows where to deliver messages. This is idempotent — safe to call every time.
    if body.type == TriggerType.channel:
        connector = await session.get(Connector, UUID(str(config["connector_id"])))
        if connector and connector.type == ConnectorType.telegram_bot:
            await _auto_register_telegram_webhook(session, connector)

    session.add(trigger)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(trigger)

    # For phone triggers, point the Twilio number's Voice webhook at our TwiML endpoint.
    # Done after commit so trigger.id is assigned.
    if body.type == TriggerType.phone:
        connector = await session.get(Connector, UUID(str(config["connector_id"])))
        if connector and connector.type == ConnectorType.twilio:
            await _auto_configure_twilio_voice_webhook(connector, trigger.id)

    return await _trigger_out(session, trigger)


@triggers_router.patch("/{agent_id}/triggers/{trigger_id}", response_model=TriggerOut)
async def update_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    body: TriggerUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Change a trigger. Pausing clears the next run; resuming schedules it forward from now,
    so a schedule paused over a weekend does not fire twice on Monday."""
    agent = await get_owned_agent(session, current_user, agent_id)
    trigger = await session.get(AgentTrigger, trigger_id)
    if trigger is None or trigger.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Trigger not found")

    config = await _validated_trigger_config(session, agent, body)
    trigger.type = body.type
    trigger.config = config
    trigger.enabled = body.enabled
    trigger.next_run_at = (
        schedule.next_run_after(config)
        if body.type == TriggerType.schedule and body.enabled
        else None
    )

    session.add(trigger)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(trigger)

    # Keep Twilio voice webhook in sync if the connector or trigger changed.
    if body.type == TriggerType.phone:
        connector = await session.get(Connector, UUID(str(config["connector_id"])))
        if connector and connector.type == ConnectorType.twilio:
            await _auto_configure_twilio_voice_webhook(connector, trigger.id)

    return await _trigger_out(session, trigger)


@triggers_router.delete("/{agent_id}/triggers/{trigger_id}", status_code=204)
async def delete_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    agent = await get_owned_agent(session, current_user, agent_id)
    trigger = await session.get(AgentTrigger, trigger_id)
    if trigger is None or trigger.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Trigger not found")

    await session.delete(trigger)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()



# ── Scenarios ─────────────────────────────────────────────────────────────────

class ScenarioBody(BaseModel):
    name: str
    input_text: str
    expected_tools: list[str] = []


class ScenarioOut(BaseModel):
    id: UUID
    agent_id: UUID
    name: str
    input_text: str
    expected_tools: list[str]
    last_session_id: UUID | None
    last_ran_at: datetime | None
    created_at: datetime


class ScenarioRunOut(BaseModel):
    scenario_id: UUID
    session: SessionOut


@triggers_router.get("/{agent_id}/scenarios", response_model=list[ScenarioOut])
async def list_scenarios(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await get_owned_agent(session, current_user, agent_id)
    rows = await session.exec(
        select(AgentScenario)
        .where(AgentScenario.agent_id == agent.id)
        .order_by(AgentScenario.created_at)
    )
    return [ScenarioOut(**r.model_dump()) for r in rows.all()]


@triggers_router.post("/{agent_id}/scenarios", response_model=ScenarioOut, status_code=201)
async def create_scenario(
    agent_id: UUID,
    body: ScenarioBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await get_owned_agent(session, current_user, agent_id)
    sc = AgentScenario(
        agent_id=agent.id,
        name=body.name.strip(),
        input_text=body.input_text.strip(),
        expected_tools=body.expected_tools,
    )
    session.add(sc)
    await session.commit()
    await session.refresh(sc)
    return ScenarioOut(**sc.model_dump())


@triggers_router.patch("/{agent_id}/scenarios/{scenario_id}", response_model=ScenarioOut)
async def update_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    body: ScenarioBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await get_owned_agent(session, current_user, agent_id)
    sc = await session.get(AgentScenario, scenario_id)
    if sc is None or sc.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Scenario not found")
    sc.name = body.name.strip()
    sc.input_text = body.input_text.strip()
    sc.expected_tools = body.expected_tools
    session.add(sc)
    await session.commit()
    await session.refresh(sc)
    return ScenarioOut(**sc.model_dump())


@triggers_router.delete("/{agent_id}/scenarios/{scenario_id}", status_code=204)
async def delete_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await get_owned_agent(session, current_user, agent_id)
    sc = await session.get(AgentScenario, scenario_id)
    if sc is None or sc.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await session.delete(sc)
    await session.commit()


@triggers_router.post("/{agent_id}/scenarios/{scenario_id}/run", response_model=ScenarioRunOut)
async def run_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Dry-run the agent with the scenario's input_text and return the session."""
    agent = await get_owned_agent(session, current_user, agent_id)
    sc = await session.get(AgentScenario, scenario_id)
    if sc is None or sc.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Scenario not found")

    try:
        result = await run_agent(
            session,
            agent,
            trigger_type=TriggerType.manual,
            user_input=sc.input_text,
            dry_run=True,
            use_published=False,  # always test the draft
        )
    except AgentRunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sc.last_session_id = result.id
    sc.last_ran_at = datetime.now(UTC)
    session.add(sc)
    await session.commit()

    return ScenarioRunOut(scenario_id=sc.id, session=session_out(result))
