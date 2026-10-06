"""Read-only MCP tools. Every one calls the same queries the UI uses."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlmodel import select

from app.api.agents._assist import _build_agent_context_block, run_trace
from app.api.agents.router import list_agents, list_models, list_templates
from app.api.approvals.router import list_approvals
from app.api.code_skills.router import get_code_skill, list_agent_code_skills, list_code_skills
from app.api.mcp.tools.base import McpTool, ToolError
from app.api.skills.router import list_skills
from app.config import settings
from app.core.agents.base import snapshot_config
from app.core.billing.entitlements import resolve
from app.core.triggers import schedule
from app.core.workspace import get_org_timezone
from app.db.models import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentTrigger,
    Connector,
    MessageRole,
    Organization,
    SessionStatus,
)
from fastapi import HTTPException


def dumps(data) -> str:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    elif isinstance(data, list) and data and hasattr(data[0], "model_dump"):
        data = [item.model_dump(mode="json") for item in data]
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


async def _agent(session, principal, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.org_id != principal.org_id:
        raise ToolError("Agent not found")
    return agent


class NoArgs(BaseModel):
    pass


class AgentId(BaseModel):
    agent_id: UUID


class RunsArgs(BaseModel):
    agent_id: UUID
    limit: int = 20
    include_dry_runs: bool = True


class ReadRunArgs(BaseModel):
    agent_id: UUID
    run_number: int | None = None
    run_id: UUID | None = None


class ConnectorId(BaseModel):
    connector_id: UUID


class CodeSkillId(BaseModel):
    code_skill_id: UUID


class ApprovalsArgs(BaseModel):
    status: str = "pending"


class HealthArgs(BaseModel):
    agent_id: UUID
    days: int = 7


async def _workspace(principal, session, args: NoArgs) -> str:
    org = await session.get(Organization, principal.org_id)
    if org is None:
        raise ToolError("Workspace not found")
    ent = await resolve(session, principal.org_id)
    connectors = len((await session.exec(select(Connector.id).where(Connector.org_id == org.id))).all())
    agents = len((await session.exec(select(Agent.id).where(Agent.org_id == org.id))).all())
    return dumps({
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "plan_code": ent.plan_code,
        "features": ent.features,
        "limits": ent.limits,
        "included": ent.included,
        "timezone": get_org_timezone(org),
        "member_role": "owner" if principal.is_owner else "member",
        "connectors_count": connectors,
        "agents_count": agents,
    })


async def _list_agents(principal, session, args: NoArgs) -> str:
    # status filter is applied after the shared list, which already computes health.
    try:
        rows = await list_agents(principal.org_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    data = [r.model_dump(mode="json") for r in rows]
    status = getattr(args, "status", None)
    if status:
        data = [r for r in data if r.get("status") == status]
    keep = {
        "id", "name", "status", "model", "has_unpublished_changes", "published_at",
        "last_run_at", "health_score", "connector_types", "primary_trigger_type",
    }
    return dumps([{k: row.get(k) for k in keep} for row in data])


class ListAgentsArgs(BaseModel):
    status: str | None = None


async def _get_agent(principal, session, args: AgentId) -> str:
    agent = await _agent(session, principal, args.agent_id)
    context = await _build_agent_context_block(session, agent)
    triggers = (await session.exec(
        select(AgentTrigger).where(AgentTrigger.agent_id == agent.id).order_by(AgentTrigger.created_at)
    )).all()
    try:
        code_skills = await list_agent_code_skills(agent.id, principal.user, session)
    except HTTPException:
        code_skills = []
    published = bool(agent.published_config)
    changed = bool(published and agent.published_config != snapshot_config(agent))
    extra = {
        "settings": agent.settings,
        "triggers": [
            {"id": str(t.id), "type": t.type.value, "enabled": t.enabled, "config": t.config}
            for t in triggers
        ],
        "code_skills": [c.model_dump(mode="json") for c in code_skills],
        "publish": {
            "status": agent.status.value,
            "published_at": agent.published_at.isoformat() if agent.published_at else None,
            "has_unpublished_changes": changed,
        },
    }
    return context + "\n\n## Settings\n" + dumps(extra["settings"]) + "\n\n## Triggers\n" + dumps(extra["triggers"]) + "\n\n## Code skills\n" + dumps(extra["code_skills"]) + "\n\n## Publish\n" + dumps(extra["publish"])


async def _recent_runs(session, agent_id: UUID, limit: int, include_dry: bool):
    query = select(AgentSession).where(AgentSession.agent_id == agent_id)
    if not include_dry:
        query = query.where(AgentSession.dry_run.is_(False))
    return (await session.exec(query.order_by(AgentSession.started_at.desc()).limit(limit))).all()


async def _list_runs(principal, session, args: RunsArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    limit = min(max(args.limit, 1), 50)
    runs = await _recent_runs(session, agent.id, limit, args.include_dry_runs)
    return dumps([
        {
            "run_number": n,
            "id": str(r.id),
            "status": r.status.value,
            "trigger_type": r.trigger_type.value if r.trigger_type else "manual",
            "dry_run": r.dry_run,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "tokens": r.total_tokens,
            "error": r.error,
            "name": r.name,
        }
        for n, r in enumerate(runs, 1)
    ])


async def _read_run(principal, session, args: ReadRunArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    if args.run_id is not None:
        runs = await _recent_runs(session, agent.id, 50, True)
        match = next((n for n, r in enumerate(runs, 1) if r.id == args.run_id), None)
        if match is None:
            raise ToolError("Run not found in the 50 most recent runs")
        return await run_trace(session, agent, match)
    return await run_trace(session, agent, args.run_number or 1)


async def _memory(principal, session, args: AgentId) -> str:
    from app.api.agents.router import list_memory
    agent = await _agent(session, principal, args.agent_id)
    try:
        rows = await list_memory(agent.id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps([
        {"key": r.key, "value": r.value, "shared": r.shared, "updated_at": r.updated_at.isoformat()}
        for r in rows
    ])


async def _connectors(principal, session, args: NoArgs) -> str:
    rows = (await session.exec(
        select(Connector).where(Connector.org_id == principal.org_id).order_by(Connector.created_at)
    )).all()
    return dumps([
        {
            "id": str(c.id),
            "name": c.name,
            "type": c.type.value,
            "status": c.status.value,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ])


async def _skills(principal, session, args: NoArgs) -> str:
    try:
        rows = await list_skills(principal.org_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps([
        {
            "id": r.id, "name": r.name, "tagline": r.tagline, "category": r.category,
            "is_default": r.is_default, "content": r.content,
        }
        for r in rows
    ])


async def _code_skills(principal, session, args: NoArgs) -> str:
    if not settings.code_skills_enabled:
        return dumps({"items": [], "note": "Code skills are not enabled on this deployment."})
    try:
        rows = await list_code_skills(principal.org_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    hidden = {"source", "lambda_function_name", "last_deploy_error"}
    return dumps([{k: v for k, v in r.model_dump().items() if k not in hidden} for r in rows])


async def _code_skill(principal, session, args: CodeSkillId) -> str:
    try:
        row = await get_code_skill(args.code_skill_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    if str(row.org_id) != str(principal.org_id):
        raise ToolError("Code skill not found")
    return dumps(row)


async def _models(principal, session, args: ConnectorId) -> str:
    connector = await session.get(Connector, args.connector_id)
    if connector is None or connector.org_id != principal.org_id:
        raise ToolError("Connector not found")
    try:
        return dumps(await list_models(args.connector_id, principal.user, session))
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc


async def _templates(principal, session, args: NoArgs) -> str:
    try:
        return dumps(await list_templates(principal.org_id, principal.user, session))
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc


async def _presets(principal, session, args: NoArgs) -> str:
    return dumps([
        {"key": key, "cron": expression, "label": key.replace("_", " ").capitalize()}
        for key, expression in schedule.PRESETS.items()
    ])


async def _approvals(principal, session, args: ApprovalsArgs) -> str:
    resolved = args.status != "pending"
    try:
        rows = await list_approvals(principal.org_id, principal.user, session, resolved)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    if args.status != "pending":
        rows = [r for r in rows if r.status.value == args.status]
    return dumps([
        {
            "id": str(r.id),
            "agent_id": str(r.agent_id),
            "agent_name": r.agent_name,
            "tool_name": r.tool_name,
            "arguments": r.tool_args,
            "status": r.status.value,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ])


async def _health(principal, session, args: HealthArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    since = datetime.now(UTC) - timedelta(days=max(1, min(args.days, 90)))
    runs = (await session.exec(
        select(AgentSession).where(
            AgentSession.agent_id == agent.id,
            AgentSession.started_at >= since,
        )
    )).all()
    real = [r for r in runs if not r.dry_run]
    errors = [r for r in real if r.status == SessionStatus.error]
    ok = [r for r in real if r.status == SessionStatus.succeeded]
    waiting = [r for r in real if r.status == SessionStatus.waiting_approval]
    failing: dict[str, int] = {}
    if errors:
        ids = [r.id for r in errors]
        messages = (await session.exec(
            select(AgentSessionMessage).where(
                AgentSessionMessage.session_id.in_(ids),
                AgentSessionMessage.role == MessageRole.tool,
            )
        )).all()
        for message in messages:
            if (message.content or "").startswith("Error"):
                failing[message.tool_name or "?"] = failing.get(message.tool_name or "?", 0) + 1
    last_ok = max((r.started_at for r in ok), default=None)
    last_err = max(errors, key=lambda r: r.started_at, default=None)
    return dumps({
        "runs": len(real),
        "ok": len(ok),
        "error": len(errors),
        "waiting_approval": len(waiting),
        "dry_runs": sum(1 for r in runs if r.dry_run),
        "error_rate": round(len(errors) / len(real), 3) if real else None,
        "tokens_total": sum(r.total_tokens for r in real),
        "last_ok_at": last_ok.isoformat() if last_ok else None,
        "last_error_at": last_err.started_at.isoformat() if last_err else None,
        "last_error": last_err.error if last_err else None,
        "failing_tools": [{"tool_name": name, "count": count} for name, count in sorted(failing.items())],
    })


def _tool(name, description, model, handler) -> McpTool:
    return McpTool(name=name, description=description, args_model=model, handler=handler)


TOOLS = {
    t.name: t for t in [
        _tool("setod_get_workspace", "The connected workspace: plan, limits, timezone, and counts.", NoArgs, _workspace),
        _tool("setod_list_agents", "Agents in this workspace, with health of the last 20 live runs.", ListAgentsArgs, _list_agents),
        _tool("setod_get_agent", "One agent: instructions, tools, skills, triggers, settings, and recent runs.", AgentId, _get_agent),
        _tool("setod_list_runs", "Recent runs. run_number 1 is the most recent.", RunsArgs, _list_runs),
        _tool("setod_read_run", "One run's trace: every tool call, its arguments, and what it returned.", ReadRunArgs, _read_run),
        _tool("setod_list_memory", "Key-value memory this agent can see, including shared: keys.", AgentId, _memory),
        _tool("setod_list_connectors", "Connectors in this workspace. Never includes credentials.", NoArgs, _connectors),
        _tool("setod_list_skills", "Prompt skills in the workspace library, including their text.", NoArgs, _skills),
        _tool("setod_list_code_skills", "Code skills, without source. Use setod_get_code_skill for the source.", NoArgs, _code_skills),
        _tool("setod_get_code_skill", "One code skill, including its Python source. Never includes secrets.", CodeSkillId, _code_skill),
        _tool("setod_list_models", "Models the given model connector can run, priced models only.", ConnectorId, _models),
        _tool("setod_list_templates", "Agent templates and which connectors this workspace still needs for each.", NoArgs, _templates),
        _tool("setod_list_schedule_presets", "Named schedules and the cron expression each one stores.", NoArgs, _presets),
        _tool("setod_list_approvals", "Approval requests. status defaults to pending.", ApprovalsArgs, _approvals),
        _tool("setod_agent_health", "Run counts, error rate, and which tools failed, over the last N days.", HealthArgs, _health),
    ]
}

# list_agents handler is registered with ListAgentsArgs, but the function annotation above
# used NoArgs. Rebind so the status filter is actually parsed.
TOOLS["setod_list_agents"] = _tool(
    "setod_list_agents",
    "Agents in this workspace, with health of the last 20 live runs. Optional status: draft, published, or paused.",
    ListAgentsArgs,
    _list_agents,
)
