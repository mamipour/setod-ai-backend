"""
Agents API
==========
CRUD for agents plus running them.

Routes
------
POST   /agents/                          create agent (draft)
GET    /agents/                          list agents for an org
GET    /agents/{id}                      agent detail
PATCH  /agents/{id}                      update — every field optional, builder autosaves
DELETE /agents/{id}                      delete agent and its sessions
POST   /agents/{id}/publish              snapshot the draft into published_config
POST   /agents/{id}/unpublish            take offline, keep the draft
POST   /agents/{id}/run                  run once, returns the finished session
GET    /agents/{id}/sessions             list sessions, newest first
GET    /agents/{id}/sessions/{sid}       session detail with full message thread
GET    /agents/{id}/knowledge            list uploaded knowledge files
POST   /agents/{id}/knowledge            upload a document (indexed by the worker)
DELETE /agents/{id}/knowledge/{fid}      remove a document and its chunks
GET    /agents/models                    models available on a provider connector
GET    /agents/templates                 template gallery, annotated with what the org has
GET    /agents/schedule-presets          plain-language schedule options
GET    /agents/overview                  workspace dashboard: counts, today's runs, latest

The static routes above are declared before `/{agent_id}`, since FastAPI matches in
declaration order and would otherwise read "templates" as an agent id.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.agents.schemas import (
    AgentCreate,
    AgentLinkCreate,
    AgentLinkOut,
    AgentOut,
    AgentRunRequest,
    AgentToolAttach,
    AgentToolOut,
    AgentUpdate,
    KnowledgeFileOut,
    SessionDetailOut,
    SessionOut,
    ToolOut,
    TriggerOut,
    TriggerUpsert,
)
from app.api.auth.dependencies import get_current_user
from app.core.agents import templates
from app.core.agents.base import AgentRunError, RegisteredTool, run_agent, snapshot_config
from app.core.agents.templates import TEMPLATES
from app.core import knowledge
from app.core.crypto import decrypt_json
from app.core.llm.client import (
    DEFAULT_MODELS,
    ToolSpec,
    build_client,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)
from app.integrations import websearch as _websearch
from app.core.triggers import schedule
from app.integrations.base import ToolContext
from app.integrations import mcp
from app.integrations.registry import BUILDERS, INBOUND_TYPES
from app.db.models import (
    Agent,
    AgentAssistMessage,
    AgentKnowledgeFile,
    AgentLink,
    AgentProcessedItem,
    AgentPublishSnapshot,
    AgentSession,
    AgentSessionMessage,
    AgentSkillLink,
    AgentStatus,
    AgentTool,
    AgentTrigger,
    Connector,
    ConnectorStatus,
    ConnectorType,
    DEFAULT_AGENT_SETTINGS,
    MessageRole,
    Organization,
    OrganizationMember,
    SessionStatus,
    Skill,
    TriggerType,
    User,
)
from app.db.session import get_session

router = APIRouter(prefix="/agents", tags=["agents"])
log = logging.getLogger("setod.assist")

# Channel triggers are switched off by product decision (2026-08-26): they require a public
# webhook URL, which in development means babysitting a tunnel, and cron scheduling covers
# every current template. The webhook receivers, inbound event queue, and worker path all
# stay intact — flip this to True to bring the feature back.
CHANNEL_TRIGGERS_ENABLED = False

LLM_PROVIDERS = {ConnectorType.openai, ConnectorType.anthropic}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _assert_org_member(session: AsyncSession, user: User, org_id: UUID) -> None:
    result = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if result.first() is None:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")


async def _get_owned_agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _assert_org_member(session, user, agent.org_id)
    return agent


def _to_out(agent: Agent) -> AgentOut:
    out = AgentOut.model_validate(agent)
    # Surfaces the "you have unpublished edits" indicator in the builder header.
    out.has_unpublished_changes = bool(
        agent.published_config and agent.published_config != snapshot_config(agent)
    )
    return out


async def _assert_model_connector(
    session: AsyncSession, org_id: UUID, connector_id: UUID | None
) -> None:
    if connector_id is None:
        return
    connector = await session.get(Connector, connector_id)
    if connector is None or connector.org_id != org_id:
        raise HTTPException(status_code=404, detail="Model connector not found")
    if connector.type not in LLM_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"{connector.name} is not an AI model provider",
        )


# Must stay above /{agent_id}: FastAPI matches in declaration order, so a literal path
# declared after a dynamic one of the same shape is unreachable.
@router.get("/schedule-presets")
async def list_schedule_presets():
    """Plain-language schedule options for the builder, so it need not hardcode cron."""
    return [
        {"key": key, "cron": expression, "label": key.replace("_", " ").capitalize()}
        for key, expression in schedule.PRESETS.items()
    ]


@router.get("/models")
async def list_models(
    connector_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Models available on a provider connector, newest first.

    Asked of the provider rather than hardcoded, because model ids go stale — Anthropic
    retired `claude-sonnet-4` mid-2026 and a baked-in list would have started every affected
    run with a 404. If the provider cannot be reached the list comes back empty rather than
    erroring: the agent runs fine on the default, so a provider outage should not also block
    someone from editing a name.
    """
    connector = await session.get(Connector, connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    await _assert_org_member(session, current_user, connector.org_id)
    if connector.type not in (ConnectorType.openai, ConnectorType.anthropic):
        raise HTTPException(status_code=422, detail="That connector is not a model provider")

    config = decrypt_json(connector.config)
    key = config.get("api_key", "")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if connector.type == ConnectorType.openai:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
            else:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models?limit=100",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
        if resp.status_code != 200:
            return {"models": [], "detail": f"Provider returned {resp.status_code}"}
        data = resp.json().get("data", [])
    except httpx.HTTPError as exc:
        return {"models": [], "detail": str(exc)}

    if connector.type == ConnectorType.openai:
        # OpenAI's list includes embeddings, moderation, TTS and image models, none of which
        # can hold a conversation. Filtering by capability is not possible from this endpoint,
        # so it goes by prefix.
        chat = [m for m in data if m["id"].startswith(("gpt-", "o1", "o3", "o4"))]
        chat.sort(key=lambda m: m.get("created", 0), reverse=True)
        models = [{"id": m["id"], "label": m["id"]} for m in chat]
    else:
        models = [{"id": m["id"], "label": m.get("display_name") or m["id"]} for m in data]

    return {"models": models, "default": DEFAULT_MODELS.get(connector.type.value, "")}


@router.get("/templates")
async def list_templates(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """The template gallery, annotated with what this workspace has already connected.

    The readiness check is done here rather than in the browser because the UI would
    otherwise need its own copy of the rule for which connector types satisfy which
    requirement — a rule that would then drift from the one the create flow enforces.
    """
    await _assert_org_member(session, current_user, org_id)

    rows = await session.exec(
        select(Connector).where(
            Connector.org_id == org_id,
            Connector.status == ConnectorStatus.active,
        )
    )
    owned = {c.type for c in rows.all()}

    out = []
    for template in TEMPLATES:
        missing = [c.value for c in template.required_connectors if c not in owned]
        out.append({**template.as_dict(), "missing_connectors": missing, "ready": not missing})
    return out


@router.get("/overview")
async def workspace_overview(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tz: Annotated[str, Query()] = "UTC",
):
    """One call for the dashboard: counts, today's activity, and the latest runs.

    Aggregated here rather than assembled in the browser, because the alternative is the UI
    fetching sessions per agent — a request per agent on every dashboard visit.
    """
    await _assert_org_member(session, current_user, org_id)

    # Resolve the user's timezone, falling back to UTC for unknown values.
    try:
        user_tz = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError):
        user_tz = ZoneInfo("UTC")

    agents_result = await session.exec(select(Agent).where(Agent.org_id == org_id))
    all_agents = agents_result.all()
    by_id = {a.id: a for a in all_agents}

    # Compute local-day boundaries and convert to UTC for the DB query (timestamps are UTC).
    now_local = datetime.now(user_tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight = midnight_local.astimezone(UTC)

    today = await session.exec(
        select(
            func.count(AgentSession.id),
            func.count(AgentSession.id).filter(AgentSession.status == SessionStatus.error),
            func.coalesce(
                func.sum(AgentSession.prompt_tokens + AgentSession.completion_tokens), 0
            ),
        ).where(
            AgentSession.org_id == org_id,
            AgentSession.started_at >= midnight,
        )
    )
    runs_today, failures_today, tokens_today = today.one()

    yesterday_midnight = midnight - timedelta(days=1)
    yesterday = await session.exec(
        select(
            func.count(AgentSession.id),
            func.count(AgentSession.id).filter(AgentSession.status == SessionStatus.error),
            func.coalesce(
                func.sum(AgentSession.prompt_tokens + AgentSession.completion_tokens), 0
            ),
        ).where(
            AgentSession.org_id == org_id,
            AgentSession.started_at >= yesterday_midnight,
            AgentSession.started_at < midnight,
        )
    )
    runs_yesterday, failures_yesterday, tokens_yesterday = yesterday.one()

    # Last 7 days of activity for the dashboard chart, bucketed in the user's local timezone.
    # AT TIME ZONE converts the stored UTC timestamp to local time before truncating to day.
    week_start = midnight - timedelta(days=6)
    day_col = func.date_trunc(
        "day",
        func.timezone(tz, AgentSession.started_at),
    )
    per_day_result = await session.exec(
        select(
            day_col,
            func.count(AgentSession.id),
            func.count(AgentSession.id).filter(AgentSession.status == SessionStatus.error),
        )
        .where(
            AgentSession.org_id == org_id,
            AgentSession.started_at >= week_start,
        )
        .group_by(day_col)
    )
    per_day = {row[0].date().isoformat(): (row[1], row[2]) for row in per_day_result.all()}
    daily_runs = []
    for i in range(7):
        day = (midnight_local - timedelta(days=6 - i)).date().isoformat()
        runs, failures = per_day.get(day, (0, 0))
        daily_runs.append({"date": day, "runs": runs, "failures": failures})

    recent_result = await session.exec(
        select(AgentSession)
        .where(AgentSession.org_id == org_id)
        .order_by(AgentSession.started_at.desc())
        .limit(8)
    )

    recent = []
    for run in recent_result.all():
        agent = by_id.get(run.agent_id)
        out = SessionOut.model_validate(run).model_dump()
        out["total_tokens"] = run.total_tokens
        out["agent_name"] = agent.name if agent else "Deleted agent"
        out["agent_icon"] = agent.icon if agent else "robot"
        recent.append(out)

    return {
        "agents_live": sum(1 for a in all_agents if a.status == AgentStatus.published),
        "agents_total": len(all_agents),
        "runs_today": runs_today,
        "failures_today": failures_today,
        "tokens_today": tokens_today,
        "runs_yesterday": runs_yesterday,
        "failures_yesterday": failures_yesterday,
        "tokens_yesterday": tokens_yesterday,
        "daily_runs": daily_runs,
        "recent_sessions": recent,
    }


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.post("/", response_model=AgentOut, status_code=201)
async def create_agent(
    body: AgentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Create a draft agent, optionally pre-filled from a template.

    A template supplies defaults, not overrides: the create flow shows the instructions in an
    editable box before this is called, so anything the caller sends explicitly wins.
    """
    await _assert_org_member(session, current_user, body.org_id)
    await _assert_model_connector(session, body.org_id, body.model_connector_id)

    template = templates.get(body.template_key) if body.template_key else None
    if body.template_key and template is None:
        raise HTTPException(status_code=404, detail="Unknown template")

    settings_ = dict(DEFAULT_AGENT_SETTINGS)
    if template:
        settings_.update(template.settings)
    settings_.update(body.settings or {})

    agent = Agent(
        org_id=body.org_id,
        created_by=current_user.id,
        name=body.name or (template.name if template else "Untitled agent"),
        icon=body.icon or (template.icon if template else "robot"),
        instructions=body.instructions or (template.instructions if template else ""),
        template_key=body.template_key,
        model_connector_id=body.model_connector_id,
        model=body.model,
        settings=settings_,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_org_member(session, current_user, org_id)
    result = await session.exec(
        select(Agent).where(Agent.org_id == org_id).order_by(Agent.created_at.desc())
    )
    return [_to_out(a) for a in result.all()]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    return _to_out(await _get_owned_agent(session, current_user, agent_id))


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    changes = body.model_dump(exclude_unset=True)

    if "model_connector_id" in changes:
        await _assert_model_connector(session, agent.org_id, changes["model_connector_id"])

    # Settings merge rather than replace, so the builder can PATCH one toggle at a time.
    if (incoming := changes.pop("settings", None)) is not None:
        agent.settings = {**agent.settings, **incoming}

    for key, value in changes.items():
        setattr(agent, key, value)

    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)

    # No ON DELETE CASCADE on these FKs, so children go first.
    sessions = await session.exec(select(AgentSession.id).where(AgentSession.agent_id == agent.id))
    session_ids = list(sessions.all())
    # Processed items before sessions: their session_id FK is SET NULL, but their agent_id
    # FK is plain, so leaving them would block the agent delete itself.
    await session.exec(delete(AgentProcessedItem).where(AgentProcessedItem.agent_id == agent.id))
    if session_ids:
        await session.exec(
            delete(AgentSessionMessage).where(AgentSessionMessage.session_id.in_(session_ids))
        )
    await session.exec(delete(AgentSession).where(AgentSession.agent_id == agent.id))
    await session.exec(delete(AgentTool).where(AgentTool.agent_id == agent.id))
    await session.exec(delete(AgentTrigger).where(AgentTrigger.agent_id == agent.id))
    # Knowledge files last-but-one: their chunks cascade from the file rows.
    await session.exec(delete(AgentKnowledgeFile).where(AgentKnowledgeFile.agent_id == agent.id))
    await session.delete(agent)
    await session.commit()


# ── Publish ───────────────────────────────────────────────────────────────────

@router.post("/{agent_id}/publish", response_model=AgentOut)
async def publish_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    if agent.model_connector_id is None:
        raise HTTPException(status_code=422, detail="Choose an AI model before publishing")
    if not agent.instructions.strip():
        raise HTTPException(status_code=422, detail="Add instructions before publishing")

    config = snapshot_config(agent)

    # Count existing snapshots to assign the next version number
    version_result = await session.exec(
        select(func.count(AgentPublishSnapshot.id)).where(
            AgentPublishSnapshot.agent_id == agent.id
        )
    )
    next_version = (version_result.one() or 0) + 1

    snapshot = AgentPublishSnapshot(
        agent_id=agent.id,
        org_id=agent.org_id,
        published_by=current_user.id,
        config=config,
        version=next_version,
    )
    session.add(snapshot)

    agent.published_config = config
    agent.status = AgentStatus.published
    agent.published_at = datetime.now(UTC)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


@router.post("/{agent_id}/unpublish", response_model=AgentOut)
async def unpublish_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    agent.published_config = None
    agent.status = AgentStatus.draft
    agent.published_at = None
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


@router.get("/{agent_id}/publish-history")
async def publish_history(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """All publish snapshots for an agent, newest first."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(AgentPublishSnapshot)
        .where(AgentPublishSnapshot.agent_id == agent.id)
        .order_by(AgentPublishSnapshot.published_at.desc())
    )
    snapshots = result.all()
    return [
        {
            "id": str(s.id),
            "version": s.version,
            "published_at": s.published_at.isoformat(),
            "model": s.config.get("model") or "—",
            "instructions_preview": (s.config.get("instructions") or "")[:2000],
        }
        for s in snapshots
    ]


@router.post("/{agent_id}/rollback/{snapshot_id}", response_model=AgentOut)
async def rollback_agent(
    agent_id: UUID,
    snapshot_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Restore a past snapshot to the draft (does not auto-publish)."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    snapshot = await session.get(AgentPublishSnapshot, snapshot_id)
    if snapshot is None or snapshot.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    cfg = snapshot.config
    agent.instructions = cfg.get("instructions") or ""
    agent.model = cfg.get("model")
    agent.model_connector_id = UUID(cfg["model_connector_id"]) if cfg.get("model_connector_id") else None
    agent.settings = cfg.get("settings") or agent.settings
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


# ── Run ───────────────────────────────────────────────────────────────────────

@router.post("/{agent_id}/run", response_model=SessionOut)
async def run_agent_once(
    agent_id: UUID,
    body: AgentRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Run synchronously and return the finished session.

    Fine for manual runs and previews. Scheduled and channel-triggered runs go through the
    arq worker instead — a 150-second run cannot sit on an HTTP request.
    """
    agent = await _get_owned_agent(session, current_user, agent_id)
    if agent.org_id != body.org_id:
        raise HTTPException(status_code=403, detail="Agent belongs to another workspace")

    use_published = not body.use_draft
    if use_published and not agent.published_config:
        raise HTTPException(
            status_code=422,
            detail="This agent has not been published yet. Use the preview to run the draft.",
        )

    try:
        result = await run_agent(
            session,
            agent,
            trigger_type=TriggerType.manual,
            user_input=body.message,
            dry_run=body.dry_run,
            use_published=use_published,
        )
    except AgentRunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _session_out(result)


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/sessions", response_model=list[SessionOut])
async def list_sessions(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(AgentSession)
        .where(AgentSession.agent_id == agent.id)
        .order_by(AgentSession.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = result.all()

    # Resolve caller agent names for runs started by another agent.
    parent_session_ids = [
        r.triggered_by_session_id for r in runs if r.triggered_by_session_id
    ]
    caller_names: dict[UUID, str] = {}
    if parent_session_ids:
        parent_result = await session.exec(
            select(AgentSession.id, Agent.name)
            .join(Agent, Agent.id == AgentSession.agent_id)
            .where(AgentSession.id.in_(parent_session_ids))
        )
        caller_names = {row[0]: row[1] for row in parent_result.all()}

    return [
        _session_out(s, caller_names.get(s.triggered_by_session_id))
        for s in runs
    ]


@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionDetailOut)
async def get_session_detail(
    agent_id: UUID,
    session_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    run = await session.get(AgentSession, session_id)
    if run is None or run.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await session.exec(
        select(AgentSessionMessage)
        .where(AgentSessionMessage.session_id == run.id)
        .order_by(AgentSessionMessage.sequence)
    )
    detail = SessionDetailOut.model_validate(run)
    detail.total_tokens = run.total_tokens
    detail.messages = list(messages.all())
    return detail


def _session_out(run: AgentSession, caller_name: str | None = None) -> SessionOut:
    out = SessionOut.model_validate(run)
    out.total_tokens = run.total_tokens
    out.triggered_by_agent_name = caller_name
    return out


# ── Agent calls (agent-as-tool links) ────────────────────────────────────────

@router.get("/{agent_id}/calls", response_model=list[AgentLinkOut])
async def list_agent_calls(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AgentLinkOut]:
    """Return all agents this agent is allowed to call as tools."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(AgentLink, Agent)
        .join(Agent, Agent.id == AgentLink.target_agent_id)
        .where(AgentLink.agent_id == agent.id)
        .order_by(AgentLink.created_at)
    )
    return [
        AgentLinkOut(
            id=link.id,
            agent_id=link.agent_id,
            target_agent_id=link.target_agent_id,
            target_agent_name=target.name,
            target_agent_status=target.status,
            description=link.description,
            created_at=link.created_at,
        )
        for link, target in result.all()
    ]


@router.post("/{agent_id}/calls", response_model=AgentLinkOut, status_code=201)
async def attach_agent_call(
    agent_id: UUID,
    body: AgentLinkCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentLinkOut:
    """Grant this agent the ability to call another published agent as a tool."""
    caller = await _get_owned_agent(session, current_user, agent_id)

    if body.target_agent_id == caller.id:
        raise HTTPException(status_code=422, detail="An agent cannot call itself.")

    target = await session.get(Agent, body.target_agent_id)
    if target is None or target.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="Target agent not found in this workspace.")
    if target.status != AgentStatus.published:
        raise HTTPException(
            status_code=422,
            detail="Only published agents can be called. Publish the target agent first.",
        )

    if not body.description.strip():
        raise HTTPException(status_code=422, detail="Description cannot be empty.")

    # Upsert by pair — idempotent, just updates the description on re-attach.
    existing_result = await session.exec(
        select(AgentLink).where(
            AgentLink.agent_id == caller.id,
            AgentLink.target_agent_id == target.id,
        )
    )
    existing = existing_result.first()
    if existing:
        existing.description = body.description.strip()
        session.add(existing)
        await session.commit()
        await session.refresh(existing)
        link = existing
    else:
        link = AgentLink(
            agent_id=caller.id,
            target_agent_id=target.id,
            description=body.description.strip(),
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)

    return AgentLinkOut(
        id=link.id,
        agent_id=link.agent_id,
        target_agent_id=link.target_agent_id,
        target_agent_name=target.name,
        target_agent_status=target.status,
        description=link.description,
        created_at=link.created_at,
    )


@router.delete("/{agent_id}/calls/{link_id}", status_code=204)
async def detach_agent_call(
    agent_id: UUID,
    link_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove an agent-call link."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    link = await session.get(AgentLink, link_id)
    if link is None or link.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Link not found.")
    await session.delete(link)
    await session.commit()


# ── Assistant ─────────────────────────────────────────────────────────────────

_ASSIST_SYSTEM = """You are a prompt engineer embedded inside setod, an AI agent automation platform.

Your only job is to help the user write, improve, and debug the instruction prompt for their agent.

## What you have access to

Every request includes a `<agent_context>` block injected into the system with the latest live data:
- The agent's **current instructions** (the full prompt it runs with)
- The **tools** currently attached (which connectors and which tool functions are enabled)
- The **skills** currently attached (reusable behaviour rules the agent runs alongside its instructions)
- The **last up to 20 run logs**, each showing: status (ok / error / waiting_approval), timestamp, trigger type, token count, and a one-line summary

Use this data proactively:
- If the user asks whether the agent is healthy, check the recent run statuses and summaries — tell them what you see (e.g. "Your last 3 runs all errored", "Looks healthy — 5 successful runs in the past week")
- If the user asks why something went wrong, look at the run statuses and summaries before asking them to share logs
- If the user asks to improve the prompt, read the current instructions first so your suggestions are grounded in what's already there
- If no tools are attached, proactively note that the agent can't take any actions yet
- If a skill is relevant to the user's goal (e.g. they describe wanting silence when idle), mention it: "You already have the Silence when idle skill — attach it to this agent from the Skills section in the Agent tab"

## The setod ecosystem — what you know

Agents on setod:
- Have a single system prompt (the "instructions") that governs all their behaviour
- Run on a cron schedule (every N minutes/hours, or at a specific time)
- Are powered by OpenAI or Anthropic models — the user picks one
- Must be published before they go live; drafts are safe to experiment with

Connectors available (tools the agent can call):
- Google — read unread emails, send email, list and create primary-calendar events
- Telegram Bot — send a message to a configured chat (notifications only, one direction)
- Telegram Client — read messages from any chat/group the user's account has access to, send messages
- Twilio — send SMS
- MCP — remote HTTPS tool servers (GitHub, Linear, Notion, Slack, Atlassian, Zapier, or a custom URL). Tools are whatever the user attached from that server; do not invent MCP tool names.

- Other agents — call a published agent as a tool (call_X) and get its final answer back. The target runs with its own accounts and approval rules. Depth is capped at 1 — a called agent cannot itself call agents.

Tool behaviour rules every prompt must respect:
- Agents can only use tools from connectors the user has attached
- Reading tools (Gmail read, Telegram read) are safe to call freely
- Writing tools (send email, send SMS, send Telegram) should be called once per run unless the prompt explicitly allows more
- Agents have no memory between runs unless the prompt explicitly builds one from tool output
- There is no file system and no code execution. Web search is a settings toggle. MCP tools only exist if the user attached an MCP connector.

Human approval:
- Individual tools can be flagged "requires approval" — the agent will pause and wait before executing them
- Useful for any action that is irreversible or external-facing

Skills:
- Skills are reusable prompt fragments stored in the org's Skills library
- Each skill covers one concern: a behaviour rule, output format, or safety guardrail
- Skills are attached per-agent from the Agent tab → Skills section
- When attached, a skill's content is injected into the agent's system prompt automatically — the user does not need to copy its text into the instructions
- Default skills available in every org: Silence when idle, No duplicate actions, One action per run, Urgency first, After-hours notifications only, Escalate when unsure, Professional tone, Concise run summary, No PII in summaries, Stop gracefully at budget, Lead qualification
- Users can edit any skill or create their own from the Skills page (sidebar → Skills)
- When writing a prompt, you should NOT duplicate behaviour that a skill already handles — instead tell the user to attach the relevant skill

## Research tools (you can use these yourself)

You have two tools available in this conversation:

- `search_web` — search the web and get titles, URLs, and short snippets. Use it whenever you need to look something up to give a grounded answer.
- `fetch_page` — fetch the full text of any public URL. Use it to read a page and understand its structure before writing a prompt that references it.

- `read_run_trace` — read one of this agent's past runs step by step: every tool it called, the arguments it passed, what each tool returned, and its closing message. Runs are numbered in the context below, 1 being the most recent.

Use these tools proactively when the user gives you a URL or asks about a third-party service you are not certain about. Do not invent API formats, RSS URLs, or field names — verify them.

Call at most 4–6 tools per reply. Stop as soon as you have enough information to write the prompt.

## Diagnosing a run

When the user asks why the agent did or did not do something on a run — a missing link, a message that never arrived, the wrong items picked — call `read_run_trace` first. The run list in your context shows only status and token count; the trace shows what actually happened.

Read the trace before forming a theory. It tells you which tool the agent called, the exact arguments it passed (an outbound message body appears here, so you can see precisely what was sent), and what each tool returned — so you can tell a prompt problem apart from a tool returning data that did not contain what the prompt asked for.

Never tell the user you cannot see the run's internal trace or the content of a message it sent. You can: read it.

## URL monitoring protocol

When the user wants the agent to periodically check a website for new or changed content:

1. **Fetch the URL** the user gave you. Read the page text.
2. **Look for a better data source.** Check the page for links labelled "download", "API", "CSV", "RSS", "open data", or "dataset". Also run a search for `"[site domain]" API OR RSS OR "open data"` to find official feeds. Prefer sources in this order: API > CSV/dataset > RSS > filtered HTML listing > unfiltered pages. Each step down costs the user more tokens per run and breaks more easily, so the difference is real money: a structured source is often 10–20x cheaper per run than page-by-page browsing.
3. **Verify the best endpoint** by fetching it. Confirm you get readable, structured content.
4. **Write instructions that embed the verified endpoint** — the exact URL the agent should fetch — plus extraction hints (column names, keywords to filter on, what a match looks like). Do not leave the URL discovery to the agent at runtime.
   - **If only HTML listings exist**, write instructions that use the site's own filters and sorting (query parameters for category, status, newest-first) so one page carries the most relevant rows, and give the agent a stop rule: read newest-first and stop fetching further pages or detail links as soon as items fall outside the monitoring window (older than N days, already seen, wrong status). Fetched pages may end with a "[truncated — showing X of Y lines]" note; that means the page continued, so narrow the filters or follow the pagination link rather than assuming everything was seen.
5. **Check the agent's web settings** (shown in `<agent_context>`). If web search or live page access is off, tell the user: "Go to this agent's Settings tab and enable Web search and Live page access — the agent needs those to fetch the URL during its runs."
6. **If the page returns almost no text** (likely a JavaScript-rendered SPA), say so honestly: "This page appears to need a browser to load — the agent's built-in fetch tool will not see content here. Look for an RSS feed, API, or data download on the site instead."

## When the user asks for something not yet possible

If the user describes a need that setod does not currently support (reading Slack, WhatsApp, Notion; running code; persistent memory; event-based triggers), respond:
"That's not something setod supports yet. If it's important for your workflow, send a feature request to support — the team reviews them and prioritises based on demand. In the meantime, here's the closest thing you can do with what's available: [suggest an alternative if one exists]"

Never say "you could connect X" if X is not in the list of available connectors.

## When the user asks for something possible but not yet connected

You know which connectors are currently attached to this agent (shown in the context above). If the user describes a goal that requires a connector that exists on setod but is not attached:
1. Tell them which connector they need and what it does
2. Give them the exact path: "Go to Connectors → connect [X] → then come back to this agent's Agent tab → add it under Tools"
3. Offer to pre-write the prompt now so it's ready when they connect it

Available connectors and what they require:
- Google — a Google account (Gmail + Calendar), authorised via OAuth
- Telegram Bot — a bot token + an admin chat ID
- Telegram Client — the user's own Telegram account, authorised via phone number
- Twilio — a Twilio account SID, auth token, and a provisioned phone number
- MCP — Connectors → MCP servers. Catalog cards (GitHub, Linear, Notion, Slack, Atlassian, Zapier) or a custom HTTPS URL. Auth is probed: OAuth, a pasted bearer token, or none.

## Your job

When the user describes what they want their agent to do:
1. Ask one clarifying question if the goal is ambiguous — no more
2. Write a complete, ready-to-use instruction prompt
3. Present it in a code block so the user can copy or click "Apply"

When the user shares an existing prompt and asks for improvements:
1. Identify the specific problem (vague trigger, no silence rule, missing tool constraint, etc.)
2. Rewrite the prompt with the fix applied
3. Explain in one sentence what changed and why

When the user shares run logs and asks why something went wrong:
1. Read the logs carefully
2. Identify the root cause
3. Suggest a specific prompt change that prevents it — quote the exact line to add or change

## Output rules
- Always present the final prompt in a fenced code block
- Never invent connector types, tool names, or platform features not listed above
- If a silence rule is needed and the agent has the "Silence when idle" skill attached, do NOT include a silence rule in the prompt — the skill already handles it. If the skill is not attached, include it and suggest attaching the skill instead
- Keep prompts concise — under 400 words unless the task genuinely requires more
- If the user asks something unrelated to their agent's prompt, redirect them:
  "I can help with your agent's instructions — what would you like the agent to do?"
"""


# All tools each connector type can expose; used when enabled_tools is null/empty (= all on).
_ALL_CONNECTOR_TOOLS: dict[str, list[str]] = {
    "gmail": [
        "read_unread_emails",
        "search_emails",
        "send_email",
        "reply_to_email",
        "archive_email",
        "list_calendar_events",
        "create_calendar_event",
    ],
    "telegram_bot": ["send_telegram_message"],
    "telegram_client": ["read_telegram_messages", "send_telegram_message"],
    "twilio": ["send_sms"],
}


# Caps for a run trace handed to the copilot. Per-step so one giant tool result (a fetched
# CSV, a full inbox) cannot swallow the trace, and overall so a long run still fits in a
# reply. The middle is elided rather than the tail — the closing message and the last tool
# calls are usually what the question is about.
TRACE_STEP_CHARS = 1_200
TRACE_TOTAL_CHARS = 10_000


async def _run_trace(session: AsyncSession, agent: Agent, run_number: int) -> str:
    """Render one past run's message trace for the copilot to read.

    Includes tool arguments, not just results: the body of an outbound message lives in
    `tool_args`, so without it the copilot cannot see what the agent actually sent.
    """
    runs_result = await session.exec(
        select(AgentSession)
        .where(AgentSession.agent_id == agent.id)
        .order_by(AgentSession.started_at.desc())
        .limit(20)
    )
    runs = runs_result.all()
    if not runs:
        return "This agent has no runs yet."
    if run_number < 1 or run_number > len(runs):
        return f"No run numbered {run_number}. This agent has {len(runs)} recent run(s), numbered 1 (most recent) to {len(runs)}."

    run = runs[run_number - 1]
    msgs_result = await session.exec(
        select(AgentSessionMessage)
        .where(AgentSessionMessage.session_id == run.id)
        .order_by(AgentSessionMessage.sequence)
    )
    messages = msgs_result.all()

    steps: list[str] = []
    for m in messages:
        body = (m.content or "").strip()
        if len(body) > TRACE_STEP_CHARS:
            body = body[:TRACE_STEP_CHARS] + " …[truncated]"
        if m.role == MessageRole.tool:
            args = json.dumps(m.tool_args or {}, ensure_ascii=False)
            if len(args) > TRACE_STEP_CHARS:
                args = args[:TRACE_STEP_CHARS] + " …[truncated]"
            steps.append(f"CALLED {m.tool_name}({args})\n  RETURNED: {body}")
        else:
            steps.append(f"{m.role.value.upper()}: {body}")

    # Keep the head and the tail, drop the middle if the whole thing will not fit.
    total = sum(len(s) for s in steps)
    if total > TRACE_TOTAL_CHARS:
        head, tail, budget = [], [], TRACE_TOTAL_CHARS // 2
        spent = 0
        for s in steps:
            if spent + len(s) > budget:
                break
            head.append(s); spent += len(s)
        spent = 0
        for s in reversed(steps[len(head):]):
            if spent + len(s) > budget:
                break
            tail.insert(0, s); spent += len(s)
        omitted = len(steps) - len(head) - len(tail)
        steps = head + ([f"…[{omitted} step(s) omitted]…"] if omitted > 0 else []) + tail

    started = run.started_at.strftime("%Y-%m-%d %H:%M UTC") if run.started_at else "?"
    header = (
        f"Run [{run_number}] — {run.status.value.upper()}, started {started}, "
        f"trigger {run.trigger_type.value if run.trigger_type else 'manual'}, "
        f"{run.total_tokens or 0} tokens"
        + (f", error: {run.error}" if run.error else "")
    )
    return header + "\n\n" + "\n\n".join(steps)


async def _build_agent_context_block(session: AsyncSession, agent: Agent) -> str:
    """Build a live <agent_context> block for the system prompt on every request."""
    tool_rows = await session.exec(select(AgentTool).where(AgentTool.agent_id == agent.id))
    tool_lines = []
    for at in tool_rows.all():
        connector = await session.get(Connector, at.connector_id)
        if connector:
            # None or [] both mean "all tools enabled" (same convention as the agent runner)
            if connector.type == ConnectorType.mcp and connector.config:
                frozen = decrypt_json(connector.config).get("tools") or []
                catalog = [t.get("name") for t in frozen if t.get("name")]
            else:
                catalog = _ALL_CONNECTOR_TOOLS.get(connector.type.value, [])
            active = at.enabled_tools or catalog
            tool_lines.append(f"  - {connector.name or connector.type.value} ({', '.join(active)})")

    skills_result = await session.exec(
        select(Skill)
        .join(AgentSkillLink, AgentSkillLink.skill_id == Skill.id)
        .where(AgentSkillLink.agent_id == agent.id)
        .order_by(Skill.category, Skill.name)
    )
    skill_lines = [f"  - {s.name} ({s.category})" for s in skills_result.all()]

    calls_result = await session.exec(
        select(AgentLink, Agent)
        .join(Agent, Agent.id == AgentLink.target_agent_id)
        .where(AgentLink.agent_id == agent.id)
        .order_by(AgentLink.created_at)
    )
    call_lines = [
        f"  - {target.name} [{target.status.value}]: {link.description}"
        for link, target in calls_result.all()
    ]

    runs_result = await session.exec(
        select(AgentSession)
        .where(AgentSession.agent_id == agent.id)
        .order_by(AgentSession.started_at.desc())
        .limit(20)
    )
    run_lines = []
    # Numbered so the copilot can name one when calling read_run_trace. 1 = most recent.
    for n, r in enumerate(runs_result.all(), 1):
        date = r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "?"
        tokens = f" | {r.total_tokens} tok" if r.total_tokens else ""
        error = f" | error: {r.error[:80]}" if r.error else ""
        trigger = r.trigger_type.value if r.trigger_type else "manual"
        run_lines.append(f"  [{n}] [{r.status.value.upper()}] {date} ({trigger}){tokens}{error}")

    settings = {**DEFAULT_AGENT_SETTINGS, **(agent.settings or {})}
    web_on = settings.get("web_search", False)
    page_on = settings.get("live_page_access", False)
    web_status = (
        "Web search ON, live page access ON"
        if web_on and page_on
        else "Web search ON, live page access OFF"
        if web_on
        else "Web search OFF (both toggles off)"
    )

    return (
        f"<agent_context>\n"
        f"Agent: {agent.name}\n"
        f"Model: {agent.model or 'not set'}\n"
        f"Web settings: {web_status}\n\n"
        f"Current instructions:\n```\n{agent.instructions or '(empty)'}\n```\n\n"
        f"Attached tools:\n{chr(10).join(tool_lines) or '  (none)'}\n\n"
        f"Attached skills:\n{chr(10).join(skill_lines) or '  (none)'}\n\n"
        f"Agents it can call:\n{chr(10).join(call_lines) or '  (none)'}\n\n"
        f"Last {len(run_lines)} runs:\n{chr(10).join(run_lines) or '  (no runs yet)'}\n"
        f"</agent_context>"
    )


class AssistChatRequest(BaseModel):
    content: str
    model_connector_id: UUID


@router.get("/{agent_id}/assist/messages")
async def list_assist_messages(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Return the full persistent conversation thread for this agent."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(AgentAssistMessage)
        .where(AgentAssistMessage.agent_id == agent.id)
        .order_by(AgentAssistMessage.created_at)
    )
    return [{"id": str(m.id), "role": m.role, "content": m.content} for m in result.all()]


@router.post("/{agent_id}/assist/chat")
async def assist_chat(
    agent_id: UUID,
    body: AssistChatRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Save the user message, stream the assistant response, then save the completed reply."""
    agent = await _get_owned_agent(session, current_user, agent_id)

    connector = await session.get(Connector, body.model_connector_id)
    if not connector or connector.org_id != agent.org_id:
        raise HTTPException(status_code=404, detail="Model connector not found")

    config = decrypt_json(connector.config)
    api_key = config.get("api_key", "")
    # Pick the best reasoning-capable model from our copilot preference list.
    # Falls back to the agent's configured model when nothing from the list is available,
    # so the copilot always gets a valid model even on restricted API keys.
    configured_model = config.get("model") or agent.model or ""
    from app.core.llm.client import pick_copilot_model
    model = await pick_copilot_model(connector.type.value, api_key, fallback=configured_model)
    llm = build_client(connector.type.value, api_key, model)

    # Persist the user message immediately
    user_msg = AgentAssistMessage(agent_id=agent.id, role="user", content=body.content)
    session.add(user_msg)
    await session.commit()

    # Load full thread history (including the message we just saved)
    history_result = await session.exec(
        select(AgentAssistMessage)
        .where(AgentAssistMessage.agent_id == agent.id)
        .order_by(AgentAssistMessage.created_at)
    )
    history = history_result.all()

    # Build the fresh context block and inject it into the system prompt.
    # NOTE: REASONING_PREAMBLE is intentionally NOT used here. The models we
    # select (gpt-5-mini, claude-sonnet-5, etc.) reason natively and silently.
    # Adding the preamble caused the model to output its internal monologue as
    # visible reply text ("Reasoning: I reviewed..."), which clutters the chat.
    context_block = await _build_agent_context_block(session, agent)
    full_system = _ASSIST_SYSTEM + "\n\n" + context_block

    msgs: list[dict] = [system_message(full_system)]
    for m in history:
        msgs.append({"role": m.role, "content": m.content})

    # Research tools — always available to the copilot, no connector required.
    from app.core.workspace import get_tavily_key
    _copilot_org = await session.get(Organization, agent.org_id)
    _research_tools = _websearch.build_tools(
        live_page_access=True,
        context_size="medium",
        tavily_api_key=get_tavily_key(_copilot_org),
    )

    # Lets the copilot read what an agent actually did on a past run: the tools it called,
    # the arguments it passed (which is where an outbound message body lives), and what
    # came back. Without it the copilot only sees run status and has to guess at causes.
    async def _trace_handler(args: dict, dry_run: bool) -> str:
        try:
            run_number = int(args.get("run", 1))
        except (TypeError, ValueError):
            return "Error: 'run' must be a whole number, 1 for the most recent run."
        return await _run_trace(session, agent, run_number)

    _research_tools = _research_tools + [
        RegisteredTool(
            spec=ToolSpec(
                name="read_run_trace",
                description=(
                    "Read the full step-by-step trace of one of this agent's past runs: "
                    "every tool it called, the arguments it passed, what each tool "
                    "returned, and its closing message. Use this before diagnosing why a "
                    "run behaved a certain way — do not guess from the run status alone. "
                    "Runs are numbered in the agent context, 1 being the most recent."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "run": {
                            "type": "integer",
                            "description": "Which run to read. 1 is the most recent.",
                        }
                    },
                    "required": ["run"],
                },
            ),
            handler=_trace_handler,
        )
    ]

    _research_specs = [t.spec for t in _research_tools]
    _research_handlers = {t.spec.name: t.handler for t in _research_tools}

    # Status label shown to the user while the model calls tools.
    def _status_line(tool_name: str, args: dict) -> str:
        if tool_name == "search_web":
            return f"Searching the web for \"{args.get('query', '')}\"…"
        if tool_name == "fetch_page":
            url = args.get("url", "")
            host = url.split("/")[2] if url.count("/") >= 2 else url
            return f"Reading {host}…"
        if tool_name == "read_run_trace":
            return f"Reading the trace of run {args.get('run', 1)}…"
        return f"Running {tool_name}…"

    async def event_stream():
        answer = ""
        agent_tag = f"agent={agent_id} model={model!r}"
        try:
            # Bounded tool loop: up to RESEARCH_MAX_ROUNDS rounds.  If the model
            # still wants tools after the cap, one final call without tools forces
            # a text answer.
            RESEARCH_MAX_ROUNDS = 6
            loop_msgs = list(msgs)  # shallow copy so original is unchanged

            log.info("[assist] START %s user=%r", agent_tag, body.content[:120])

            for round_num in range(RESEARCH_MAX_ROUNDS + 1):
                force_text = round_num == RESEARCH_MAX_ROUNDS
                log.info("[assist] round=%d force_text=%s %s", round_num, force_text, agent_tag)

                resp = await llm.chat(
                    loop_msgs,
                    tools=None if force_text else _research_specs,
                )

                log.info(
                    "[assist] round=%d tool_calls=%d content_len=%d %s",
                    round_num,
                    len(resp.tool_calls or []),
                    len(resp.content or ""),
                    agent_tag,
                )

                if not resp.tool_calls or force_text:
                    # Final text answer — emit as a single streamed payload.
                    answer = resp.content
                    log.info("[assist] FINAL answer_len=%d %s", len(answer), agent_tag)
                    encoded = answer.replace("\n", "\\n")
                    yield f"data: {encoded}\n\n"
                    break

                # Execute each tool call and stream a status line for each.
                loop_msgs.append(assistant_message(resp.content, resp.tool_calls))
                for tc in resp.tool_calls:
                    log.info(
                        "[assist] tool_call name=%r args=%r %s",
                        tc.name,
                        tc.arguments,
                        agent_tag,
                    )
                    status = _status_line(tc.name, tc.arguments)
                    yield f"data: [STATUS] {status}\n\n"
                    handler = _research_handlers.get(tc.name)
                    if handler is None:
                        result = f"Unknown tool: {tc.name}"
                        log.warning("[assist] unknown tool %r %s", tc.name, agent_tag)
                    else:
                        result = await handler(tc.arguments, False)
                    log.info(
                        "[assist] tool_result name=%r result_len=%d snippet=%r %s",
                        tc.name,
                        len(result),
                        result[:200],
                        agent_tag,
                    )
                    loop_msgs.append(tool_message(tc.id, tc.name, result))

        except Exception as exc:
            log.exception("[assist] ERROR %s", agent_tag)
            yield f"data: [ERROR] {exc}\n\n"
        finally:
            # Persist only the final answer text; the research trace is ephemeral.
            if answer:
                assistant_msg = AgentAssistMessage(
                    agent_id=agent.id, role="assistant", content=answer
                )
                session.add(assistant_msg)
                await session.commit()
            log.info("[assist] DONE persisted=%s %s", bool(answer), agent_tag)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{agent_id}/assist/messages", status_code=204)
async def clear_assist_thread(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Wipe the conversation thread for this agent."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    await session.exec(
        delete(AgentAssistMessage).where(AgentAssistMessage.agent_id == agent.id)
    )
    await session.commit()


# ── Knowledge ─────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/knowledge", response_model=list[KnowledgeFileOut])
async def list_knowledge_files(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(AgentKnowledgeFile)
        .where(AgentKnowledgeFile.agent_id == agent.id)
        .order_by(AgentKnowledgeFile.created_at.desc())
    )
    return list(result.all())


@router.post("/{agent_id}/knowledge", response_model=KnowledgeFileOut, status_code=201)
async def upload_knowledge_file(
    agent_id: UUID,
    file: UploadFile,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Parse the upload to text and queue it; the worker chunks and embeds it.

    Parsing happens here rather than in the worker so a corrupt or scanned PDF fails in
    the uploader's face instead of silently minutes later. Embedding uses the workspace's
    OpenAI key, so one must be connected - checked now for the same reason.
    """
    agent = await _get_owned_agent(session, current_user, agent_id)

    try:
        await knowledge.openai_key_for_org(session, agent.org_id)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    data = await file.read()
    if len(data) > knowledge.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Files are limited to {knowledge.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        text = knowledge.extract_text(file.filename or "upload", data)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = AgentKnowledgeFile(
        agent_id=agent.id,
        org_id=agent.org_id,
        filename=file.filename or "upload",
        size_bytes=len(data),
        text=text,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete("/{agent_id}/knowledge/{file_id}", status_code=204)
async def delete_knowledge_file(
    agent_id: UUID,
    file_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    row = await session.get(AgentKnowledgeFile, file_id)
    if row is None or row.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="File not found")
    # Chunks cascade from the file row.
    await session.delete(row)
    await session.commit()


# ── Tools ─────────────────────────────────────────────────────────────────────

def _agent_tool_out(
    session: AsyncSession, agent_id: UUID, agent_tool: AgentTool, connector: Connector
) -> AgentToolOut:
    """Describe one attached account and the tools it contributes.

    The tools are built rather than read from a table because their names depend on the alias,
    and the builder UI has to show the model's view of them, not an idealised one.
    """
    ctx = ToolContext(db=session, agent_id=agent_id, connector=connector, alias=agent_tool.alias)
    enabled = set(agent_tool.enabled_tools) if agent_tool.enabled_tools else None
    approval = set(agent_tool.approval_tools) if agent_tool.approval_tools else set()
    return AgentToolOut(
        id=agent_tool.id,
        connector_id=connector.id,
        connector_name=connector.name,
        connector_type=connector.type.value,
        connector_status=connector.status.value,
        alias=agent_tool.alias,
        tools=[
            ToolOut(
                name=t.spec.name,
                description=t.spec.description,
                enabled=enabled is None or t.spec.name in enabled,
                requires_approval=t.spec.name in approval,
            )
            for t in BUILDERS[connector.type](ctx)
        ],
    )


@router.get("/{agent_id}/tools", response_model=list[AgentToolOut])
async def list_agent_tools(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """The accounts this agent can act through, and what it can do with each."""
    agent = await _get_owned_agent(session, current_user, agent_id)

    rows = await session.exec(
        select(AgentTool, Connector)
        .join(Connector, Connector.id == AgentTool.connector_id)
        .where(AgentTool.agent_id == agent.id)
        .order_by(AgentTool.created_at)
    )
    return [
        _agent_tool_out(session, agent.id, agent_tool, connector)
        for agent_tool, connector in rows.all()
        if connector.type in BUILDERS
    ]


@router.post("/{agent_id}/tools", response_model=AgentToolOut, status_code=201)
async def attach_agent_tool(
    agent_id: UUID,
    body: AgentToolAttach,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Give an agent access to a connected account, or update which of its tools are on."""
    agent = await _get_owned_agent(session, current_user, agent_id)

    connector = await session.get(Connector, body.connector_id)
    if connector is None or connector.org_id != agent.org_id:
        raise HTTPException(status_code=404, detail="Connector not found")

    if connector.type not in BUILDERS:
        raise HTTPException(
            status_code=422,
            detail=f"{connector.name} does not provide tools an agent can use",
        )

    existing = (
        await session.exec(
            select(AgentTool).where(
                AgentTool.agent_id == agent.id,
                AgentTool.connector_id == connector.id,
            )
        )
    ).first()

    agent_tool = existing or AgentTool(agent_id=agent.id, connector_id=connector.id)
    agent_tool.alias = body.alias.strip()
    agent_tool.enabled_tools = body.enabled_tools
    agent_tool.approval_tools = body.approval_tools
    # First attach of an MCP server: gate write-like tools until the owner opts them on.
    if (
        existing is None
        and connector.type == ConnectorType.mcp
        and body.approval_tools is None
        and connector.config
    ):
        frozen = decrypt_json(connector.config).get("tools") or []
        agent_tool.approval_tools = mcp.write_tool_names(frozen) or None
    session.add(agent_tool)

    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent_tool)

    return _agent_tool_out(session, agent.id, agent_tool, connector)


@router.delete("/{agent_id}/tools/{connector_id}", status_code=204)
async def detach_agent_tool(
    agent_id: UUID,
    connector_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Revoke an agent's access to an account. The connector itself is untouched."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    await session.exec(
        delete(AgentTool).where(
            AgentTool.agent_id == agent.id,
            AgentTool.connector_id == connector_id,
        )
    )
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()


# ── Triggers ──────────────────────────────────────────────────────────────────

async def _trigger_out(session: AsyncSession, trigger: AgentTrigger) -> TriggerOut:
    out = TriggerOut.model_validate(trigger)
    if trigger.type == TriggerType.schedule:
        out.summary = schedule.describe(trigger.config)
    else:
        # Name the account being listened to — "When a message arrives" alone does not tell
        # the user which of their connectors this trigger is bound to.
        connector = await session.get(Connector, trigger.config.get("connector_id"))
        out.summary = (
            f"Listens on {connector.name}" if connector else "Listens on a deleted connector"
        )
    return out


async def _validated_trigger_config(
    session: AsyncSession, agent: Agent, body: TriggerUpsert
) -> dict:
    """Reject a trigger that could never fire, at save time rather than silently at run time."""
    if body.type == TriggerType.manual:
        raise HTTPException(
            status_code=422,
            detail="Manual runs need no trigger — every agent can already be run by hand.",
        )

    if body.type == TriggerType.schedule:
        try:
            return schedule.validate(body.config)
        except schedule.InvalidSchedule as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not CHANNEL_TRIGGERS_ENABLED:
        raise HTTPException(
            status_code=422,
            detail="Channel triggers are not available yet. Use a schedule instead.",
        )

    connector_id = body.config.get("connector_id")
    if not connector_id:
        raise HTTPException(
            status_code=422, detail="A channel trigger needs the account it listens to."
        )
    connector = await session.get(Connector, UUID(str(connector_id)))
    if connector is None or connector.org_id != agent.org_id:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.type not in INBOUND_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"{connector.name} cannot receive incoming messages",
        )
    return {"connector_id": str(connector.id)}


@router.get("/{agent_id}/triggers", response_model=list[TriggerOut])
async def list_agent_triggers(
    agent_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    rows = await session.exec(
        select(AgentTrigger)
        .where(AgentTrigger.agent_id == agent.id)
        .order_by(AgentTrigger.created_at)
    )
    return [await _trigger_out(session, t) for t in rows.all()]


@router.post("/{agent_id}/triggers", response_model=TriggerOut, status_code=201)
async def create_agent_trigger(
    agent_id: UUID,
    body: TriggerUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Add a schedule or a channel listener to an agent."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    config = await _validated_trigger_config(session, agent, body)

    trigger = AgentTrigger(
        agent_id=agent.id, type=body.type, config=config, enabled=body.enabled
    )
    if body.type == TriggerType.schedule and body.enabled:
        trigger.next_run_at = schedule.next_run_after(config)

    session.add(trigger)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(trigger)
    return await _trigger_out(session, trigger)


@router.patch("/{agent_id}/triggers/{trigger_id}", response_model=TriggerOut)
async def update_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    body: TriggerUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Change a trigger. Pausing clears the next run; resuming schedules it forward from now,
    so a schedule paused over a weekend does not fire twice on Monday."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    trigger = await session.get(AgentTrigger, trigger_id)
    if trigger is None or trigger.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Trigger not found")

    config = await _validated_trigger_config(session, agent, body)
    trigger.type = body.type
    trigger.config = config
    trigger.enabled = body.enabled
    trigger.next_run_at = (
        schedule.next_run_after(config)
        if body.type == TriggerType.schedule and body.enabled
        else None
    )

    session.add(trigger)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(trigger)
    return await _trigger_out(session, trigger)


@router.delete("/{agent_id}/triggers/{trigger_id}", status_code=204)
async def delete_agent_trigger(
    agent_id: UUID,
    trigger_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    trigger = await session.get(AgentTrigger, trigger_id)
    if trigger is None or trigger.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Trigger not found")

    await session.delete(trigger)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()


