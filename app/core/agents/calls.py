"""
Agent-calls-agent tools
=======================
Builds `call_<name>` tools for each published agent the owner has linked to the
caller. Each tool runs the target as a full, independent session and returns its
final answer (or a clear message when approval is needed). Uses a fresh DB
session per call — never the caller's session — matching the worker pattern so
the two runs' commits never interleave.

Guardrails:
- Published targets only (silently omitted when draft).
- Depth 1: tools are only added when triggered_by_session_id is None, enforced
  in run_agent — but calls.py also trusts that constraint and does not recheck.
- Rate cap: 10 calls per caller→target pair per hour.
- No permission inheritance: the target runs with its own connectors and tools.
- Dry run propagates: simulated callers produce simulated targets.
- Provenance framing: kickoff message names the caller agent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import aliased
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import (
    Agent,
    AgentLink,
    AgentSession,
    AgentSessionMessage,
    AgentStatus,
    MessageRole,
    SessionStatus,
    TriggerType,
)
from app.db.session import AsyncSessionLocal
from app.integrations.base import slug

if TYPE_CHECKING:
    from app.core.agents.base import RegisteredTool

# How many times one caller may invoke the same target in a rolling hour.
RATE_LIMIT_PER_HOUR = 10

# Max chars of the target's final message returned as the tool result.
MAX_RESULT_CHARS = 2_000


async def build_tools(
    db: AsyncSession,
    caller: Agent,
    session_id: UUID,
) -> list["RegisteredTool"]:
    """Return one call_X tool for every published agent linked to the caller.

    Returns an empty list when there are no links (or no published targets), so
    the model is never offered tools that would always fail.
    """
    from app.core.agents.base import RegisteredTool, run_agent
    from app.core.llm.client import ToolSpec

    links_result = await db.exec(
        select(AgentLink, Agent)
        .join(Agent, Agent.id == AgentLink.target_agent_id)
        .where(
            AgentLink.agent_id == caller.id,
            Agent.status == AgentStatus.published,
        )
        .order_by(AgentLink.created_at)
    )
    links = links_result.all()
    if not links:
        return []

    tools: list[RegisteredTool] = []

    for link, target in links:
        # Capture loop variables for the closure.
        _link: AgentLink = link
        _target: Agent = target

        async def handler(
            args: dict,
            dry_run: bool,
            _link: AgentLink = _link,
            _target: Agent = _target,
        ) -> str:
            message = str(args.get("message") or "").strip()
            if not message:
                return "Provide a message describing what you need the agent to do."

            # Rate cap is per caller→target *pair*, not per session: a scheduled caller
            # starts a fresh session every run, so counting within one session would never
            # engage. Join each child run back to its parent to find the calling agent.
            cutoff = datetime.now(UTC) - timedelta(hours=1)
            parent = aliased(AgentSession)
            count_result = await db.exec(
                select(func.count(AgentSession.id))
                .join(parent, parent.id == AgentSession.triggered_by_session_id)
                .where(
                    AgentSession.agent_id == _target.id,
                    AgentSession.trigger_type == TriggerType.agent,
                    parent.agent_id == caller.id,
                    AgentSession.started_at >= cutoff,
                )
            )
            if (count_result.one() or 0) >= RATE_LIMIT_PER_HOUR:
                return (
                    f"Rate limit: already called {_target.name!r} {RATE_LIMIT_PER_HOUR} "
                    "times this hour — do not retry."
                )

            kickoff = f"Agent '{caller.name}' asks: {message}"

            if dry_run:
                return (
                    f"[simulated] Would call agent '{_target.name}' with: {message[:200]}"
                )

            # Fresh session per call — never reuse the caller's DB session.
            async with AsyncSessionLocal() as db2:
                child_session = await run_agent(
                    db2,
                    _target,
                    trigger_type=TriggerType.agent,
                    user_input=kickoff,
                    dry_run=False,
                    use_published=True,
                    triggered_by_session_id=session_id,
                )

                if child_session.status == SessionStatus.succeeded:
                    msg_result = await db2.exec(
                        select(AgentSessionMessage.content)
                        .where(
                            AgentSessionMessage.session_id == child_session.id,
                            AgentSessionMessage.role == MessageRole.assistant,
                        )
                        .order_by(AgentSessionMessage.sequence.desc())
                        .limit(1)
                    )
                    answer = (msg_result.first() or "").strip()
                    if not answer:
                        return f"Agent '{_target.name}' finished but produced no output."
                    truncated = answer[:MAX_RESULT_CHARS]
                    if len(answer) > MAX_RESULT_CHARS:
                        truncated += " … [truncated]"
                    return f"Agent '{_target.name}' finished: {truncated}"

                if child_session.status == SessionStatus.waiting_approval:
                    return (
                        f"Agent '{_target.name}' needs the owner's approval to continue. "
                        "It will finish on its own after approval — proceed without its result."
                    )

                # error or any other terminal state
                detail = child_session.error or "unknown error"
                return f"Agent '{_target.name}' failed: {detail}"

        tool_name = f"call_{slug(_target.name)}"
        tools.append(
            RegisteredTool(
                spec=ToolSpec(
                    name=tool_name,
                    description=_link.description,
                    parameters={
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": (
                                    "What you need the agent to do. Be specific — include "
                                    "names, numbers, and any context it will need to act."
                                ),
                            }
                        },
                        "required": ["message"],
                    },
                ),
                handler=handler,
            )
        )

    return tools
