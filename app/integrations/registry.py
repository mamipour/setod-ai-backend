"""
Tool registry
=============
Turns an agent's `AgentTool` rows into the concrete tool list the runner hands to the model.

The registry owns three things the individual integrations should not have to care about:
which connector types can produce tools, how to auto-assign aliases when an agent has two
connectors of the same type, and how to flush the run's idempotency ledger.
"""

import logging
from uuid import UUID

from sqlalchemy import delete as sa_delete, update as sa_update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import (
    AgentProcessedItem,
    AgentSession,
    AgentTool,
    Connector,
    ConnectorStatus,
    ConnectorType,
    ProcessedItemStatus,
    SessionStatus,
)
from app.integrations import gmail, hubspot, instagram, mcp, pipedrive, sheets, slack, telegram, twilio, whatsapp
from app.integrations.base import RegisteredTool, ToolContext, slug

log = logging.getLogger(__name__)

BUILDERS = {
    ConnectorType.gmail: gmail.build_tools,
    ConnectorType.telegram_bot: telegram.build_tools,
    ConnectorType.telegram_client: telegram.build_tools,
    ConnectorType.twilio: twilio.build_tools,
    ConnectorType.mcp: mcp.build_tools,
    ConnectorType.slack_webhook: slack.build_tools,
    ConnectorType.google_sheets: sheets.build_tools,
    ConnectorType.whatsapp: whatsapp.build_tools,
    ConnectorType.instagram: instagram.build_tools,
    ConnectorType.hubspot: hubspot.build_tools,
    ConnectorType.pipedrive: pipedrive.build_tools,
}


# Connectors that can wake an agent by pushing an event to us. Gmail is deliberately absent:
# its push API needs a Google Cloud Pub/Sub topic, and a schedule plus `read_unread_emails`
# already covers "when a new email arrives" — the processed-item ledger makes polling exact.
INBOUND_TYPES = {
    ConnectorType.telegram_bot,
    ConnectorType.telegram_client,
    ConnectorType.twilio,
    ConnectorType.webhook,    # generic inbound webhook
    ConnectorType.whatsapp,   # Meta Cloud API inbound
    ConnectorType.instagram,  # Meta Instagram inbound (comments + DMs)
}


def supported_types() -> list[str]:
    return [t.value for t in BUILDERS]


async def build_tools_for_agent(
    db: AsyncSession, agent_id: UUID
) -> tuple[list[RegisteredTool], list[ToolContext], set[str]]:
    """Every tool this agent can call, plus the contexts holding the run's seen-item list,
    plus the set of tool names that require human approval before execution.

    Contexts come back alongside the tools because the runner needs them after the model is
    done, to flush what the read tools surfaced.
    """
    rows = await db.exec(
        select(AgentTool, Connector)
        .join(Connector, Connector.id == AgentTool.connector_id)
        .where(AgentTool.agent_id == agent_id)
        .order_by(AgentTool.created_at)
    )
    pairs = rows.all()

    # An alias is only needed when two connectors of one type collide on the same agent.
    type_counts: dict[ConnectorType, int] = {}
    for _, connector in pairs:
        type_counts[connector.type] = type_counts.get(connector.type, 0) + 1

    tools: list[RegisteredTool] = []
    contexts: list[ToolContext] = []
    approval_required: set[str] = set()

    for agent_tool, connector in pairs:
        builder = BUILDERS.get(connector.type)
        if builder is None:
            continue
        if connector.status != ConnectorStatus.active:
            log.warning(
                "agent %s skipping connector %s — status is %s",
                agent_id, connector.id, connector.status.value,
            )
            continue

        alias = agent_tool.alias
        if not alias and type_counts[connector.type] > 1:
            alias = connector.name

        ctx = ToolContext(db=db, agent_id=agent_id, connector=connector, alias=alias)
        built = builder(ctx)

        if agent_tool.enabled_tools:
            allowed = set(agent_tool.enabled_tools)
            built = [t for t in built if t.spec.name in allowed or _base_name(t.spec.name, alias) in allowed]

        if agent_tool.approval_tools:
            gated_bases = set(agent_tool.approval_tools)
            for t in built:
                if _base_name(t.spec.name, alias) in gated_bases:
                    approval_required.add(t.spec.name)

        tools.extend(built)
        contexts.append(ctx)

    return _dedupe(tools), contexts, approval_required


async def flush_seen(
    db: AsyncSession,
    agent_id: UUID,
    contexts: list[ToolContext],
    session_id: UUID | None,
    *,
    status: ProcessedItemStatus = ProcessedItemStatus.permanent,
) -> int:
    """Write the items surfaced by read tools to the idempotency ledger.

    Two modes controlled by `status`:

    `permanent` (default, used at run-end):
        Rows are written as confirmed.  On conflict, an existing `in_flight` row is
        upgraded to `permanent` — handles the case where a prior approval run reserved
        the item and the current run is confirming it.

    `in_flight` (used at approval-pause time):
        Rows are reserved for this session.  Other concurrent or subsequent runs see the
        row and skip the item.  On conflict, the existing row is left untouched — a
        `permanent` record from a previous run must never be downgraded.
    """
    rows = [
        {
            "agent_id": agent_id,
            "connector_id": connector_id,
            "external_id": external_id,
            "session_id": session_id,
            "status": status,
        }
        for ctx in contexts
        for connector_id, external_id in ctx.seen
    ]
    if not rows:
        return 0

    if status == ProcessedItemStatus.permanent:
        # Upgrade any in_flight row for the same item to permanent.
        stmt = insert(AgentProcessedItem).values(rows).on_conflict_do_update(
            constraint="uq_agent_processed_item",
            set_={"status": ProcessedItemStatus.permanent, "session_id": session_id},
        )
    else:
        # Reserve: never overwrite a row that already exists (might be permanent).
        stmt = insert(AgentProcessedItem).values(rows).on_conflict_do_nothing(
            constraint="uq_agent_processed_item",
        )

    await db.exec(stmt)
    await db.commit()
    return len(rows)


async def confirm_seen(db: AsyncSession, session_id: UUID) -> int:
    """Upgrade all in_flight reservations for a session to permanent.

    Called after a resumed session finishes successfully, confirming that the items
    reserved at pause-time were properly handled.
    """
    stmt = (
        sa_update(AgentProcessedItem)
        .where(
            AgentProcessedItem.session_id == session_id,
            AgentProcessedItem.status == ProcessedItemStatus.in_flight,
        )
        .values(status=ProcessedItemStatus.permanent)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount


async def release_abandoned_reservations(db: AsyncSession) -> int:
    """Delete in_flight rows whose session has ended without confirming them.

    Runs on every worker tick.  Covers three failure modes:
    - Approval rejected: session ends as error/succeeded but in_flight rows were never
      confirmed (because confirm_seen was not called for rejected outcomes).
    - Worker crash: session stuck in running/waiting_approval never finished.
    - Any other unhandled exit path.

    Rows for active sessions (running, waiting_approval) are deliberately left alone.
    """
    dead_statuses = [SessionStatus.error, SessionStatus.succeeded]
    stmt = sa_delete(AgentProcessedItem).where(
        AgentProcessedItem.status == ProcessedItemStatus.in_flight,
        AgentProcessedItem.session_id.in_(
            select(AgentSession.id).where(AgentSession.status.in_(dead_statuses))
        ),
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount


def _base_name(name: str, alias: str) -> str:
    """`send_email_sales` back to `send_email`, so enabled_tools can be stored unaliased."""
    suffix = f"_{slug(alias)}" if alias else ""
    return name[: -len(suffix)] if suffix and name.endswith(suffix) else name


def _dedupe(tools: list[RegisteredTool]) -> list[RegisteredTool]:
    """Last line of defence against duplicate names, which both providers reject outright."""
    seen: set[str] = set()
    out: list[RegisteredTool] = []
    for tool in tools:
        if tool.spec.name in seen:
            log.warning("dropping duplicate tool name %s", tool.spec.name)
            continue
        seen.add(tool.spec.name)
        out.append(tool)
    return out
