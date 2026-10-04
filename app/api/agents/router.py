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

# ---------------------------------------------------------------------------
# Token-level cost table (input_price, output_price) in USD per 1 million tokens.
# Kept in sync with platform-ui/src/components/agents/shared.tsx MODEL_PRICING.
# ---------------------------------------------------------------------------
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5":            (10.00, 40.00),
    "gpt-5-mini":       ( 0.25,  2.00),
    "gpt-5.4":          ( 2.50, 10.00),
    "gpt-5.4-mini":     ( 0.75,  4.50),
    "gpt-4o":           ( 2.50, 10.00),
    "gpt-4o-mini":      ( 0.15,  0.60),
    "gpt-4-turbo":      (10.00, 30.00),
    "gpt-4":            (30.00, 60.00),
    "gpt-3.5-turbo":    ( 0.50,  1.50),
    "o1":               (15.00, 60.00),
    "o1-mini":          ( 3.00, 12.00),
    "o3-mini":          ( 1.10,  4.40),
    # Anthropic
    "claude-opus-4-5":  (15.00, 75.00),
    "claude-sonnet-4-5":( 3.00, 15.00),
    "claude-haiku-3-5": ( 0.80,  4.00),
    "claude-opus-4":    (15.00, 75.00),
    "claude-sonnet-4":  ( 3.00, 15.00),
    "claude-haiku-3":   ( 0.25,  1.25),
}
_DEFAULT_PRICE: tuple[float, float] = (0.75, 4.50)  # gpt-5.4-mini platform default


def _price_for(model_slug: str) -> tuple[float, float]:
    if not model_slug:
        return _DEFAULT_PRICE
    # Sort longest key first so "gpt-5.4-mini" beats "gpt-5" on prefix matches.
    for key, price in sorted(_MODEL_PRICING.items(), key=lambda x: -len(x[0])):
        if model_slug == key or model_slug.startswith(key):
            return price
    return _DEFAULT_PRICE


def _compute_spend(rows: list[tuple[str, int, int]]) -> float:
    """Sum spend in USD from a list of (model_slug, prompt_tokens, completion_tokens)."""
    total = 0.0
    for slug, prompt, completion in rows:
        inp, out = _price_for(slug)
        total += (prompt / 1_000_000) * inp + (completion / 1_000_000) * out
    return round(total, 6)
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
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
from app.api.auth.dependencies import assert_org_member, assert_org_owner, get_current_user
from app.limiter import limiter
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

# Import and include sub-routers (R2 refactor).
# These must be included AFTER the static routes above (models, templates…)
# so FastAPI's declaration-order matching works correctly.
from app.api.agents._assist import assist_router  # noqa: E402
from app.api.agents._triggers import triggers_router  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

# assert_org_member is imported from app.api.auth.dependencies (shared R4 refactor)


async def _get_owned_agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await assert_org_member(session, user, agent.org_id)
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
    """Attach run health, primary trigger type, and connected connector types.

    Returns the same object mutated in place for convenience.
    Best-effort — a DB error leaves the extra fields at their defaults rather than
    failing the request.
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

    try:
        # Primary trigger: first enabled trigger by creation order.
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
        # Connector types attached as tools, excluding LLM-provider connectors (those are
        # the AI brain, not integration services, and are already shown via agent.model).
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


@router.get("/platform-models")
async def list_platform_models(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Models available via Setod-managed keys (requires managed_models entitlement).

    The list is driven by ``model_prices``: a managed model is only offered if we
    have an active price row for it, because an unpriced model would run for free
    against the credit ledger.  Add a row in the admin to expose a new model.
    """
    from app.core.billing.entitlements import resolve as resolve_ent
    await assert_org_member(session, current_user, org_id)
    ent = await resolve_ent(session, org_id)
    if not ent.allows("managed_models"):
        return {"models": [], "available": False}

    from app.config import settings
    providers: list[str] = []
    if settings.platform_openai_api_key:
        providers.append("openai")
    if settings.platform_anthropic_api_key:
        providers.append("anthropic")
    if not providers:
        return {"models": [], "available": True}

    from app.core.billing.usage import default_managed_model

    priced = await _priced_models(session, providers)
    # Per-provider default (recommended model if priced, else cheapest priced).
    defaults = {p: await default_managed_model(session, p) for p in providers}

    # Build list with the default model pinned first per provider, rest by price desc.
    def _sort_key(item: tuple[str, str]) -> tuple[int, int]:
        provider, slug = item
        is_default = 0 if slug == defaults.get(provider) else 1
        rank = next((i for i, (p, s) in enumerate(priced) if p == provider and s == slug), 999)
        return (is_default, rank)

    sorted_priced = sorted(priced, key=_sort_key)
    models = [
        {"id": slug, "label": _model_label(slug), "provider": provider}
        for provider, slug in sorted_priced
    ]
    return {"models": models, "available": True, "providers": providers, "defaults": defaults}


async def _priced_models(session: AsyncSession, providers: list[str]) -> list[tuple[str, str]]:
    """Return (provider, model_slug) pairs that have an active price row, ordered by cost desc."""
    from app.db.models import ModelPrice
    rows = await session.exec(
        select(ModelPrice.provider, ModelPrice.model_slug, ModelPrice.input_per_m)
        .where(ModelPrice.provider.in_(providers), ModelPrice.active == True)  # noqa: E712
        .order_by(ModelPrice.provider, ModelPrice.input_per_m.desc())
    )
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for provider, slug, _ in rows.all():
        key = (provider, slug)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _model_label(slug: str) -> str:
    """Human label for a model slug, e.g. 'claude-sonnet-4-5' → 'Claude Sonnet 4.5'."""
    if slug.startswith("claude-"):
        parts = slug.split("-")[1:]
        words, version = [], []
        for p in parts:
            (version if p.isdigit() else words).append(p)
        return "Claude " + " ".join(w.capitalize() for w in words) + (" " + ".".join(version) if version else "")
    if slug.startswith("gpt-"):
        return "GPT-" + slug[4:].replace("-mini", " mini").replace("-nano", " nano")
    return slug


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
    await assert_org_member(session, current_user, connector.org_id)
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
        models = [{"id": m["id"], "label": _model_label(m["id"])} for m in chat]
    else:
        models = [{"id": m["id"], "label": m.get("display_name") or _model_label(m["id"])} for m in data]

    # Only offer models we have a price row for.  The provider's live list confirms the
    # model still exists on the user's key; the price table confirms we can account for it.
    # Sort by price descending with the default model pinned first — same ordering as the
    # managed model list — so both pickers show models in the same sequence.
    priced = await _priced_models(session, [connector.type.value])
    default_slug = DEFAULT_MODELS.get(connector.type.value, "")
    price_rank: dict[str, int] = {slug: i for i, (_, slug) in enumerate(priced)}
    models = [m for m in models if m["id"] in price_rank]
    models.sort(key=lambda m: (0 if m["id"] == default_slug else 1, price_rank[m["id"]]))

    return {"models": models, "default": default_slug}


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
    await assert_org_member(session, current_user, org_id)

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
    await assert_org_member(session, current_user, org_id)

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

    # Per-model spend for today (accurate pricing, not a single blended rate).
    spend_today_rows = await session.exec(
        select(
            AgentSession.model_slug,
            func.coalesce(func.sum(AgentSession.prompt_tokens), 0),
            func.coalesce(func.sum(AgentSession.completion_tokens), 0),
        )
        .where(
            AgentSession.org_id == org_id,
            AgentSession.started_at >= midnight,
        )
        .group_by(AgentSession.model_slug)
    )
    spend_today = _compute_spend(list(spend_today_rows.all()))

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

    # Per-model spend for yesterday.
    spend_yesterday_rows = await session.exec(
        select(
            AgentSession.model_slug,
            func.coalesce(func.sum(AgentSession.prompt_tokens), 0),
            func.coalesce(func.sum(AgentSession.completion_tokens), 0),
        )
        .where(
            AgentSession.org_id == org_id,
            AgentSession.started_at >= yesterday_midnight,
            AgentSession.started_at < midnight,
        )
        .group_by(AgentSession.model_slug)
    )
    spend_yesterday = _compute_spend(list(spend_yesterday_rows.all()))

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
        "spend_today": spend_today,
        "runs_yesterday": runs_yesterday,
        "failures_yesterday": failures_yesterday,
        "tokens_yesterday": tokens_yesterday,
        "spend_yesterday": spend_yesterday,
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
    await assert_org_member(session, current_user, body.org_id)
    await _assert_model_connector(session, body.org_id, body.model_connector_id)

    # Entitlement gate: agent count limit
    try:
        from app.core.billing.entitlements import resolve as _resolve_ent, EntitlementError, _to_http
        from app.db.models import Agent as _Agent, AgentStatus as _AgentStatus
        from sqlmodel import select as _select, func as _func
        _ent = await _resolve_ent(session, body.org_id)
        _active_statuses = [_AgentStatus.draft, _AgentStatus.published, _AgentStatus.paused]
        _agent_count = (await session.exec(
            _select(_func.count()).where(_Agent.org_id == body.org_id, _Agent.status.in_(_active_statuses))
        )).one()
        _ent.require_limit("agents", _agent_count)
    except EntitlementError as e:
        from app.core.billing.entitlements import _to_http
        raise _to_http(e)

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
    await assert_org_member(session, current_user, org_id)
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
        # No BYOK connector is fine when the org can run on Setod-managed keys.
        from app.core.billing.entitlements import resolve as _resolve_ent
        ent = await _resolve_ent(session, agent.org_id)
        if not ent.allows("managed_models"):
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
@limiter.limit("30/minute")
async def run_agent_once(
    request: Request,
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
# Extracted to _assist.py; mounted below.

# ── Triggers + Scenarios ──────────────────────────────────────────────────────
# Extracted to _triggers.py; mounted below.

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
    # from_attributes: `file` is an ORM row, not a dict. Without it pydantic 2.13 rejects
    # the object outright (older releases were lenient, which is how this got past local).
    out = KnowledgeFileOut.model_validate(file, from_attributes=True)
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
@limiter.limit("20/minute")
async def add_knowledge_url(
    request: Request,
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

    # Use the agent's own brain (BYOK connector or Setod-managed key).  If neither is
    # available, borrow any active OpenAI/Anthropic connector in the org for the summary.
    from app.core.agents.base import AgentRunError, _build_client_for, _resolve_config
    from app.core.llm.client import user_message as _user_msg

    config = _resolve_config(agent, use_published=False)
    try:
        try:
            client, *_ = await _build_client_for(session, agent, config)
        except AgentRunError:
            mc_result = await session.exec(
                select(Connector).where(
                    Connector.org_id == agent.org_id,
                    Connector.type.in_([ConnectorType.openai, ConnectorType.anthropic]),
                    Connector.status == ConnectorStatus.active,
                ).limit(1)
            )
            mc = mc_result.first()
            if mc is None:
                return ExplainOut(session_id=run.id, summary="No AI model available to generate a summary.")
            config["model_connector_id"] = str(mc.id)
            config["model"] = ""
            client, *_ = await _build_client_for(session, agent, config)

        response = await client.chat([_user_msg(prompt)], max_tokens=256)
        summary = response.content.strip()
    except Exception as exc:  # noqa: BLE001
        summary = f"Could not generate summary: {exc}"

    return ExplainOut(session_id=run.id, summary=summary)


# ── Tools ─────────────────────────────────────────────────────────────────────

async def _agent_tool_out(
    session: AsyncSession, agent_id: UUID, agent_tool: AgentTool, connector: Connector
) -> AgentToolOut:
    """Describe one attached account and the tools it contributes.

    The tools are built rather than read from a table because their names depend on the alias,
    and the builder UI has to show the model's view of them, not an idealised one.
    """
    ctx = ToolContext(db=session, agent_id=agent_id, connector=connector, alias=agent_tool.alias)
    if connector.type == ConnectorType.tables:
        # The tables builder generates one tool set per table and needs the live table
        # list pre-fetched — same as the agent runner path in registry.build_tools_for_agent.
        from app.integrations.registry import _fetch_org_tables
        object.__setattr__(ctx, "_org_tables", await _fetch_org_tables(session, connector.org_id))
    enabled = set(agent_tool.enabled_tools) if agent_tool.enabled_tools is not None else None
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
        await _agent_tool_out(session, agent.id, agent_tool, connector)
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
    # Built-in tables connector: first attach grants nothing. Table data is business-sensitive,
    # so the owner opts each table in (read / write) explicitly rather than opting out.
    if existing is None and connector.type == ConnectorType.tables and body.enabled_tools is None:
        agent_tool.enabled_tools = []
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

    return await _agent_tool_out(session, agent.id, agent_tool, connector)


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




# ── Sub-router mounts ────────────────────────────────────────────────────────
# These are included last so FastAPI matches static routes (/{agent_id}/knowledge,
# /{agent_id}/sessions, etc.) before the dynamic sub-router paths.
router.include_router(assist_router)
router.include_router(triggers_router)
