"""
Turning triggers into runs
==========================
Claiming due work, guarding against overlap, and firing agents from inbound events.

**Claiming.** Due triggers are selected `FOR UPDATE SKIP LOCKED` and their `next_run_at` is
advanced inside the same short transaction, before the agent runs. Two consequences follow,
both deliberate: a second worker polling concurrently skips locked rows instead of blocking,
and a worker that dies mid-run drops that occurrence rather than repeating it. For an agent
that emails customers, missing one run is a nuisance and running twice is an incident.

**Overlap.** An agent whose run outlasts its own interval would otherwise stack up. A trigger
whose agent is already running is skipped, and its slot moves to the next occurrence.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.agents.base import run_agent
from app.core.triggers import schedule
from app.db.models import (
    Agent,
    AgentSession,
    AgentStatus,
    AgentTrigger,
    InboundEvent,
    InboundEventStatus,
    SessionStatus,
    TriggerType,
)

log = logging.getLogger(__name__)

# A run that has not finished in this long is treated as dead. Nothing legitimately takes an
# hour: the iteration ceiling and per-call timeouts cap a healthy run far below it, so a
# session still open past this point means the worker died holding it.
STALE_RUN_AFTER = timedelta(hours=1)


async def claim_due(db: AsyncSession, *, limit: int = 10, now: datetime | None = None) -> list[UUID]:
    """Take ownership of up to `limit` triggers that are due, returning their ids.

    Advancing `next_run_at` here rather than after the run is what stops a slow or crashed run
    from being claimed again by the next poll.
    """
    now = now or datetime.now(UTC)

    rows = await db.exec(
        select(AgentTrigger)
        .where(
            AgentTrigger.type == TriggerType.schedule,
            AgentTrigger.enabled.is_(True),
            AgentTrigger.next_run_at.is_not(None),
            AgentTrigger.next_run_at <= now,
        )
        .order_by(AgentTrigger.next_run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

    claimed = []
    for trigger in rows.all():
        try:
            trigger.next_run_at = schedule.next_run_after(trigger.config, now)
        except schedule.InvalidSchedule as exc:
            # Disabled rather than retried: a malformed cron will never become valid on its
            # own, and re-reading it every poll forever is just noise.
            log.error("trigger %s disabled — %s", trigger.id, exc)
            trigger.enabled = False
            trigger.next_run_at = None
        trigger.last_run_at = now
        db.add(trigger)
        claimed.append(trigger.id)

    await db.commit()
    return claimed


async def reap_stale_sessions(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Close out sessions abandoned by a dead worker.

    Without this a single crash blocks that agent's schedule forever, because the overlap
    guard keeps seeing a run in progress.
    """
    cutoff = (now or datetime.now(UTC)) - STALE_RUN_AFTER
    result = await db.exec(
        update(AgentSession)
        .where(
            AgentSession.status == SessionStatus.running,
            AgentSession.started_at < cutoff,
        )
        .values(
            status=SessionStatus.error,
            finished_at=datetime.now(UTC),
            error="The worker running this agent stopped unexpectedly.",
        )
    )
    await db.commit()
    return result.rowcount or 0


async def is_running(db: AsyncSession, agent_id: UUID) -> bool:
    rows = await db.exec(
        select(AgentSession.id).where(
            AgentSession.agent_id == agent_id,
            AgentSession.status == SessionStatus.running,
        )
    )
    return rows.first() is not None


async def run_trigger(db: AsyncSession, trigger_id: UUID, *, user_input: str | None = None) -> AgentSession | None:
    """Execute the agent behind a trigger. Returns None when the run was skipped.

    Skips are normal operation, not errors: an unpublished or archived agent should not run,
    and neither should one that is already mid-run.
    """
    trigger = await db.get(AgentTrigger, trigger_id)
    if trigger is None:
        return None

    agent = await db.get(Agent, trigger.agent_id)
    if agent is None or agent.status not in (AgentStatus.published,) or not agent.published_config:
        if agent and agent.status == AgentStatus.paused:
            log.info("trigger %s skipped — agent %s is paused", trigger_id, agent.id)
        else:
            log.info("trigger %s skipped — agent is not published", trigger_id)
        return None

    if await is_running(db, agent.id):
        log.info("trigger %s skipped — agent %s is already running", trigger_id, agent.id)
        return None

    return await run_agent(
        db,
        agent,
        trigger_type=trigger.type,
        user_input=user_input,
        dry_run=False,
        use_published=True,
    )


async def fire_channel_triggers(
    db: AsyncSession, connector_id: UUID, user_input: str
) -> list[AgentSession]:
    """Run every agent listening to this connector, for one inbound message."""
    rows = await db.exec(
        select(AgentTrigger).where(
            AgentTrigger.type == TriggerType.channel,
            AgentTrigger.enabled.is_(True),
            AgentTrigger.config["connector_id"].astext == str(connector_id),
        )
    )

    sessions = []
    for trigger in rows.all():
        session = await run_trigger(db, trigger.id, user_input=user_input)
        if session is not None:
            trigger.last_run_at = datetime.now(UTC)
            db.add(trigger)
            sessions.append(session)
    await db.commit()
    return sessions


async def claim_inbound(db: AsyncSession, *, limit: int = 10) -> list[UUID]:
    """Take ownership of queued webhook events, same locking rules as schedules."""
    rows = await db.exec(
        select(InboundEvent)
        .where(InboundEvent.status == InboundEventStatus.pending)
        .order_by(InboundEvent.received_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = []
    for event in rows.all():
        # Marked before the run for the same reason schedules are: a crash mid-run must not
        # replay someone's message to the agent a second time.
        event.status = InboundEventStatus.processed
        event.processed_at = datetime.now(UTC)
        db.add(event)
        claimed.append(event.id)
    await db.commit()
    return claimed


async def run_inbound(db: AsyncSession, event_id: UUID) -> list[AgentSession]:
    """Wake every agent listening to the connector this event arrived on."""
    event = await db.get(InboundEvent, event_id)
    if event is None:
        return []

    # Build an opening message that includes enough context for the agent to pick the right
    # reply tool.  Instagram comment events carry a media_id in their payload; DMs do not.
    _payload = event.payload or {}
    if "media_id" in _payload:
        # Instagram comment — the agent must use reply_to_instagram_comment, not the DM tool.
        _media_id = _payload.get("media_id", "")
        opening = (
            f"Instagram comment from @{event.sender} on post {_media_id}: {event.text}\n"
            f"[comment_id={event.external_id}]"
        )
    else:
        opening = f"Message from {event.sender}: {event.text}" if event.sender else event.text
    try:
        sessions = await fire_channel_triggers(db, event.connector_id, opening)
    except Exception as exc:
        event.status = InboundEventStatus.failed
        event.error = f"{type(exc).__name__}: {exc}"
        db.add(event)
        await db.commit()
        raise

    if not sessions:
        event.status = InboundEventStatus.ignored
        db.add(event)
        await db.commit()
    return sessions


# ── Data retention pruning ─────────────────────────────────────────────────────

async def prune_sessions(db: AsyncSession) -> dict[str, int]:
    """Delete or scrub sessions older than each org's retention policy.

    Returns a dict with counts: {"deleted": N, "scrubbed": N}.

    Called nightly by the worker.  Safe to call multiple times — idempotent.

    Scrub mode:
        Deletes AgentSessionMessage rows for old sessions (removes PII in the
        message content) but keeps the AgentSession header so token/cost
        aggregates remain accurate for reporting.

    Delete mode:
        Deletes the AgentSession rows entirely; messages cascade automatically.
    """
    from datetime import timedelta
    from sqlmodel import delete as sql_delete

    from app.db.models import AgentSessionMessage, Organization

    log.info("retention: starting nightly prune")

    orgs_result = await db.exec(
        select(Organization).where(Organization.data_retention_days.is_not(None))
    )
    orgs = orgs_result.all()

    deleted = 0
    scrubbed = 0
    now = datetime.now(UTC)

    for org in orgs:
        cutoff = now - timedelta(days=org.data_retention_days)

        if org.scrub_content_only:
            # Delete just the messages for old sessions in this org.
            old_sessions = await db.exec(
                select(AgentSession.id).where(
                    AgentSession.org_id == org.id,
                    AgentSession.started_at < cutoff,
                )
            )
            old_ids = [r for r in old_sessions.all()]
            if old_ids:
                await db.exec(
                    sql_delete(AgentSessionMessage).where(
                        AgentSessionMessage.session_id.in_(old_ids)
                    )
                )
                scrubbed += len(old_ids)
                log.info(
                    "retention: scrubbed messages for %d sessions in org %s",
                    len(old_ids), org.id,
                )
        else:
            # Hard delete old sessions (messages cascade).
            old_sessions = await db.exec(
                select(AgentSession).where(
                    AgentSession.org_id == org.id,
                    AgentSession.started_at < cutoff,
                )
            )
            sessions_to_delete = old_sessions.all()
            for s in sessions_to_delete:
                await db.delete(s)
            deleted += len(sessions_to_delete)
            if sessions_to_delete:
                log.info(
                    "retention: deleted %d sessions for org %s",
                    len(sessions_to_delete), org.id,
                )

    await db.commit()
    log.info("retention: done — deleted=%d scrubbed=%d", deleted, scrubbed)
    return {"deleted": deleted, "scrubbed": scrubbed}
