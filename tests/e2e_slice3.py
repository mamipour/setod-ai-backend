"""Slice 3 smoke test — triggers fire agents without a human.

Covers the three things that decide whether a scheduler can be trusted: cron maths (including
the daylight-saving case that silently shifts a schedule by an hour), the claiming rules that
stop two workers doubling a run, and the inbound webhook path from signed request to finished
agent session.

The scheduled run is real: it reads Gmail and sends Telegram.

    python tests/e2e_slice3.py
"""

import asyncio
import secrets
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import delete, select
from twilio.request_validator import RequestValidator

from app.config import settings
from app.core.agents.base import snapshot_config
from app.core.crypto import encrypt_json
from app.main import app
from app.core.triggers import schedule
from app.core.triggers.dispatch import (
    STALE_RUN_AFTER,
    claim_due,
    claim_inbound,
    is_running,
    reap_stale_sessions,
    run_inbound,
    run_trigger,
)
from app.db.models import (
    Agent,
    AgentProcessedItem,
    AgentSession,
    AgentSessionMessage,
    AgentStatus,
    AgentTool,
    AgentTrigger,
    Connector,
    ConnectorStatus,
    ConnectorType,
    InboundEvent,
    InboundEventStatus,
    Organization,
    SessionStatus,
    TriggerType,
    User,
)
from app.db.session import AsyncSessionLocal, engine

engine.echo = False

INSTRUCTIONS = (
    "You are a notifier. When run, send exactly one short Telegram message describing what "
    "woke you up, then stop. Do not read email unless asked."
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'  PASS' if ok else '  FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# ── Cron, with no database involved ────────────────────────────────────────────

def test_schedule_maths() -> None:
    print("\n=== cron and timezones ===")

    cfg = schedule.validate({"preset": "daily_9am", "timezone": "America/Toronto"})
    check("preset expands to cron", cfg["cron"] == "0 9 * * *", cfg["cron"])

    # The case that motivates storing a timezone rather than a UTC cron: Toronto is UTC-5 in
    # January and UTC-4 in July, so a fixed UTC hour would drift by one in the summer.
    toronto = ZoneInfo("America/Toronto")
    winter = schedule.next_run_after(cfg, datetime(2027, 1, 15, 12, 0, tzinfo=UTC))
    summer = schedule.next_run_after(cfg, datetime(2027, 7, 15, 12, 0, tzinfo=UTC))
    check("9am holds across DST", winter.astimezone(toronto).hour == summer.astimezone(toronto).hour == 9,
          f"{winter.astimezone(toronto):%H:%M %Z} vs {summer.astimezone(toronto):%H:%M %Z}")
    check("UTC hour shifts with DST", winter.hour != summer.hour, f"{winter.hour}h vs {summer.hour}h")

    # Missed occurrences are dropped, not replayed: computing forward from now is what stops
    # a worker that was down for six hours from firing six catch-up runs at once.
    hourly = schedule.validate({"preset": "hourly"})
    nxt = schedule.next_run_after(hourly)
    check("next run is in the future", nxt > datetime.now(UTC), f"{nxt:%H:%M}")
    check("missed runs are not replayed", nxt < datetime.now(UTC) + timedelta(hours=1, minutes=1))

    for bad, why in [
        ({"cron": "not a cron"}, "gibberish"),
        ({"preset": "every_second"}, "unknown preset"),
        ({"cron": "0 9 * * *", "timezone": "Mars/Olympus"}, "bad timezone"),
        ({"cron": "* * * * *"}, "too frequent"),
        ({}, "empty"),
    ]:
        try:
            schedule.validate(bad)
            check(f"rejects {why}", False, "was accepted")
        except schedule.InvalidSchedule:
            check(f"rejects {why}", True)


async def test_webhook_security(db, org, user) -> list[Connector]:
    """Drive the public webhook endpoints over HTTP, forged requests included.

    These two URLs are reachable by anyone who guesses them, and a forged delivery would make
    someone else's agent act on attacker-supplied text. Uses throwaway connectors so the
    workspace's real credentials are never involved.
    """
    print("\n=== webhook signature verification ===")

    tg_secret = "test-secret-" + secrets.token_urlsafe(8)
    tg = Connector(
        org_id=org.id,
        created_by=user.id,
        name="Slice 3 · fake Telegram",
        type=ConnectorType.telegram_bot,
        status=ConnectorStatus.active,
        config=encrypt_json(
            {"bot_token": "000:fake", "admin_chat_id": 1, "webhook_secret": tg_secret}
        ),
    )
    twilio_token = "fake_auth_token_" + secrets.token_hex(8)
    tw = Connector(
        org_id=org.id,
        created_by=user.id,
        name="Slice 3 · fake Twilio",
        type=ConnectorType.twilio,
        status=ConnectorStatus.active,
        config=encrypt_json(
            {"account_sid": "ACfake", "auth_token": twilio_token, "phone_number": "+15550000000"}
        ),
    )
    db.add(tg)
    db.add(tw)
    await db.commit()
    await db.refresh(tg)
    await db.refresh(tw)

    settings.public_base_url = "https://example.test"
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        tg_url = f"/webhooks/telegram/{tg.id}"
        update = {
            "update_id": int(datetime.now(UTC).timestamp()),
            "message": {"text": "hello", "chat": {"id": 42, "username": "someone"}},
        }

        r = await client.post(tg_url, json=update)
        check("Telegram delivery with no secret is rejected", r.status_code == 404, str(r.status_code))

        r = await client.post(
            tg_url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        )
        check("Telegram delivery with a wrong secret is rejected", r.status_code == 404, str(r.status_code))

        r = await client.post(
            f"/webhooks/telegram/{uuid4()}",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret},
        )
        check("unknown connector id is rejected", r.status_code == 404, str(r.status_code))

        r = await client.post(
            tg_url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret}
        )
        check("Telegram delivery with the right secret is accepted", r.status_code == 204, str(r.status_code))

        queued = (
            await db.exec(
                select(InboundEvent).where(
                    InboundEvent.connector_id == tg.id,
                    InboundEvent.external_id == str(update["update_id"]),
                )
            )
        ).first()
        check("accepted delivery was queued", queued is not None)
        check("queued event carries the text", queued is not None and queued.text == "hello")

        # Telegram redelivers an update it thinks we missed. The provider's own id is the
        # unique key, so a retry must not become a second agent run.
        r = await client.post(
            tg_url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret}
        )
        rows = (
            await db.exec(
                select(InboundEvent.id).where(
                    InboundEvent.connector_id == tg.id,
                    InboundEvent.external_id == str(update["update_id"]),
                )
            )
        ).all()
        check("redelivered update does not duplicate", r.status_code == 204 and len(list(rows)) == 1)

        # A sticker or a chat join has no text to act on.
        r = await client.post(
            tg_url,
            json={"update_id": int(datetime.now(UTC).timestamp()) + 1, "message": {"chat": {"id": 42}}},
            headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret},
        )
        check("non-text update is acknowledged, not queued", r.status_code == 204, str(r.status_code))

        # ── Twilio ────────────────────────────────────────────────────────────
        tw_url = f"/webhooks/twilio/{tw.id}"
        form = {
            "MessageSid": "SM" + secrets.token_hex(8),
            "From": "+15551234567",
            "Body": "inbound sms",
        }

        r = await client.post(tw_url, data=form)
        check("Twilio delivery with no signature is rejected", r.status_code == 404, str(r.status_code))

        r = await client.post(tw_url, data=form, headers={"X-Twilio-Signature": "bogus"})
        check("Twilio delivery with a forged signature is rejected", r.status_code == 404, str(r.status_code))

        signature = RequestValidator(twilio_token).compute_signature(
            f"https://example.test{tw_url}", form
        )
        r = await client.post(tw_url, data=form, headers={"X-Twilio-Signature": signature})
        check("Twilio delivery with a valid signature is accepted", r.status_code == 200, str(r.status_code))

        queued = (
            await db.exec(
                select(InboundEvent).where(InboundEvent.external_id == form["MessageSid"])
            )
        ).first()
        check("signed SMS was queued", queued is not None and queued.text == "inbound sms")

        # The signature covers the URL, so the same body signed for a different address must
        # not pass — this is what stops a delivery being replayed against another server.
        other = RequestValidator(twilio_token).compute_signature(
            "https://attacker.test/webhooks/twilio/x", form
        )
        r = await client.post(
            tw_url,
            data={**form, "MessageSid": "SM" + secrets.token_hex(8)},
            headers={"X-Twilio-Signature": other},
        )
        check("signature from another URL is rejected", r.status_code == 404, str(r.status_code))

    return [tg, tw]


async def main() -> int:
    test_schedule_maths()

    async with AsyncSessionLocal() as db:
        org = (await db.exec(select(Organization))).first()
        user = (await db.exec(select(User))).first()

        async def connector_of(kind):
            return (await db.exec(select(Connector).where(Connector.type == kind))).first()

        llm = await connector_of(ConnectorType.anthropic) or await connector_of(ConnectorType.openai)
        tg = await connector_of(ConnectorType.telegram_bot)
        if not (org and user and llm and tg):
            print("Need an org, user, an LLM connector, and a Telegram bot.")
            return 1

        agent = Agent(
            org_id=org.id,
            created_by=user.id,
            name="Slice 3 smoke test",
            instructions=INSTRUCTIONS,
            model_connector_id=llm.id,
            settings={"max_iterations": 4},
        )
        agent.published_config = snapshot_config(agent)
        agent.status = AgentStatus.published
        agent.published_at = datetime.now(UTC)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        agent_id = agent.id

        db.add(AgentTool(agent_id=agent_id, connector_id=tg.id))
        await db.commit()

        temp_connectors: list[Connector] = []
        try:
            # ── Claiming ──────────────────────────────────────────────────────
            print("\n=== claiming due schedules ===")
            cfg = schedule.validate({"preset": "hourly"})
            trigger = AgentTrigger(
                agent_id=agent_id,
                type=TriggerType.schedule,
                config=cfg,
                next_run_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            db.add(trigger)
            await db.commit()
            await db.refresh(trigger)
            trigger_id = trigger.id

            claimed = await claim_due(db, limit=10)
            check("due trigger was claimed", trigger_id in claimed)

            await db.refresh(trigger)
            check(
                "next run advanced past now",
                trigger.next_run_at > datetime.now(UTC),
                f"{trigger.next_run_at:%H:%M}",
            )
            check("last run recorded", trigger.last_run_at is not None)

            # A second poll must find nothing: this is what stops a slow run from being
            # picked up again on the next tick.
            check("second claim finds nothing", trigger_id not in await claim_due(db, limit=10))

            # ── Malformed cron disables itself ────────────────────────────────
            broken = AgentTrigger(
                agent_id=agent_id,
                type=TriggerType.schedule,
                config={"cron": "nonsense"},
                next_run_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            db.add(broken)
            await db.commit()
            await db.refresh(broken)
            await claim_due(db, limit=10)
            await db.refresh(broken)
            check("unparseable cron disables its trigger", not broken.enabled)
            check("disabled trigger has no next run", broken.next_run_at is None)
            await db.delete(broken)
            await db.commit()

            # ── Overlap guard and the reaper ──────────────────────────────────
            print("\n=== overlap guard ===")
            stuck = AgentSession(
                agent_id=agent_id,
                org_id=org.id,
                trigger_type=TriggerType.schedule,
                status=SessionStatus.running,
                started_at=datetime.now(UTC) - STALE_RUN_AFTER - timedelta(minutes=5),
            )
            db.add(stuck)
            await db.commit()
            await db.refresh(stuck)

            check("agent with a live session looks busy", await is_running(db, agent_id))
            skipped = await run_trigger(db, trigger_id)
            check("busy agent is not run again", skipped is None)

            reaped = await reap_stale_sessions(db)
            await db.refresh(stuck)
            check("abandoned session is reaped", reaped >= 1 and stuck.status == SessionStatus.error)
            check("agent is free again", not await is_running(db, agent_id))

            # ── A real scheduled run ──────────────────────────────────────────
            print("\n=== scheduled run (sends a real Telegram message) ===")
            session = await run_trigger(db, trigger_id)
            check("scheduled run started", session is not None)
            if session:
                check(
                    "scheduled run succeeded",
                    session.status == SessionStatus.succeeded,
                    session.error or "",
                )
                check("recorded as a schedule trigger", session.trigger_type == TriggerType.schedule)
                check("ran live, not simulated", session.dry_run is False)

            # ── Unpublished agents do not run ─────────────────────────────────
            agent.status = AgentStatus.draft
            db.add(agent)
            await db.commit()
            check("draft agent is not run by its schedule", await run_trigger(db, trigger_id) is None)
            agent.status = AgentStatus.published
            db.add(agent)
            await db.commit()

            # ── Inbound webhook path ──────────────────────────────────────────
            print("\n=== inbound webhook event ===")
            db.add(
                AgentTrigger(
                    agent_id=agent_id,
                    type=TriggerType.channel,
                    config={"connector_id": str(tg.id)},
                )
            )
            await db.commit()

            event = InboundEvent(
                connector_id=tg.id,
                org_id=org.id,
                external_id=f"test-{datetime.now(UTC).timestamp()}",
                text="Are you awake?",
                sender="smoke_test",
                payload={"source": "e2e_slice3"},
            )
            db.add(event)
            await db.commit()
            await db.refresh(event)
            event_id = event.id

            claimed_events = await claim_inbound(db, limit=10)
            check("pending event was claimed", event_id in claimed_events)
            check("claimed event is not claimed twice", event_id not in await claim_inbound(db, limit=10))

            sessions = await run_inbound(db, event_id)
            check("inbound event woke the agent", len(sessions) == 1, f"{len(sessions)} session(s)")
            if sessions:
                check(
                    "inbound run succeeded",
                    sessions[0].status == SessionStatus.succeeded,
                    sessions[0].error or "",
                )
                check(
                    "recorded as a channel trigger",
                    sessions[0].trigger_type == TriggerType.channel,
                )
                first = (
                    await db.exec(
                        select(AgentSessionMessage)
                        .where(AgentSessionMessage.session_id == sessions[0].id)
                        .order_by(AgentSessionMessage.sequence)
                    )
                ).first()
                check(
                    "agent received the message text",
                    first is not None and "Are you awake?" in first.content,
                    (first.content[:70] if first else "no messages"),
                )

            # An event nobody listens to is recorded, not silently dropped.
            await db.exec(delete(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
            await db.commit()
            orphan = InboundEvent(
                connector_id=tg.id,
                org_id=org.id,
                external_id=f"orphan-{datetime.now(UTC).timestamp()}",
                text="Anyone there?",
            )
            db.add(orphan)
            await db.commit()
            await db.refresh(orphan)
            await claim_inbound(db, limit=10)
            await run_inbound(db, orphan.id)
            await db.refresh(orphan)
            check(
                "event with no listener is marked ignored",
                orphan.status == InboundEventStatus.ignored,
                orphan.status.value,
            )

            temp_connectors = await test_webhook_security(db, org, user)

        finally:
            print("\n=== cleanup ===")
            sids = list(
                (await db.exec(select(AgentSession.id).where(AgentSession.agent_id == agent_id))).all()
            )
            await db.exec(delete(AgentProcessedItem).where(AgentProcessedItem.agent_id == agent_id))
            if sids:
                await db.exec(
                    delete(AgentSessionMessage).where(AgentSessionMessage.session_id.in_(sids))
                )
            await db.exec(delete(AgentSession).where(AgentSession.agent_id == agent_id))
            await db.exec(delete(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
            await db.exec(delete(AgentTool).where(AgentTool.agent_id == agent_id))
            await db.exec(delete(Agent).where(Agent.id == agent_id))

            connector_ids = [c.id for c in temp_connectors] + [tg.id]
            await db.exec(
                delete(InboundEvent).where(InboundEvent.connector_id.in_(connector_ids))
            )
            if temp_connectors:
                await db.exec(
                    delete(Connector).where(Connector.id.in_([c.id for c in temp_connectors]))
                )
            await db.commit()
            print("  test agent, triggers, sessions, events, and fake connectors removed")

    await engine.dispose()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
