"""
Scheduler worker
================
A long-running process that wakes every `POLL_SECONDS`, claims whatever schedules are due,
and runs those agents off the request thread.

    python -m app.tasks.worker

Run more than one for redundancy: claiming uses `SKIP LOCKED`, so instances divide the work
instead of duplicating it. Each claimed trigger gets its own database session, because a run
takes long enough that holding one open across the whole batch would pin a connection for
minutes at a time.
"""

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.agents.base import resume_agent
from app.core.knowledge import claim_pending_files, index_file
from app.integrations.registry import release_abandoned_reservations
from app.core.triggers.dispatch import (
    claim_due,
    claim_inbound,
    prune_sessions,
    reap_stale_sessions,
    run_inbound,
    run_trigger,
)
from app.db.models import AgentKnowledgeFile, ApprovalRequest, ApprovalStatus, SessionStatus
from app.db.session import AsyncSessionLocal, engine

log = logging.getLogger("worker")

POLL_SECONDS = 20
# Ceiling on how many due triggers one poll takes on. Keeps a backlog from turning into a
# hundred concurrent LLM calls the moment the worker comes back up.
BATCH = 10
# Runs are IO-bound on the provider, so this can exceed the core count comfortably.
MAX_CONCURRENT_RUNS = 4
REAP_EVERY = timedelta(minutes=10)
PRUNE_EVERY = timedelta(hours=24)


async def _run_scheduled(trigger_id: UUID, limiter: asyncio.Semaphore) -> None:
    """Run one claimed schedule in its own database session.

    Failures are logged and swallowed: the trigger's next occurrence is already scheduled, and
    one agent erroring must not stop the loop that serves every other agent.
    """
    async with limiter:
        try:
            async with AsyncSessionLocal() as db:
                session = await run_trigger(db, trigger_id)
            if session is not None:
                log.info(
                    "schedule %s → session %s (%s, %d tokens)",
                    trigger_id, session.id, session.status.value, session.total_tokens,
                )
        except Exception:
            log.exception("schedule %s failed", trigger_id)


async def _run_inbound(event_id: UUID, limiter: asyncio.Semaphore) -> None:
    async with limiter:
        try:
            async with AsyncSessionLocal() as db:
                sessions = await run_inbound(db, event_id)
            log.info("event %s woke %d agent(s)", event_id, len(sessions))
        except Exception:
            log.exception("event %s failed", event_id)


async def _expire_approvals() -> int:
    """Auto-reject any approval requests that have passed their deadline."""
    from datetime import UTC, datetime
    from sqlmodel import select
    async with AsyncSessionLocal() as db:
        rows = await db.exec(
            select(ApprovalRequest).where(
                ApprovalRequest.status == ApprovalStatus.pending,
                ApprovalRequest.expires_at <= datetime.now(UTC),
            )
        )
        expired = rows.all()
        for req in expired:
            req.status = ApprovalStatus.expired
            req.response_note = "No response within 24 hours."
            req.resolved_at = datetime.now(UTC)
            db.add(req)
        if expired:
            await db.commit()
    return len(expired)


async def _claim_resolved_approvals() -> list[UUID]:
    """Return session IDs whose approval was decided and that are still waiting."""
    from sqlmodel import select
    from app.db.models import AgentSession
    async with AsyncSessionLocal() as db:
        rows = await db.exec(
            select(ApprovalRequest.session_id)
            .join(AgentSession, AgentSession.id == ApprovalRequest.session_id)
            .where(
                ApprovalRequest.status.in_([
                    ApprovalStatus.approved, ApprovalStatus.rejected, ApprovalStatus.expired,
                ]),
                AgentSession.status == SessionStatus.waiting_approval,
            )
        )
        return list(rows.all())


async def _resume_approved(session_id: UUID, limiter: asyncio.Semaphore) -> None:
    async with limiter:
        try:
            async with AsyncSessionLocal() as db:
                session = await resume_agent(db, session_id)
            log.info(
                "resumed session %s → %s (%d tokens)",
                session_id, session.status.value, session.total_tokens,
            )
        except Exception:
            log.exception("failed to resume session %s", session_id)


async def _index_knowledge(file_id: UUID, limiter: asyncio.Semaphore) -> None:
    """Embed one uploaded knowledge file. index_file records failures on the row itself."""
    async with limiter:
        try:
            async with AsyncSessionLocal() as db:
                file = await db.get(AgentKnowledgeFile, file_id)
                if file is not None:
                    await index_file(db, file)
                    log.info(
                        "knowledge file %s (%s) → %s",
                        file.filename, file_id, file.status.value,
                    )
        except Exception:
            log.exception("knowledge file %s failed", file_id)


async def poll_once(limiter: asyncio.Semaphore) -> int:
    """One tick: drain inbound webhook events, then run whatever schedules are due, then
    index any freshly uploaded knowledge files.

    Inbound goes first because someone is waiting on the other end of it, while a schedule
    slipping by one tick is invisible.
    """
    async with AsyncSessionLocal() as db:
        events = await claim_inbound(db, limit=BATCH)
        triggers = await claim_due(db, limit=BATCH)
        files = await claim_pending_files(db, limit=BATCH)

    # Release in_flight reservations for sessions that ended without confirming them.
    # (approval rejections, crashes, max-iteration failures, etc.)
    async with AsyncSessionLocal() as db:
        released = await release_abandoned_reservations(db)
    if released:
        log.info("released %d abandoned in_flight reservation(s)", released)

    # Approval lifecycle: expire stale requests first, then pick up resolved ones.
    expired = await _expire_approvals()
    if expired:
        log.info("expired %d approval request(s)", expired)
    resumable = await _claim_resolved_approvals()

    if events:
        log.info("claimed %d inbound event(s)", len(events))
        await asyncio.gather(*(_run_inbound(e, limiter) for e in events))
    if triggers:
        log.info("claimed %d due schedule(s)", len(triggers))
        await asyncio.gather(*(_run_scheduled(t, limiter) for t in triggers))
    if files:
        log.info("claimed %d knowledge file(s)", len(files))
        await asyncio.gather(*(_index_knowledge(f, limiter) for f in files))
    if resumable:
        log.info("resuming %d approved session(s)", len(resumable))
        await asyncio.gather(*(_resume_approved(s, limiter) for s in resumable))
    return len(events) + len(triggers) + len(files) + len(resumable)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
    engine.echo = False

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    limiter = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    next_reap = datetime.now(UTC)
    next_prune = datetime.now(UTC)
    log.info("scheduler started — polling every %ds", POLL_SECONDS)

    try:
        while not stopping.is_set():
            try:
                if datetime.now(UTC) >= next_reap:
                    async with AsyncSessionLocal() as db:
                        reaped = await reap_stale_sessions(db)
                    if reaped:
                        log.warning("closed %d abandoned session(s)", reaped)
                    next_reap = datetime.now(UTC) + REAP_EVERY

                if datetime.now(UTC) >= next_prune:
                    try:
                        async with AsyncSessionLocal() as db:
                            counts = await prune_sessions(db)
                        if counts["deleted"] or counts["scrubbed"]:
                            log.info(
                                "retention: deleted=%d scrubbed=%d",
                                counts["deleted"], counts["scrubbed"],
                            )
                    except Exception:
                        log.exception("retention prune failed")
                    next_prune = datetime.now(UTC) + PRUNE_EVERY

                await poll_once(limiter)
            except Exception:
                # The loop itself must survive anything — a transient database blip should
                # cost one tick, not the scheduler.
                log.exception("poll failed")

            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=POLL_SECONDS)
    finally:
        log.info("scheduler stopping")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
