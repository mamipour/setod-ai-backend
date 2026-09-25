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

import asyncio
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
    DataTableOut,
    KnowledgeFileOut,
    MemoryEntryIn,
    MemoryEntryOut,
    SessionDetailOut,
    SessionOut,
    ToolOut,
    TriggerOut,
    TriggerUpsert,
)
from app.api.auth.dependencies import assert_org_owner, get_current_user
from app.core.agents import templates
from app.core.agents.base import AgentRunError, RegisteredTool, run_agent, snapshot_config
from app.core.agents.templates import TEMPLATES
from app.core import knowledge, kv, tabular
import secrets

from app.config import settings
from app.core.crypto import decrypt_json, encrypt_json
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
    AgentDataTable,
    AgentKV,
    AgentKnowledgeFile,
    AgentLink,
    AgentProcessedItem,
    AgentPublishSnapshot,
    AgentScenario,
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

CHANNEL_TRIGGERS_ENABLED = True

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


async def _get_owner_only_agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    """Like _get_owned_agent but additionally enforces the caller is a workspace owner."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await assert_org_owner(session, user, agent.org_id)
    return agent


def _to_out(agent: Agent) -> AgentOut:
    out = AgentOut.model_validate(agent)
    # Surfaces the "you have unpublished edits" indicator in the builder header.
    out.has_unpublished_changes = bool(
        agent.published_config and agent.published_config != snapshot_config(agent)
    )
    return out


async def _with_health(session: AsyncSession, out: AgentOut) -> AgentOut:
    """Attach a health_score (0.0–1.0) based on the last 20 non-dry real runs.

    Returns the same object mutated in place for convenience.
    Best-effort — a DB error leaves health_score as None rather than failing the request.
    """
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
            out.last_run_at = records[0][1]  # most recent started_at
    except Exception:  # noqa: BLE001
        pass
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

    # Sorted by category display order, then by gallery order within a category, so the
    # Templates page and the create-flow picker agree without the browser knowing the order.
    rank = {c: i for i, c in enumerate(templates.CATEGORIES)}
    ordered = sorted(
        enumerate(TEMPLATES),
        key=lambda p: (rank.get(p[1].category, len(rank)), p[1].category, p[0]),
    )
    out = []
    for _, template in ordered:
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

    # Copilot tokens are on AgentAssistMessage, not AgentSession — add them separately.
    copilot_today = await session.exec(
        select(
            func.coalesce(
                func.sum(AgentAssistMessage.prompt_tokens + AgentAssistMessage.completion_tokens), 0
            )
        )
        .join(Agent, AgentAssistMessage.agent_id == Agent.id)
        .where(
            Agent.org_id == org_id,
            AgentAssistMessage.role == "assistant",
            AgentAssistMessage.created_at >= midnight,
        )
    )
    tokens_today += copilot_today.one()

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

    copilot_yesterday = await session.exec(
        select(
            func.coalesce(
                func.sum(AgentAssistMessage.prompt_tokens + AgentAssistMessage.completion_tokens), 0
            )
        )
        .join(Agent, AgentAssistMessage.agent_id == Agent.id)
        .where(
            Agent.org_id == org_id,
            AgentAssistMessage.role == "assistant",
            AgentAssistMessage.created_at >= yesterday_midnight,
            AgentAssistMessage.created_at < midnight,
        )
    )
    tokens_yesterday += copilot_yesterday.one()

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
    agents = [_to_out(a) for a in result.all()]
    return [await _with_health(session, a) for a in agents]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    return await _with_health(session, _to_out(await _get_owned_agent(session, current_user, agent_id)))


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
    agent = await _get_owner_only_agent(session, current_user, agent_id)

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
    agent = await _get_owner_only_agent(session, current_user, agent_id)
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
    agent = await _get_owner_only_agent(session, current_user, agent_id)
    agent.published_config = None
    agent.status = AgentStatus.draft
    agent.published_at = None
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


@router.post("/{agent_id}/pause", response_model=AgentOut)
async def pause_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Pause a live agent.

    Sets status to *paused* and keeps `published_config` intact so the agent can be
    resumed without re-publishing. The worker skips all scheduled runs and inbound
    events for paused agents.
    """
    agent = await _get_owner_only_agent(session, current_user, agent_id)
    if agent.status != AgentStatus.published:
        raise HTTPException(status_code=422, detail="Only a live (published) agent can be paused")
    agent.status = AgentStatus.paused
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return _to_out(agent)


@router.post("/{agent_id}/resume", response_model=AgentOut)
async def resume_agent(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Resume a paused agent.

    Sets status back to *published*. The existing `published_config` snapshot is used
    as-is — no new snapshot is created and no re-publish is required.
    """
    agent = await _get_owner_only_agent(session, current_user, agent_id)
    if agent.status != AgentStatus.paused:
        raise HTTPException(status_code=422, detail="Only a paused agent can be resumed")
    if not agent.published_config:
        # Safety net: if the config somehow got cleared, fall back to draft.
        agent.status = AgentStatus.draft
    else:
        agent.status = AgentStatus.published
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
    agent = await _get_owner_only_agent(session, current_user, agent_id)
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
- If a skill in the org library covers what the user is describing but is not attached to this agent, point them to it: "There's a Silence when idle skill in your library — attach it from the Agent tab → Skills instead of writing that rule into the prompt"

## The setod ecosystem — what you know

Agents on setod:
- Have a single system prompt (the "instructions") that governs all their behaviour
- Run on a cron schedule (every N minutes/hours, or at a specific time)
- Are powered by OpenAI or Anthropic models — the user picks one
- Must be published before they go live; drafts are safe to experiment with

Connectors available, and what the user needs to connect each one:
- Google (Gmail + Calendar) — their Google address plus a Google App Password. Not OAuth.
- Telegram Bot — a bot token and an admin chat ID
- Telegram Client — their own Telegram account, authorised by phone number
- Twilio — an account SID, auth token, and a provisioned phone number
- MCP — remote HTTPS tool servers. Catalog cards (GitHub, Linear, Notion, Slack, Atlassian, Zapier) or a custom URL. Auth is probed: OAuth, a pasted bearer token, or none. Tools are whatever the user attached from that server; never invent MCP tool names.
- Other agents — call a published agent as a tool (call_X) and get its final answer back. The target runs with its own accounts and approval rules. Depth is capped at 1 — a called agent cannot itself call agents.

## What each tool actually returns — read this before writing any prompt

### Cross-run deduplication — what is and isn't covered

**Covered automatically:** dedicated inbox reading tools (`read_unread_emails`, `read_telegram_messages`). These tools track what has already been seen on every call — the agent never needs to mention deduplication in the prompt for inbox use cases. If a run crashes before acting, those items come back next run rather than being lost. Write actions like reply and archive lock their item permanently the moment they fire.

**NOT covered automatically:** fetching web pages, CSV files, RSS feeds, or web search results. For these, the platform has no way to know which items the agent already acted on. The "remember past runs" toggle (under the agent's Settings tab) handles this transparently — when enabled, the platform gives the agent a running memory it updates each run. You do not need to explain memory mechanics in the prompt; just write the intent in plain English ("don't notify me about the same tender twice").

Never write memory syntax, tool call counts, or platform mechanics into the prompt — those are implementation details the platform and the model handle internally.

### Telegram Client
- **`read_telegram_messages`**: Returns the most recent unread message for each chat/group that has unread messages, up to a limit (default 10, max 25). Returns **one message per chat** — not the full chat history. Automatically skips chats whose last message was already seen in a previous run. Returns "No new unread Telegram messages since the last run." when nothing is new. The unread flag is always available — never write fallback logic for when it is missing.
- **`send_telegram_message`**: Sends a message from the user's account to any @username, phone number, or chat ID.
- **No mark-as-read tool exists.** There is no way to mark Telegram messages as read. Do not suggest it.
- **No search tool exists.** You cannot search Telegram history or filter by keyword at fetch time.

### Telegram Bot
- **`send_telegram_message`**: Sends a message to the single admin chat configured in the connector. One direction only — it cannot read anything.

### Google Gmail
- **`read_unread_emails`**: Returns unread inbox emails (newest first), each with id, sender, subject, and first 300 characters of body. Default 10, max 25. Skips emails already handled in a previous run. Returns "No new unread email." when nothing is new.
- **`search_emails`**: Searches with Gmail-style syntax: `from:`, `subject:`, `after:`, `before:`, `is:unread`, `is:read`, etc. Returns up to 25 results. **Does NOT use the deduplication tracker** — always returns whatever matches the query regardless of prior runs.
- **`send_email`**: Sends a plain-text email. `to` and `body` are required, `subject` is optional.
- **`reply_to_email`**: Replies to an existing email (takes `message_id` from `read_unread_emails`). Immediately marks the email permanently processed — a second run cannot reply to the same email again.
- **`archive_email`**: Moves an email out of the inbox (not deleted). Immediately marks permanently processed.

### Google Calendar
- **`list_calendar_events`**: Lists events on the user's primary calendar between two dates (YYYY-MM-DD). Defaults to today. Returns title, id, start, end, location, and attendees. No deduplication — it's a query, not an inbox.
- **`create_calendar_event`**: Creates an event with title, start, end (YYYY-MM-DD or YYYY-MM-DDTHH:MM). Optional: description, location, attendees (comma-separated emails — each receives a Google invite), timezone (e.g. America/Toronto).

### Twilio
- **`send_sms`**: Sends SMS from the connector's fixed phone number. `to` must be E.164 format (e.g. +15551234567). Messages over 160 characters are split into multiple SMS segments and charged per segment — keep bodies under 320 characters when possible. No deduplication — each call sends a new SMS.

### Tool behaviour rules every prompt must respect
- Agents can only use tools from connectors the user has attached
- Reading tools (Gmail read, Telegram read) are safe to call freely; they automatically skip items already seen in past runs
- Writing tools (send email, send SMS, send Telegram) should fire once per run unless the prompt explicitly allows more — write this in plain English ("send one notification per run"), never reference tool call counts
- There is no file system and no code execution. Web search is a settings toggle. MCP tools only exist if the user attached an MCP connector.
- **`query_data`** exists only when the agent has a CSV or Excel file in its Knowledge tab. It runs one read-only SQL statement (DuckDB dialect) over those files as tables and returns up to 200 rows. The agent already sees every table's columns, types and a sample row in the tool description — prompts should say *what* to find ("tenders closing in the next 14 days in the IT category"), never write SQL or column names. Suggest it whenever a prompt would otherwise ask the agent to "read the file" or "go through all rows".
- Cross-run memory for web/CSV monitoring is handled by the platform's "remember past runs" setting — do not write memory syntax or identifier tracking into the prompt; write the intent instead ("don't notify about the same item twice")

Human approval:
- Individual tools can be flagged "requires approval" — the agent will pause and wait before executing them
- Useful for any action that is irreversible or external-facing

Skills:
- Skills are reusable prompt fragments stored in the org's Skills library
- Each skill covers one concern: a behaviour rule, output format, or safety guardrail
- Skills are attached per-agent from the Agent tab → Skills section
- When attached, a skill's content is injected into the agent's system prompt automatically — the user does not need to copy its text into the instructions
- Default skills available in every org: Silence when idle, No duplicate actions, One action per run, Urgency first, After-hours notifications only, Escalate when unsure, Professional tone, Concise run summary, No PII in summaries, Stop gracefully at budget, Lead qualification
- "No duplicate actions" covers in-run safety (prevents the agent calling the same write tool twice within a single run). It does NOT handle cross-run deduplication of fetched web/CSV content — for that, the user must enable "Remember past runs" in the agent's Settings tab.
- Users can edit any skill or create their own from the Skills page (sidebar → Skills)
- When writing a prompt, you should NOT duplicate behaviour that a skill already handles — instead tell the user to attach the relevant skill

## Research tools (you can use these yourself)

You have three tools available in this conversation:

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
2. **Look for a better data source.** Check the page for links labelled "download", "API", "CSV", "RSS", "open data", or "dataset". Also search for `"[site domain]" API OR RSS OR "open data"` to find official feeds. Prefer sources in this order: API > CSV/dataset > RSS > filtered HTML listing > unfiltered pages. A structured source is often 10–20x cheaper per run than page-by-page browsing, and far more reliable.
3. **Verify the best endpoint** by fetching it. Confirm you get readable, structured content.
4. **Write instructions that embed the verified endpoint** — the exact URL the agent should fetch — plus extraction hints (column names, keywords to filter on, what a match looks like). Do not leave URL discovery to the agent at runtime.
   - **For date-ordered results, never use web search.** Web search (`search_web`) returns results sorted by relevance, not by date. An agent told to "stop when you see items older than 7 days" will not work reliably with search results — it may never find recent items or may report old ones as new. Instead, use `fetch_page` on a listing URL that has an explicit newest-first sort (look for query parameters like `sort=date`, `order=newest`, `sort=desc`). Write that sorted URL directly into the instructions.
   - **Always tell the agent to avoid acting on the same item twice.** Write this in plain English: "skip any items you have already notified about", "only report each opportunity once". Tell the user to enable "Remember past runs" in the agent's Settings tab — the platform handles the memory mechanics automatically. Do not write memory syntax, identifier list formats, or implementation details into the prompt.
   - **If only HTML listings exist**, write instructions that use the site's own filters and sorting (query parameters for category, status, newest-first) so one page carries the most relevant rows. Give the agent a stop rule in plain English: "stop as soon as you reach items older than N days." Fetched pages may end with a "[truncated — showing X of Y lines]" note; that means the page continued, so narrow the filters or follow the pagination link.
5. **Check the agent's web settings** (shown in `<agent_context>`). If web search or live page access is off, tell the user: "Go to this agent's Settings tab and enable Web search and Live page access — the agent needs those to fetch URLs during its runs."
6. **If the page returns almost no text** (likely a JavaScript-rendered SPA), say so honestly: "This page appears to need a browser to load — the agent's built-in fetch tool will not see content here. Look for an RSS feed, API, or data download on the site instead."

## When a goal needs a connector the agent does not have

The context block shows which connectors are attached. If the goal needs one that exists on setod but is not attached:
1. Name the connector and what it does
2. Give the exact path: "Go to Connectors → connect [X] → then come back to this agent's Agent tab → add it under Tools"
3. Offer to pre-write the prompt now so it is ready when they connect it

Slack, Notion, GitHub, Linear, Atlassian and Zapier are reachable only through MCP, and only once the user has attached an MCP connector for that service.

## When the goal is genuinely not possible

Things setod cannot do at all: WhatsApp, running code, a file system. For these, respond:
"That's not something setod supports yet. If it's important for your workflow, send a feature request to support — the team reviews them and prioritises based on demand. In the meantime, here's the closest thing you can do with what's available: [suggest an alternative if one exists]"

## Your job

When the user describes what they want their agent to do:
1. If the goal is genuinely ambiguous, ask ONE clarifying question and stop — do not also write a prompt in the same reply. Wait for the answer.
2. If you can make a reasonable assumption, state it and write the prompt — do not ask a question.
3. Present the prompt in a fenced code block; the chat renders a Copy button on it.

When the user shares an existing prompt and asks for improvements:
1. Identify the specific problem (vague trigger, no silence rule, missing tool constraint, etc.)
2. Rewrite the prompt with the fix applied
3. Explain in one sentence what changed and why

When the user shares run logs and asks why something went wrong:
1. Read the logs carefully
2. Identify the root cause
3. Suggest a specific prompt change that prevents it — quote the exact line to add or change

## Output rules

**Code block purity — this is strict**
- Put the ready-to-use prompt text inside the fenced code block and NOTHING ELSE.
- Do NOT put notes, caveats, admin comments, skill suggestions, follow-up questions, or "Notes for admin" sections inside the code block. A user will copy that block verbatim into their agent. Anything that should not be in the agent's instructions must go OUTSIDE the code block, after it.

**You cannot modify the agent**
- You have no ability to apply, save, or publish anything. You are a read-only advisor.
- Never say "apply this prompt", "I'll apply it", "want me to apply?", or any phrase implying you can make changes. The user copies your suggestion and pastes it themselves.
- Never offer numbered choices like "(1) apply as-is, (2) apply a variant" — you cannot apply either.

**Connector and tool honesty**
- Before suggesting any integration, check the agent's attached tools (shown in `<agent_context>`). If it's not attached, check whether it exists in the connector list above.
- Never suggest connecting a service that is not in the connector list above. There is no Google Sheets connector, no Notion connector, no Airtable connector, no database connector. MCP is the only path to non-listed services, and only if the user has already attached an MCP connector.
- If you want to suggest a follow-on capability that would require a connector the user does not have, say exactly: "This would need [connector name] — that connector doesn't exist on setod yet. You could request it at support."

**Skills vs prompt rules — no double-enforcement**
- If you write a behaviour rule into the prompt (e.g. "do not include PII"), do NOT also suggest attaching the skill that covers the same thing — that creates double enforcement once the skill is attached.
- Instead, if a skill covers what you just wrote, tell the user: "This rule is already in the prompt above. If you prefer to manage it as a skill, remove that line and attach [skill name] from the Agent tab → Skills."
- The reverse is also true: if a skill is already attached that covers a behaviour, do not write that behaviour into the prompt.

**No regex or code in prompts**
- Write matching rules in plain English, not regex syntax. The agent reads the prompt as natural language instructions; regex notation like `(need|want) .* (cater.*)` is not executed — it adds noise and can confuse the model.
- Good: "Look for messages containing an intent word (need, looking for, hire) combined with a catering word (catering, caterer, food service)."
- Bad: `(need|looking for|hire) .* (cater|catering|caterer|food service)`

**Other rules**
- Never invent connector types, tool names, or platform features not listed above
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

    # Resolve the effective model label from the connector, since agent.model is often null
    # while model_connector_id points to the real provider. Show the connector name so the
    # copilot knows the agent is properly configured.
    model_label = agent.model or ""
    if not model_label and agent.model_connector_id:
        mc = await session.get(Connector, agent.model_connector_id)
        if mc:
            model_label = f"{mc.name} ({mc.type.value})"
    model_label = model_label or "not set — agent cannot run until a model connector is chosen"

    return (
        f"<agent_context>\n"
        f"Agent: {agent.name}\n"
        f"Model: {model_label}\n"
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
        total_prompt_tokens = 0
        total_completion_tokens = 0
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
                total_prompt_tokens += resp.prompt_tokens
                total_completion_tokens += resp.completion_tokens

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
            # Persist the final answer text and the accumulated token counts.
            if answer:
                assistant_msg = AgentAssistMessage(
                    agent_id=agent.id,
                    role="assistant",
                    content=answer,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
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
    files = list(result.all())
    tables = await _tables_by_file(session, agent.id)
    return [_knowledge_out(f, tables.get(f.id, [])) for f in files]


async def _tables_by_file(session: AsyncSession, agent_id: UUID) -> dict[UUID, list[AgentDataTable]]:
    """Table metadata per file, without the Parquet column — that is bytes the UI never needs."""
    rows = await session.exec(
        select(
            AgentDataTable.file_id, AgentDataTable.name, AgentDataTable.sheet,
            AgentDataTable.row_count, AgentDataTable.columns,
        )
        .where(AgentDataTable.agent_id == agent_id)
        .order_by(AgentDataTable.created_at)
    )
    out: dict[UUID, list] = {}
    for file_id, name, sheet, row_count, columns in rows.all():
        out.setdefault(file_id, []).append(
            {"name": name, "sheet": sheet, "row_count": row_count, "column_count": len(columns)}
        )
    return out


def _knowledge_out(file: AgentKnowledgeFile, tables: list[dict]) -> KnowledgeFileOut:
    out = KnowledgeFileOut.model_validate(file)
    out.tables = [DataTableOut(**t) for t in tables]
    return out


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

    filename = file.filename or "upload"
    lower = filename.lower()

    # Tabular files additionally become SQL tables for `query_data`. For XLSX this is the
    # only parser, so its failure is the upload's failure; for CSV the text path stands on
    # its own and a table that cannot be inferred is logged and skipped, not fatal.
    drafts: list[tabular.TableDraft] = []
    if lower.endswith(tabular.TABULAR_EXTENSIONS):
        taken = {t.name for t in await tabular.tables_for_agent(session, agent.id)}
        try:
            drafts = await asyncio.to_thread(tabular.ingest, filename, data, taken=taken)
        except tabular.TabularError as exc:
            if lower.endswith(".xlsx"):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            log.info("knowledge: %s not queryable: %s", filename, exc)

    if lower.endswith(".xlsx"):
        text = "\n\n".join(d.text for d in drafts)
        if len(text) > knowledge.MAX_TEXT_CHARS:
            text = text[: knowledge.MAX_TEXT_CHARS]
    else:
        try:
            text = knowledge.extract_text(filename, data)
        except knowledge.KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = AgentKnowledgeFile(
        agent_id=agent.id,
        org_id=agent.org_id,
        filename=filename,
        size_bytes=len(data),
        text=text,
    )
    session.add(row)
    await session.flush()
    for d in drafts:
        session.add(
            AgentDataTable(
                file_id=row.id, agent_id=agent.id, org_id=agent.org_id,
                name=d.name, sheet=d.sheet, row_count=d.row_count,
                columns=d.columns, sample=d.sample, parquet=d.parquet,
            )
        )
    await session.commit()
    await session.refresh(row)
    return _knowledge_out(
        row,
        [{"name": d.name, "sheet": d.sheet, "row_count": d.row_count, "column_count": len(d.columns)} for d in drafts],
    )


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


class KnowledgeUrlBody(BaseModel):
    url: str


@router.post("/{agent_id}/knowledge/url", response_model=KnowledgeFileOut, status_code=201)
async def add_knowledge_url(
    agent_id: UUID,
    body: KnowledgeUrlBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Fetch a URL and add its text to the agent's knowledge base.

    Works identically to a file upload: the text is extracted here, then the worker
    chunks and embeds it asynchronously.
    """
    agent = await _get_owned_agent(session, current_user, agent_id)

    try:
        await knowledge.openai_key_for_org(session, agent.org_id)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Please enter a full URL starting with https://")

    try:
        display, text = await knowledge.fetch_url_text(url)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if len(text) > knowledge.MAX_TEXT_CHARS:
        text = text[: knowledge.MAX_TEXT_CHARS]

    row = AgentKnowledgeFile(
        agent_id=agent.id,
        org_id=agent.org_id,
        filename=display,
        size_bytes=len(text.encode()),
        text=text,
        source_url=url,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ── Key-value memory ───────────────────────────────────────────────────────────
#
# The owner's window onto the agent's exact state. Keys carry the `shared:` prefix in the
# URL and the payload exactly as the model uses them; `kv.resolve` maps them to a scope.


def _memory_out(row: AgentKV) -> MemoryEntryOut:
    shared = row.agent_id is None
    return MemoryEntryOut(
        key=f"{kv.SHARED_PREFIX}{row.key}" if shared else row.key,
        shared=shared,
        value=row.value,
        updated_at=row.updated_at,
        updated_by_session_id=row.updated_by_session_id,
    )


@router.get("/{agent_id}/memory", response_model=list[MemoryEntryOut])
async def list_memory(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Every key this agent can see: its own, then the workspace's `shared:` keys."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    rows = await kv.list_entries(session, agent.org_id, agent.id)
    return [_memory_out(r) for r in rows]


@router.put("/{agent_id}/memory/{key}", response_model=MemoryEntryOut)
async def put_memory(
    agent_id: UUID,
    key: str,
    body: MemoryEntryIn,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Create or overwrite one entry. Same limits as the agent's `memory_set`."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    try:
        scope = kv.resolve(agent.org_id, agent.id, key)
        row = await kv.set_entry(session, scope, body.value, session_id=None)
    except kv.KVError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _memory_out(row)


@router.delete("/{agent_id}/memory/{key}", status_code=204)
async def delete_memory(
    agent_id: UUID,
    key: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    try:
        scope = kv.resolve(agent.org_id, agent.id, key)
    except kv.KVError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not await kv.delete_entry(session, scope):
        raise HTTPException(status_code=404, detail="Key not found")


@router.delete("/{agent_id}/memory")
async def clear_memory(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Delete all of this agent's private keys. Shared keys are untouched — other agents may
    rely on them; remove those one at a time."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    deleted = await kv.clear_private(session, agent.org_id, agent.id)
    return {"deleted": deleted}


# ── Session explain ────────────────────────────────────────────────────────────

class ExplainOut(BaseModel):
    session_id: UUID
    summary: str


@router.post("/{agent_id}/sessions/{session_id}/explain", response_model=ExplainOut)
async def explain_session(
    agent_id: UUID,
    session_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Generate a plain-English summary of what the agent did in this run.

    Calls the agent's own AI model (or the workspace's first active model) with
    a lightweight summarisation prompt over the session messages.
    """
    agent = await _get_owned_agent(session, current_user, agent_id)
    run = await session.get(AgentSession, session_id)
    if run is None or run.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Session not found")

    msgs_result = await session.exec(
        select(AgentSessionMessage)
        .where(AgentSessionMessage.session_id == run.id)
        .order_by(AgentSessionMessage.sequence)
    )
    messages = msgs_result.all()
    if not messages:
        return ExplainOut(session_id=run.id, summary="This run produced no messages to summarise.")

    # Build a compact transcript — skip raw tool result blobs, keep assistant + tool names.
    lines: list[str] = []
    for m in messages:
        if m.role == MessageRole.user:
            lines.append(f"Trigger: {m.content[:400]}")
        elif m.role == MessageRole.assistant:
            if m.tool_name:
                args_preview = json.dumps(m.tool_args or {})[:200] if m.tool_args else ""
                lines.append(f"Called tool: {m.tool_name}({args_preview})")
            elif m.content:
                lines.append(f"Agent: {m.content[:400]}")
        elif m.role == MessageRole.tool and m.content:
            lines.append(f"Tool result: {m.content[:300]}")

    transcript = "\n".join(lines)

    prompt = (
        "You are summarising an AI agent run for its owner — a non-technical small business person.\n"
        "Write 2–4 plain English sentences explaining what the agent did and what outcome it achieved. "
        "Focus on what happened in the real world (emails sent, rows read, messages posted), not on "
        "technical steps. If something went wrong, say so clearly. Do not use jargon.\n\n"
        f"TRANSCRIPT:\n{transcript}\n\nSUMMARY:"
    )

    # Use the agent's own model connector; fall back to first active OpenAI/Anthropic in the org.
    from app.core.agents.base import _resolve_config, _build_client_for
    config = _resolve_config(agent, use_published=False)
    if not config.get("model_connector_id"):
        # Find any active model connector in the org.
        mc_result = await session.exec(
            select(Connector).where(
                Connector.org_id == agent.org_id,
                Connector.type.in_([ConnectorType.openai, ConnectorType.anthropic]),
                Connector.status == ConnectorStatus.active,
            ).limit(1)
        )
        mc = mc_result.first()
        if mc is None:
            return ExplainOut(session_id=run.id, summary="No AI model connector available to generate a summary.")
        config["model_connector_id"] = str(mc.id)
        config["model"] = config.get("model") or ""

    try:
        client = await _build_client_for(session, agent, config)
        from app.core.llm.client import user_message as _user_msg
        response = await client.chat([_user_msg(prompt)], max_tokens=256)
        summary = response.content.strip()
    except Exception as exc:  # noqa: BLE001
        summary = f"Could not generate summary: {exc}"

    return ExplainOut(session_id=run.id, summary=summary)


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


async def _auto_register_telegram_webhook(session: AsyncSession, connector: Connector) -> None:
    """Register (or re-register) a Telegram bot webhook if PUBLIC_BASE_URL is configured.

    Safe to call multiple times — Telegram simply updates the registered URL. Failures are
    logged and swallowed so a network hiccup never blocks the trigger from being saved.
    """
    if not settings.public_base_url:
        log.info("PUBLIC_BASE_URL not set — skipping Telegram webhook registration for %s", connector.id)
        return
    try:
        config = decrypt_json(connector.config)
        webhook_secret = config.get("webhook_secret") or secrets.token_urlsafe(32)
        url = f"{settings.public_base_url.rstrip('/')}/webhooks/telegram/{connector.id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{config['bot_token']}/setWebhook",
                json={
                    "url": url,
                    "secret_token": webhook_secret,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": True,
                },
            )
        data = resp.json()
        if data.get("ok"):
            log.info("Telegram webhook registered for connector %s → %s", connector.id, url)
            config["webhook_secret"] = webhook_secret
            connector.config = encrypt_json(config)
            session.add(connector)
        else:
            log.warning("Telegram webhook registration failed for %s: %s", connector.id, data)
    except Exception:
        log.exception("Unexpected error registering Telegram webhook for connector %s", connector.id)


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

    # For channel triggers on a Telegram bot connector, ensure the webhook is registered so
    # Telegram knows where to deliver messages. This is idempotent — safe to call every time.
    if body.type == TriggerType.channel:
        connector = await session.get(Connector, UUID(str(config["connector_id"])))
        if connector and connector.type == ConnectorType.telegram_bot:
            await _auto_register_telegram_webhook(session, connector)

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



# ── Scenarios ─────────────────────────────────────────────────────────────────

class ScenarioBody(BaseModel):
    name: str
    input_text: str
    expected_tools: list[str] = []


class ScenarioOut(BaseModel):
    id: UUID
    agent_id: UUID
    name: str
    input_text: str
    expected_tools: list[str]
    last_session_id: UUID | None
    last_ran_at: datetime | None
    created_at: datetime


class ScenarioRunOut(BaseModel):
    scenario_id: UUID
    session: SessionOut


@router.get("/{agent_id}/scenarios", response_model=list[ScenarioOut])
async def list_scenarios(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    rows = await session.exec(
        select(AgentScenario)
        .where(AgentScenario.agent_id == agent.id)
        .order_by(AgentScenario.created_at)
    )
    return [ScenarioOut(**r.model_dump()) for r in rows.all()]


@router.post("/{agent_id}/scenarios", response_model=ScenarioOut, status_code=201)
async def create_scenario(
    agent_id: UUID,
    body: ScenarioBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    sc = AgentScenario(
        agent_id=agent.id,
        name=body.name.strip(),
        input_text=body.input_text.strip(),
        expected_tools=body.expected_tools,
    )
    session.add(sc)
    await session.commit()
    await session.refresh(sc)
    return ScenarioOut(**sc.model_dump())


@router.patch("/{agent_id}/scenarios/{scenario_id}", response_model=ScenarioOut)
async def update_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    body: ScenarioBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    sc = await session.get(AgentScenario, scenario_id)
    if sc is None or sc.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Scenario not found")
    sc.name = body.name.strip()
    sc.input_text = body.input_text.strip()
    sc.expected_tools = body.expected_tools
    session.add(sc)
    await session.commit()
    await session.refresh(sc)
    return ScenarioOut(**sc.model_dump())


@router.delete("/{agent_id}/scenarios/{scenario_id}", status_code=204)
async def delete_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    agent = await _get_owned_agent(session, current_user, agent_id)
    sc = await session.get(AgentScenario, scenario_id)
    if sc is None or sc.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await session.delete(sc)
    await session.commit()


@router.post("/{agent_id}/scenarios/{scenario_id}/run", response_model=ScenarioRunOut)
async def run_scenario(
    agent_id: UUID,
    scenario_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Dry-run the agent with the scenario's input_text and return the session."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    sc = await session.get(AgentScenario, scenario_id)
    if sc is None or sc.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Scenario not found")

    try:
        result = await run_agent(
            session,
            agent,
            trigger_type=TriggerType.manual,
            user_input=sc.input_text,
            dry_run=True,
            use_published=False,  # always test the draft
        )
    except AgentRunError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sc.last_session_id = result.id
    sc.last_ran_at = datetime.now(UTC)
    session.add(sc)
    await session.commit()

    return ScenarioRunOut(scenario_id=sc.id, session=_session_out(result))
