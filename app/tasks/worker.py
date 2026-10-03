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
from app.core.media_ingest import process_pending_media
from app.integrations.registry import release_abandoned_reservations
from app.core.triggers.dispatch import (
    claim_due,
    claim_inbound,
    prune_conversations,
    prune_sessions,
    reap_stale_sessions,
    run_inbound,
    run_trigger,
)
from app.db.models import AgentKnowledgeFile, ApprovalRequest, ApprovalStatus, SessionStatus
from app.db.session import AsyncSessionLocal, engine

log = logging.getLogger("worker")

# Inbound events are polled frequently so the 10s debounce window resolves quickly.
POLL_SECONDS = 5
# Ceiling on how many due triggers one poll takes on. Keeps a backlog from turning into a
# hundred concurrent LLM calls the moment the worker comes back up.
BATCH = 10
# Runs are IO-bound on the provider, so this can exceed the core count comfortably.
MAX_CONCURRENT_RUNS = 4
REAP_EVERY = timedelta(minutes=10)
PRUNE_EVERY = timedelta(hours=24)
ROLLUP_EVERY = timedelta(hours=1)
# Slower tasks (schedules, files, approvals) run every 4th inbound tick (~20s).
_SLOW_TASK_DIVISOR = 4


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


async def _run_inbound(
    conversation_id: UUID | None,
    event_ids: list[UUID],
    limiter: asyncio.Semaphore,
) -> None:
    async with limiter:
        try:
            async with AsyncSessionLocal() as db:
                sessions = await run_inbound(db, conversation_id, event_ids)
            log.info(
                "conv %s bundle(%d events) → %d agent run(s)",
                conversation_id, len(event_ids), len(sessions),
            )
        except Exception:
            log.exception("conv %s bundle failed", conversation_id)
            # If the DB transaction was left in an aborted state (PendingRollbackError),
            # any running AgentSession for this conversation will be stuck in 'running'
            # status, blocking all future events.  Open a fresh connection and clean up.
            try:
                from app.db.models import AgentSession, SessionStatus
                from sqlalchemy import text as _text
                async with AsyncSessionLocal() as db2:
                    await db2.exec(
                        _text(
                            "UPDATE agent_sessions SET status='error', finished_at=now(), "
                            "error='PendingRollbackError — worker cleaned up' "
                            "WHERE conversation_id = :cid AND status = 'running'"
                        ).bindparams(cid=conversation_id)
                    )
                    await db2.commit()
            except Exception:
                log.exception("conv %s cleanup also failed", conversation_id)


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


async def poll_once(limiter: asyncio.Semaphore, *, tick: int = 0) -> int:
    """One tick: drain inbound webhook events, then run whatever schedules are due, then
    index any freshly uploaded knowledge files.

    Inbound goes first because someone is waiting on the other end of it, while a schedule
    slipping by one tick is invisible.

    `tick` is the monotonic poll counter. Slow tasks (schedules, files, approvals) run only
    every _SLOW_TASK_DIVISOR ticks to keep the 5s inbound loop cheap.
    """
    # ── Media processing (every tick, before inbound so new events may already have media) ──
    try:
        async with AsyncSessionLocal() as db:
            media_processed = await process_pending_media(db)
        if media_processed:
            log.info("processed %d pending media attachment(s)", media_processed)
    except Exception:
        log.exception("process_pending_media failed")

    # ── Inbound (every tick) ──────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        bundles = await claim_inbound(db, limit=BATCH)

    if bundles:
        log.info("claimed %d conversation bundle(s)", len(bundles))
        await asyncio.gather(*(
            _run_inbound(conv_id, eids, limiter)
            for conv_id, eids in bundles
        ))

    # ── Slow tasks (every 4th tick ≈ 20s) ─────────────────────────────────────
    if tick % _SLOW_TASK_DIVISOR != 0:
        return len(bundles)

    async with AsyncSessionLocal() as db:
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

    if triggers:
        log.info("claimed %d due schedule(s)", len(triggers))
        await asyncio.gather(*(_run_scheduled(t, limiter) for t in triggers))
    if files:
        log.info("claimed %d knowledge file(s)", len(files))
        await asyncio.gather(*(_index_knowledge(f, limiter) for f in files))
    if resumable:
        log.info("resuming %d approved session(s)", len(resumable))
        await asyncio.gather(*(_resume_approved(s, limiter) for s in resumable))
    return len(bundles) + len(triggers) + len(files) + len(resumable)


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
    next_rollup = datetime.now(UTC)
    tick = 0
    log.info("scheduler started — polling every %ds (slow tasks every %ds)",
             POLL_SECONDS, POLL_SECONDS * _SLOW_TASK_DIVISOR)

    try:
        while not stopping.is_set():
            try:
                if datetime.now(UTC) >= next_reap:
                    async with AsyncSessionLocal() as db:
                        reaped = await reap_stale_sessions(db)
                    if reaped:
                        log.warning("closed %d abandoned session(s)", reaped)
                    next_reap = datetime.now(UTC) + REAP_EVERY

                if datetime.now(UTC) >= next_rollup:
                    try:
                        from app.core.billing.usage import rollup_usage_periods
                        async with AsyncSessionLocal() as db:
                            await rollup_usage_periods(db)
                        log.info("usage rollup completed")
                    except Exception:
                        log.exception("usage rollup failed")
                    next_rollup = datetime.now(UTC) + ROLLUP_EVERY

                if datetime.now(UTC) >= next_prune:
                    try:
                        async with AsyncSessionLocal() as db:
                            counts = await prune_sessions(db)
                        if counts["deleted"] or counts["scrubbed"]:
                            log.info(
                                "retention: sessions deleted=%d scrubbed=%d",
                                counts["deleted"], counts["scrubbed"],
                            )
                        async with AsyncSessionLocal() as db:
                            conv_counts = await prune_conversations(db)
                        if any(conv_counts.values()):
                            log.info(
                                "retention: conv messages=%d conversations=%d media=%d",
                                conv_counts["messages_deleted"],
                                conv_counts["conversations_deleted"],
                                conv_counts["media_files_deleted"],
                            )
                    except Exception:
                        log.exception("retention prune failed")
                    next_prune = datetime.now(UTC) + PRUNE_EVERY

                await poll_once(limiter, tick=tick)
                tick += 1
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
