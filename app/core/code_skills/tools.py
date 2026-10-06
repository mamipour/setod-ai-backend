"""Expose ready code skills to an agent run as LLM tools."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.core.billing.entitlements import EntitlementError, resolve
from app.core.billing.usage import record_event
from app.core.code_skills import get_backend, service
from app.db.models import Agent, AgentCodeSkillLink, CodeSkill, CodeSkillDeployStatus
from app.db.session import AsyncSessionLocal

log = logging.getLogger("setod.code_skills")

MAX_CALLS_PER_RUN = 25


async def _record(skill_id: UUID, text: str, *, ok: bool, duration_ms: int, agent_id: UUID, session_id: UUID | None) -> None:
    async with AsyncSessionLocal() as db:
        fresh = await db.get(CodeSkill, skill_id)
        if fresh is None:
            return
        fresh.invocation_count += 1
        fresh.last_invoked_at = datetime.now(UTC)
        if not ok:
            fresh.last_error = text[:2000]
        db.add(fresh)
        await record_event(
            db,
            org_id=fresh.org_id,
            meter="code_invocations",
            quantity=1.0,
            idempotency_key=f"session:{session_id}:code:{fresh.id}:{uuid4().hex}",
            agent_id=agent_id,
            session_id=session_id,
            cost_usd=0.0,
            meta={"skill_id": str(fresh.id), "duration_ms": duration_ms, "ok": ok},
        )


async def build_tools(db: AsyncSession, agent: Agent, session_id: UUID | None):
    """Code-skill tools for one run, plus the subset that requires approval.

    Returns ``([], set())`` when the feature is off or the agent has no ready code skills.
    Attached skills that are not ready are skipped.
    """
    from app.core.agents.base import RegisteredTool
    from app.core.llm.client import ToolSpec

    if not settings.code_skills_enabled:
        return [], set()

    rows = (await db.exec(
        select(CodeSkill, AgentCodeSkillLink)
        .join(AgentCodeSkillLink, AgentCodeSkillLink.code_skill_id == CodeSkill.id)
        .where(AgentCodeSkillLink.agent_id == agent.id)
        .order_by(CodeSkill.name)
    )).all()
    if not rows:
        return [], set()

    calls_made = [0]
    tools = []
    approval: set[str] = set()

    for skill, link in rows:
        if skill.deploy_status != CodeSkillDeployStatus.ready.value:
            log.info("skipping code skill %s — status is %s", skill.id, skill.deploy_status)
            continue
        tool_name = f"code_{skill.tool_name}"
        if link.requires_approval:
            approval.add(tool_name)

        skill_id = skill.id
        read_only = skill.read_only
        display_name = skill.name

        async def handler(
            args: dict,
            dry_run: bool,
            *,
            _skill_id: UUID = skill_id,
            _read_only: bool = read_only,
            _display_name: str = display_name,
        ) -> str:
            if calls_made[0] >= MAX_CALLS_PER_RUN:
                return "Error: this run has reached the limit of 25 code-skill calls."
            if dry_run and not _read_only:
                return f"Would run code skill `{_display_name}` with {json.dumps(args, default=str)[:500]}"
            calls_made[0] += 1
            async with AsyncSessionLocal() as s:
                fresh = await s.get(CodeSkill, _skill_id)
                if fresh is None or fresh.deploy_status != CodeSkillDeployStatus.ready.value:
                    return "Error: this code skill is no longer available."
                ent = await resolve(s, fresh.org_id)
                snapshot = {
                    "function_name": fresh.lambda_function_name or service.function_name_for(fresh.id),
                    "timeout_seconds": fresh.timeout_seconds,
                    "org_id": fresh.org_id,
                    "skill_id": fresh.id,
                }
            try:
                ent.require_quota("code_invocations")
            except EntitlementError:
                return "Error: code skill quota reached for this workspace."
            payload_args = args if isinstance(args, dict) else {}
            if len(json.dumps(payload_args, default=str).encode()) > 65_536:
                return "Error: input too large"
            backend = get_backend()
            try:
                res = await backend.invoke(
                    function_name=snapshot["function_name"],
                    payload={
                        "input": payload_args,
                        "context": {
                            "org_id": str(agent.org_id),
                            "agent_id": str(agent.id),
                            "session_id": str(session_id) if session_id else None,
                            "skill_id": str(snapshot["skill_id"]),
                            "dry_run": dry_run,
                            "invoked_at": datetime.now(UTC).isoformat(),
                        },
                    },
                    timeout_seconds=snapshot["timeout_seconds"],
                    want_logs=False,
                )
            except Exception as exc:  # noqa: BLE001
                text = f"Error: {type(exc).__name__}: {exc}"
                await _record(snapshot["skill_id"], text, ok=False, duration_ms=0, agent_id=agent.id, session_id=session_id)
                return text
            text = service.result_text(res["payload"])
            ok = bool(res["payload"] and res["payload"].get("ok"))
            await _record(
                snapshot["skill_id"], text, ok=ok, duration_ms=res["duration_ms"],
                agent_id=agent.id, session_id=session_id,
            )
            return text

        tools.append(RegisteredTool(
            spec=ToolSpec(
                name=tool_name,
                description=skill.tool_description + "\n\n(Custom code skill written by your workspace owner.)",
                parameters=skill.input_schema,
            ),
            handler=handler,
        ))

    return tools, approval
