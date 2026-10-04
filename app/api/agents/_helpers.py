"""Shared helper functions for the agents API sub-modules.

Extracted from router.py (R2 refactor) so that _assist.py and _triggers.py
can import them without creating circular imports.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import assert_org_member, assert_org_owner
from app.db.models import (
    Agent,
    AgentSession,
    AgentTool,
    AgentTrigger,
    Connector,
    ConnectorType,
    SessionStatus,
    User,
)
from app.api.agents.schemas import AgentOut, SessionOut

# LLM provider connector types (not integration connectors)
LLM_PROVIDERS = {ConnectorType.openai, ConnectorType.anthropic}


async def get_owned_agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    """Return the agent if the user is any member of its org, else 403/404."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await assert_org_member(session, user, agent.org_id)
    return agent


async def get_owner_only_agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    """Like get_owned_agent but additionally enforces owner role."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await assert_org_owner(session, user, agent.org_id)
    return agent


def session_out(run: "AgentSession", caller_name: str | None = None) -> SessionOut:
    """Convert an AgentSession ORM row to a SessionOut schema."""
    out = SessionOut.model_validate(run)
    out.total_tokens = run.total_tokens
    out.triggered_by_agent_name = caller_name
    return out


async def with_health(session: AsyncSession, out: AgentOut) -> AgentOut:
    """Attach run health stats. Best-effort — DB error leaves defaults."""
    try:
        rows = await session.exec(
            select(AgentSession.status, AgentSession.started_at)
            .where(
                AgentSession.agent_id == out.id,
                AgentSession.dry_run.is_(False),
            )
            .order_by(AgentSession.started_at.desc())
            .limit(20)
        )
        records = rows.all()
        if records:
            statuses = [r[0] for r in records]
            succeeded = sum(1 for s in statuses if s == SessionStatus.succeeded)
            out.health_score = round(succeeded / len(statuses), 3)
            out.last_run_at = records[0][1]
    except Exception:  # noqa: BLE001
        pass
    try:
        trig_rows = await session.exec(
            select(AgentTrigger.type)
            .where(AgentTrigger.agent_id == out.id, AgentTrigger.enabled.is_(True))
            .order_by(AgentTrigger.created_at.asc())
            .limit(1)
        )
        trig = trig_rows.first()
        if trig:
            out.primary_trigger_type = trig
    except Exception:  # noqa: BLE001
        pass
    try:
        ct_rows = await session.exec(
            select(Connector.type)
            .join(AgentTool, AgentTool.connector_id == Connector.id)
            .where(
                AgentTool.agent_id == out.id,
                Connector.type.notin_(LLM_PROVIDERS),
            )
        )
        out.connector_types = [ct.value for ct in ct_rows.all()]
    except Exception:  # noqa: BLE001
        pass
    return out
