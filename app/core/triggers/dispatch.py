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
    AgentSessionMessage,
    AgentStatus,
    AgentTrigger,
    Conversation,
    ConversationMessage,
    ConversationStatus,
    InboundEvent,
    InboundEventStatus,
    Organization,
    SessionStatus,
    TriggerType,
)

# How long to wait after the last inbound before firing.
DEBOUNCE_SECONDS = 10
# Hard cap: fire even if messages keep arriving within the debounce window.
DEBOUNCE_MAX_SECONDS = 30

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


async def run_trigger(
    db: AsyncSession,
    trigger_id: UUID,
    *,
    user_input: str | None = None,
    conversation_id: UUID | None = None,
) -> AgentSession | None:
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
        conversation_id=conversation_id,
    )


async def fire_channel_triggers(
    db: AsyncSession,
    connector_id: UUID,
    user_input: str,
    conversation_id: UUID | None = None,
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
        session = await run_trigger(
            db, trigger.id, user_input=user_input, conversation_id=conversation_id
        )
        if session is not None:
            trigger.last_run_at = datetime.now(UTC)
            db.add(trigger)
            sessions.append(session)
    await db.commit()
    return sessions


async def claim_inbound(
    db: AsyncSession,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> list[tuple[UUID, list[UUID]]]:
    """Claim conversation bundles whose debounce window has closed.

    Returns a list of (conversation_id, [event_id, ...]) tuples.

    Strategy:
    - Only conversations that have pending events are considered.
    - A conversation is ready when BOTH:
        (a) its oldest pending event is at least DEBOUNCE_MAX_SECONDS old, OR
            its newest pending event is at least DEBOUNCE_SECONDS old (quiet window),
        (b) no AgentSession for this conversation is currently running.
    - Events are marked processed immediately (before the run) to prevent replay.
    """
    _now = now or datetime.now(UTC)
    debounce_cutoff = _now - timedelta(seconds=DEBOUNCE_SECONDS)
    hard_cutoff = _now - timedelta(seconds=DEBOUNCE_MAX_SECONDS)

    # Find conversations that have pending events
    pending_rows = await db.exec(
        select(InboundEvent)
        .where(InboundEvent.status == InboundEventStatus.pending)
        .where(InboundEvent.conversation_id.is_not(None))
        .order_by(InboundEvent.received_at)
        .with_for_update(skip_locked=True)
    )
    pending_events = pending_rows.all()

    # Group by conversation
    by_conversation: dict[UUID, list[InboundEvent]] = {}
    for ev in pending_events:
        cid = ev.conversation_id
        if cid not in by_conversation:
            by_conversation[cid] = []
        by_conversation[cid].append(ev)

    # Also handle events with no conversation_id (legacy / race condition)
    orphan_events = [ev for ev in pending_events if ev.conversation_id is None]

    ready_bundles: list[tuple[UUID | None, list[UUID]]] = []

    for conversation_id, events in by_conversation.items():
        if len(by_conversation) >= limit:
            break  # claimed enough

        oldest = min(ev.received_at for ev in events)
        newest = max(ev.received_at for ev in events)

        # Debounce: quiet for 10s, or oldest event is 30s old
        if newest > debounce_cutoff and oldest > hard_cutoff:
            continue  # still within debounce window

        # Skip if a run for this conversation is already in progress
        running = await db.exec(
            select(AgentSession.id)
            .where(
                AgentSession.conversation_id == conversation_id,
                AgentSession.status == SessionStatus.running,
                AgentSession.started_at > _now - STALE_RUN_AFTER,
            )
            .limit(1)
        )
        if running.first() is not None:
            continue  # overlap guard — another run is live

        # Check conversation status (skip if human takeover)
        conversation = await db.get(Conversation, conversation_id)
        if conversation and conversation.status == ConversationStatus.human:
            continue

        # Claim these events
        event_ids = []
        for ev in events:
            ev.status = InboundEventStatus.processed
            ev.processed_at = _now
            db.add(ev)
            event_ids.append(ev.id)

        ready_bundles.append((conversation_id, event_ids))

    # Handle orphan events (no conversation_id) — one-at-a-time old behaviour
    for ev in orphan_events[:max(0, limit - len(ready_bundles))]:
        ev.status = InboundEventStatus.processed
        ev.processed_at = _now
        db.add(ev)
        ready_bundles.append((None, [ev.id]))

    await db.commit()
    return ready_bundles


async def run_inbound(
    db: AsyncSession,
    conversation_id: UUID | None,
    event_ids: list[UUID],
) -> list[AgentSession]:
    """Fire agents for a bundle of events from the same conversation.

    When `conversation_id` is set, events are bundled into a single opening message
    and the run is linked to the conversation.  When None (legacy path), behaves as
    before with a single event.
    """
    if not event_ids:
        return []

    events = []
    for eid in event_ids:
        ev = await db.get(InboundEvent, eid)
        if ev is not None:
            events.append(ev)

    if not events:
        return []

    # The connector is the same for all events in a conversation bundle.
    connector_id = events[0].connector_id

    # ── Build the opening message ──────────────────────────────────────────────
    opening = _build_opening(events, conversation_id)

    # Prepend conversation history transcript when a conversation is set
    if conversation_id is not None:
        from app.core.conversations import render_transcript
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None:
            event_message_ids = [
                ev.conversation_message_id
                for ev in events
                if ev.conversation_message_id is not None
            ]
            transcript = await render_transcript(
                db, conversation, exclude_message_ids=event_message_ids
            )
            if transcript:
                opening = f"{transcript}\n\n---\n\n{opening}"

    # ── Fire agents ────────────────────────────────────────────────────────────
    try:
        sessions = await fire_channel_triggers(
            db, connector_id, opening, conversation_id=conversation_id
        )
    except Exception as exc:
        now = datetime.now(UTC)
        for ev in events:
            ev.status = InboundEventStatus.failed
            ev.error = f"{type(exc).__name__}: {exc}"
            db.add(ev)
        await db.commit()
        raise

    if not sessions:
        for ev in events:
            ev.status = InboundEventStatus.ignored
            db.add(ev)
        await db.commit()

    return sessions


def _build_opening(events: list[InboundEvent], conversation_id: UUID | None) -> str:
    """Compose a single opening message from one or more inbound events in a bundle."""
    if len(events) == 1:
        ev = events[0]
        payload = ev.payload or {}
        if "media_id" in payload:
            media_id = payload.get("media_id", "")
            return (
                f"Instagram comment from @{ev.sender} on post {media_id}: {ev.text}\n"
                f"[comment_id={ev.external_id}]"
            )
        lines = []
        if ev.sender:
            lines.append(f"Message from {ev.sender}:")
        lines.append(ev.text or "[media — no text body]")
        # Attach media markers (Phase 1: attachments not yet processed)
        conv_msg = None  # will enrich in Phase 2
        return "\n".join(lines)

    # Multiple messages — bundle them
    sender = events[0].sender or "customer"
    header = f"You have {len(events)} new messages from {sender}:\n"
    parts = []
    for i, ev in enumerate(events, 1):
        body = ev.text or "[media — no text body]"
        parts.append(f"  {i}. {body}")
    return header + "\n".join(parts)


# ── Data retention pruning ─────────────────────────────────────────────────────

async def prune_conversations(db: AsyncSession) -> dict[str, int]:
    """Delete conversation messages and conversations past each org's retention policy.

    Called by `prune_sessions` — no need to call separately.

    Returns {"messages_deleted": N, "conversations_deleted": N, "media_files_deleted": N}.
    """
    from datetime import timedelta
    from sqlmodel import delete as sql_delete

    from app.core import media_store
    from app.db.models import ConversationMessage, Organization

    log.info("retention: starting conversation prune")
    orgs_result = await db.exec(
        select(Organization).where(Organization.data_retention_days.is_not(None))
    )
    orgs = orgs_result.all()

    messages_deleted = 0
    conversations_deleted = 0
    media_files = 0
    now = datetime.now(UTC)

    for org in orgs:
        cutoff = now - timedelta(days=org.data_retention_days)

        # Delete messages past the cutoff first (so we can collect paths for media deletion)
        old_msg_rows = await db.exec(
            select(ConversationMessage).where(
                ConversationMessage.org_id == org.id,
                ConversationMessage.created_at < cutoff,
            )
        )
        old_messages = old_msg_rows.all()
        for msg in old_messages:
            for att in msg.attachments or []:
                stored_path = att.get("stored_path", "")
                if stored_path:
                    if media_store.delete(stored_path):
                        media_files += 1
            await db.delete(msg)
        messages_deleted += len(old_messages)

        # Delete conversations with no recent messages (last_inbound_at < cutoff)
        old_conv_rows = await db.exec(
            select(Conversation).where(
                Conversation.org_id == org.id,
                Conversation.last_inbound_at < cutoff,
            )
        )
        old_convs = old_conv_rows.all()
        for conv in old_convs:
            # Delete any remaining media files for this conversation
            count = media_store.delete_conversation(org.id, conv.id)
            media_files += count
            await db.delete(conv)
        conversations_deleted += len(old_convs)

    await db.commit()
    log.info(
        "retention: conversations — messages_deleted=%d conversations_deleted=%d media=%d",
        messages_deleted, conversations_deleted, media_files,
    )
    return {
        "messages_deleted": messages_deleted,
        "conversations_deleted": conversations_deleted,
        "media_files_deleted": media_files,
    }


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
