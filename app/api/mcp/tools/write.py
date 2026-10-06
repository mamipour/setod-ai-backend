"""Write tools. Owner + write scope, enforced in the protocol layer before we get here."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app.api.agents._triggers import create_agent_trigger, update_agent_trigger
from app.api.agents.router import (
    attach_agent_tool,
    create_agent,
    detach_agent_tool,
    pause_agent,
    publish_agent,
    publish_history,
    resume_agent,
    rollback_agent,
    unpublish_agent,
    update_agent,
)
from app.api.agents.schemas import AgentCreate, AgentToolAttach, AgentUpdate, MemoryEntryIn, TriggerUpsert
from app.api.approvals.router import approve, reject
from app.api.approvals.router import ApprovalDecision
from app.api.code_skills.router import (
    AttachIn,
    CodeSkillCreate,
    CodeSkillUpdate,
    TestIn,
    attach_code_skill,
    create_code_skill,
    detach_code_skill,
    run_code_skill_test,
    start_code_skill_deploy,
    update_code_skill,
)
from app.api.agents._assist import run_trace
from app.api.mcp.tools.base import McpTool, ToolError
from app.api.mcp.tools.read import _agent, dumps
from app.api.skills.router import attach_skill, detach_skill
from app.core.agents.base import run_agent, snapshot_config
from app.core.triggers import schedule
from app.db.models import (
    DEFAULT_AGENT_SETTINGS,
    Agent,
    AgentCodeSkillLink,
    AgentSession,
    AgentSkillLink,
    AgentStatus,
    AgentTool,
    AgentTrigger,
    ApprovalRequest,
    CodeSkill,
    Connector,
    ConnectorStatus,
    SessionStatus,
    TriggerType,
)


def _detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


async def _route(awaitable):
    try:
        return await awaitable
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc


def _filter_settings(current: dict, incoming: dict) -> dict:
    allowed = set(DEFAULT_AGENT_SETTINGS)
    cleaned = {key: value for key, value in incoming.items() if key in allowed}
    if isinstance(cleaned.get("media_policy"), dict) and isinstance(current.get("media_policy"), dict):
        cleaned["media_policy"] = {**current["media_policy"], **cleaned["media_policy"]}
    return cleaned


async def _require_apply_live(agent: Agent, confirm: str | None) -> None:
    if agent.status == AgentStatus.published and confirm != "APPLY LIVE":
        raise ToolError(
            'This agent is published. Attaching or detaching takes effect on the next run '
            'without another publish. Pass confirm="APPLY LIVE" after the owner agrees.'
        )


class CreateAgentArgs(BaseModel):
    name: str
    instructions: str = ""
    model_connector_id: UUID | None = None
    model: str = ""
    template_key: str | None = None
    settings: dict[str, Any] | None = None


class UpdateAgentArgs(BaseModel):
    agent_id: UUID
    name: str | None = None
    icon: str | None = None
    instructions: str | None = None
    model_connector_id: UUID | None = None
    model: str | None = None
    settings: dict[str, Any] | None = None


class AttachConnectorArgs(BaseModel):
    agent_id: UUID
    connector_id: UUID
    enabled_tools: list[str] | None = None
    approval_tools: list[str] | None = None
    alias: str = ""
    confirm: str | None = None


class DetachConnectorArgs(BaseModel):
    agent_id: UUID
    connector_id: UUID
    confirm: str | None = None


class SkillLinkArgs(BaseModel):
    agent_id: UUID
    skill_id: UUID
    confirm: str | None = None


class CodeSkillLinkArgs(BaseModel):
    agent_id: UUID
    code_skill_id: UUID
    requires_approval: bool = False
    confirm: str | None = None


class CodeSkillDetachArgs(BaseModel):
    agent_id: UUID
    code_skill_id: UUID
    confirm: str | None = None


class CreateCodeSkillArgs(BaseModel):
    name: str
    tool_name: str
    tool_description: str
    input_schema: dict[str, Any]
    source: str
    network_access: bool = False
    read_only: bool = False
    timeout_seconds: int = 10


class UpdateCodeSkillArgs(BaseModel):
    code_skill_id: UUID
    name: str | None = None
    tool_name: str | None = None
    tool_description: str | None = None
    input_schema: dict[str, Any] | None = None
    source: str | None = None
    network_access: bool | None = None
    read_only: bool | None = None
    timeout_seconds: int | None = None


class DeployArgs(BaseModel):
    code_skill_id: UUID


class TestCodeArgs(BaseModel):
    code_skill_id: UUID
    input: dict[str, Any] = {}


class TriggerArgs(BaseModel):
    agent_id: UUID
    preset: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool = True
    confirm: str | None = None


class MemoryArgs(BaseModel):
    agent_id: UUID
    key: str
    value: Any = None


class MemoryKeyArgs(BaseModel):
    agent_id: UUID
    key: str


class ValidateArgs(BaseModel):
    agent_id: UUID


class RunArgs(BaseModel):
    agent_id: UUID
    message: str | None = None
    dry_run: bool = True
    use_draft: bool = True
    confirm: str | None = None


class PublishArgs(BaseModel):
    agent_id: UUID
    confirm: str


class AgentOnly(BaseModel):
    agent_id: UUID


class RollbackArgs(BaseModel):
    agent_id: UUID
    snapshot_id: UUID


class ApprovalArgs(BaseModel):
    approval_id: UUID
    note: str | None = None


async def _create_agent(principal, session, args: CreateAgentArgs) -> str:
    settings = _filter_settings({}, args.settings or {}) if args.settings else None
    try:
        out = await create_agent(
            AgentCreate(
                org_id=principal.org_id,
                name=args.name,
                instructions=args.instructions,
                model_connector_id=args.model_connector_id,
                model=args.model,
                template_key=args.template_key,
                settings=settings,
            ),
            principal.user,
            session,
        )
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _get_after(principal, session, out.id)


async def _get_after(principal, session, agent_id: UUID) -> str:
    from app.api.mcp.tools.read import _get_agent, AgentId
    return await _get_agent(principal, session, AgentId(agent_id=agent_id))


async def _update_agent(principal, session, args: UpdateAgentArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    changes = args.model_dump(exclude_unset=True, exclude={"agent_id"})
    if "settings" in changes and changes["settings"] is not None:
        changes["settings"] = _filter_settings(agent.settings or {}, changes["settings"])
    try:
        await update_agent(agent.id, AgentUpdate(**changes), principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _get_after(principal, session, agent.id)


async def _attach_connector(principal, session, args: AttachConnectorArgs) -> str:
    if args.enabled_tools is not None and len(args.enabled_tools) == 0:
        raise ToolError("enabled_tools cannot be empty; omit it to enable every tool")
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    try:
        await attach_agent_tool(
            agent.id,
            AgentToolAttach(
                connector_id=args.connector_id,
                alias=args.alias,
                enabled_tools=args.enabled_tools,
                approval_tools=args.approval_tools,
            ),
            session,
            principal.user,
        )
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    note = "This is live on the next run.\n\n" if agent.status == AgentStatus.published else ""
    return note + await _get_after(principal, session, agent.id)


async def _detach_connector(principal, session, args: DetachConnectorArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    try:
        await detach_agent_tool(agent.id, args.connector_id, session, principal.user)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    note = "This is live on the next run.\n\n" if agent.status == AgentStatus.published else ""
    return note + await _get_after(principal, session, agent.id)


async def _attach_skill(principal, session, args: SkillLinkArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    try:
        await attach_skill(agent.id, args.skill_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _get_after(principal, session, agent.id)


async def _detach_skill(principal, session, args: SkillLinkArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    try:
        await detach_skill(agent.id, args.skill_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _get_after(principal, session, agent.id)


async def _attach_code(principal, session, args: CodeSkillLinkArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    try:
        await attach_code_skill(
            agent.id, args.code_skill_id, AttachIn(requires_approval=args.requires_approval),
            principal.user, session,
        )
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _get_after(principal, session, agent.id)


async def _detach_code(principal, session, args: CodeSkillDetachArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    try:
        await detach_code_skill(agent.id, args.code_skill_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _get_after(principal, session, agent.id)


async def _create_code(principal, session, args: CreateCodeSkillArgs) -> str:
    from app.api.mcp.tools.read import _code_skill, CodeSkillId
    try:
        created = await create_code_skill(
            CodeSkillCreate(org_id=principal.org_id, **args.model_dump()),
            principal.user,
            session,
        )
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _code_skill(principal, session, CodeSkillId(code_skill_id=UUID(created.id)))


async def _update_code(principal, session, args: UpdateCodeSkillArgs) -> str:
    from app.api.mcp.tools.read import _code_skill, CodeSkillId
    skill = await session.get(CodeSkill, args.code_skill_id)
    if skill is None or skill.org_id != principal.org_id:
        raise ToolError("Code skill not found")
    fields = args.model_dump(exclude_unset=True, exclude={"code_skill_id"})
    try:
        await update_code_skill(skill.id, CodeSkillUpdate(**fields), principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return await _code_skill(principal, session, CodeSkillId(code_skill_id=skill.id))


async def _deploy_code(principal, session, args: DeployArgs) -> str:
    skill = await session.get(CodeSkill, args.code_skill_id)
    if skill is None or skill.org_id != principal.org_id:
        raise ToolError("Code skill not found")
    try:
        started = await start_code_skill_deploy(session, principal.user, skill.id)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps({"deploy_status": "deploying", **started})


async def _test_code(principal, session, args: TestCodeArgs) -> str:
    skill = await session.get(CodeSkill, args.code_skill_id)
    if skill is None or skill.org_id != principal.org_id:
        raise ToolError("Code skill not found")
    try:
        return dumps(await run_code_skill_test(session, principal.user, skill.id, TestIn(input=args.input)))
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc


async def _set_trigger(principal, session, args: TriggerArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    await _require_apply_live(agent, args.confirm)
    if args.preset:
        if args.preset not in schedule.PRESETS:
            raise ToolError("Unknown preset. Call setod_list_schedule_presets.")
        config = {"preset": args.preset}
    elif args.cron:
        config = {"cron": args.cron, "timezone": args.timezone or "UTC"}
    else:
        raise ToolError("Pass preset or cron")
    body = TriggerUpsert(type=TriggerType.schedule, config=config, enabled=args.enabled)
    existing = (await session.exec(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == agent.id,
            AgentTrigger.type == TriggerType.schedule,
        )
    )).first()
    try:
        if existing:
            out = await update_agent_trigger(agent.id, existing.id, body, session, principal.user)
        else:
            out = await create_agent_trigger(agent.id, body, session, principal.user)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps(out)


async def _set_memory(principal, session, args: MemoryArgs) -> str:
    from app.api.agents.router import put_memory
    agent = await _agent(session, principal, args.agent_id)
    try:
        row = await put_memory(agent.id, args.key, MemoryEntryIn(value=args.value), principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps(row)


async def _delete_memory(principal, session, args: MemoryKeyArgs) -> str:
    from app.api.agents.router import delete_memory
    agent = await _agent(session, principal, args.agent_id)
    try:
        await delete_memory(agent.id, args.key, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps({"deleted": args.key})


def validation_problems(agent: Agent, tools: list, code_links: list, code_skills: dict, triggers: list, connectors: dict) -> list[dict]:
    """Pure checks. `ok` is false only for problems that will make a run fail."""
    problems: list[dict] = []

    def add(code: str, message: str, fix: str, *, blocks: bool = True) -> None:
        problems.append({"code": code, "message": message, "fix": fix, "blocks": blocks})

    if agent.model_connector_id is None:
        add("no_model", "No model connector is selected.", "Attach an OpenAI or Anthropic connector and set it as the model.")
    if len((agent.instructions or "").strip()) < 40:
        add("no_instructions", "Instructions are missing or shorter than 40 characters.", "Write the instructions in plain English.")
    web_on = bool((agent.settings or {}).get("web_search") or (agent.settings or {}).get("live_page_access"))
    if not tools and not code_links and not web_on:
        add("no_tools", "Nothing is attached and web access is off.", "Attach a connector or a code skill, or turn on web search.")
    for tool in tools:
        if tool.enabled_tools is not None and len(tool.enabled_tools) == 0:
            connector = connectors.get(tool.connector_id)
            # The tables connector starts empty on purpose; opting tables in is a separate step.
            if connector is None or connector.type.value != "tables":
                add(
                    "empty_enabled_tools",
                    "A connector is attached with enabled_tools set to an empty list, so it exposes nothing.",
                    "Omit enabled_tools to enable every tool, or list the ones you want.",
                )
                break
        connector = connectors.get(tool.connector_id)
        if connector is not None and connector.status != ConnectorStatus.active:
            add("connector_error", f"{connector.name} is {connector.status.value}.", "Reconnect it on the Connectors page.")
    for link in code_links:
        skill = code_skills.get(link.code_skill_id)
        if skill is not None and skill.deploy_status != "ready":
            add("code_skill_not_ready", f"Code skill {skill.name} is {skill.deploy_status}.", "Deploy it and wait until it is ready.")
    if not any(t.enabled for t in triggers):
        add("no_trigger", "No enabled trigger, so the agent only runs when someone starts it.", "Add a schedule with setod_set_trigger.", blocks=False)
    if agent.published_config and agent.published_config != snapshot_config(agent):
        add("unpublished_changes", "The draft differs from what is published.", "Dry-run the draft, then publish.", blocks=False)
    text = agent.instructions or ""
    if "memory_set(" in text or "code_" in text or "send_sms(" in text:
        add(
            "mentions_tool_syntax",
            "Instructions mention tool syntax. The agent follows plain English better.",
            "Say what to remember or send, and do not name the tool.",
            blocks=False,
        )
    return problems


async def _validate(principal, session, args: ValidateArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    tools = (await session.exec(select(AgentTool).where(AgentTool.agent_id == agent.id))).all()
    links = (await session.exec(select(AgentCodeSkillLink).where(AgentCodeSkillLink.agent_id == agent.id))).all()
    skills = {}
    if links:
        rows = (await session.exec(select(CodeSkill).where(CodeSkill.id.in_([link.code_skill_id for link in links])))).all()
        skills = {row.id: row for row in rows}
    triggers = (await session.exec(select(AgentTrigger).where(AgentTrigger.agent_id == agent.id))).all()
    ids = [tool.connector_id for tool in tools]
    connectors = {}
    if ids:
        rows = (await session.exec(select(Connector).where(Connector.id.in_(ids)))).all()
        connectors = {row.id: row for row in rows}
    problems = validation_problems(agent, tools, links, skills, triggers, connectors)
    ok = not any(p["blocks"] for p in problems)
    return dumps({"ok": ok, "problems": problems})


async def _run(principal, session, args: RunArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    if not args.dry_run and args.confirm != "RUN LIVE":
        raise ToolError('A live run sends real messages. Pass confirm="RUN LIVE" only after the owner asks for it.')
    if not args.use_draft and not agent.published_config:
        raise ToolError("This agent has not been published yet. Run the draft instead.")
    from app.db.models import TriggerType as TT
    try:
        result = await run_agent(
            session,
            agent,
            trigger_type=TT.manual,
            user_input=args.message,
            dry_run=args.dry_run,
            use_published=not args.use_draft,
        )
    except Exception as exc:  # AgentRunError and anything the runner raises
        from app.core.agents.base import AgentRunError
        if isinstance(exc, AgentRunError):
            raise ToolError(str(exc)) from exc
        raise
    await session.refresh(result)
    trace = await run_trace(session, agent, 1)
    summary = {
        "run_number": 1,
        "id": str(result.id),
        "status": result.status.value,
        "dry_run": result.dry_run,
        "tokens": result.total_tokens,
        "error": result.error,
    }
    return dumps(summary) + "\n\n" + trace


async def publish_refusal(agent: Agent, sessions: list[AgentSession], watermark: datetime, now: datetime) -> str | None:
    """None when a recent succeeded dry run is newer than the watermark. The text is the refusal."""
    cutoff = now - timedelta(minutes=30)
    ok = any(
        s.dry_run and s.status == SessionStatus.succeeded and s.started_at and s.started_at > watermark and s.started_at > cutoff
        for s in sessions
    )
    if ok:
        return None
    return (
        "Refused: run setod_run_agent (dry run, draft) after your last edit and get status "
        "succeeded, then publish."
    )


async def _watermark(session, agent: Agent) -> datetime:
    times = [agent.updated_at]
    for row in (await session.exec(select(AgentTool.created_at).where(AgentTool.agent_id == agent.id))).all():
        times.append(row)
    for row in (await session.exec(select(AgentSkillLink.attached_at).where(AgentSkillLink.agent_id == agent.id))).all():
        times.append(row)
    for row in (await session.exec(select(AgentCodeSkillLink.attached_at).where(AgentCodeSkillLink.agent_id == agent.id))).all():
        times.append(row)
    return max(t for t in times if t is not None)


async def _publish(principal, session, args: PublishArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    if args.confirm != "PUBLISH":
        raise ToolError('Pass confirm="PUBLISH" to publish.')
    sessions = (await session.exec(
        select(AgentSession).where(AgentSession.agent_id == agent.id, AgentSession.dry_run.is_(True))
    )).all()
    refusal = await publish_refusal(agent, sessions, await _watermark(session, agent), datetime.now(UTC))
    if refusal:
        raise ToolError(refusal)
    try:
        out = await publish_agent(agent.id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps(out)


async def _status_change(fn, principal, session, args: AgentOnly) -> str:
    agent = await _agent(session, principal, args.agent_id)
    try:
        out = await fn(agent.id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps(out)


async def _unpublish(principal, session, args: AgentOnly) -> str:
    return await _status_change(unpublish_agent, principal, session, args)


async def _pause(principal, session, args: AgentOnly) -> str:
    return await _status_change(pause_agent, principal, session, args)


async def _resume(principal, session, args: AgentOnly) -> str:
    return await _status_change(resume_agent, principal, session, args)


async def _history(principal, session, args: AgentOnly) -> str:
    agent = await _agent(session, principal, args.agent_id)
    try:
        rows = await publish_history(agent.id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps([
        {
            "snapshot_id": row["id"],
            "published_at": row["published_at"],
            "model": row["model"],
            "instructions_preview": row["instructions_preview"],
        }
        for row in rows
    ])


async def _rollback(principal, session, args: RollbackArgs) -> str:
    agent = await _agent(session, principal, args.agent_id)
    try:
        out = await rollback_agent(agent.id, args.snapshot_id, principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps(out)


async def _approval(fn, principal, session, args: ApprovalArgs) -> str:
    req = await session.get(ApprovalRequest, args.approval_id)
    if req is None or req.org_id != principal.org_id:
        raise ToolError("Approval request not found")
    try:
        out = await fn(req.id, ApprovalDecision(note=args.note or ""), principal.user, session)
    except HTTPException as exc:
        raise ToolError(_detail(exc)) from exc
    return dumps(out)


async def _approve(principal, session, args: ApprovalArgs) -> str:
    return await _approval(approve, principal, session, args)


async def _reject(principal, session, args: ApprovalArgs) -> str:
    return await _approval(reject, principal, session, args)


def _w(name, description, model, handler, *, destructive=False) -> McpTool:
    return McpTool(
        name=name, description=description, args_model=model, handler=handler,
        write=True, destructive=destructive, audited=True,
    )


TOOLS = {
    t.name: t for t in [
        _w("setod_create_agent", "Create a draft agent in this workspace.", CreateAgentArgs, _create_agent),
        _w("setod_update_agent", "Edit a draft. Settings merge; unknown keys are dropped.", UpdateAgentArgs, _update_agent),
        _w("setod_attach_connector", "Attach a connector, or update which of its tools are on. Empty enabled_tools is rejected. A published agent needs confirm APPLY LIVE.", AttachConnectorArgs, _attach_connector),
        _w("setod_detach_connector", "Detach a connector from an agent. Does not delete the connector. A published agent needs confirm APPLY LIVE.", DetachConnectorArgs, _detach_connector),
        _w("setod_attach_skill", "Attach a prompt skill. A published agent needs confirm APPLY LIVE.", SkillLinkArgs, _attach_skill),
        _w("setod_detach_skill", "Detach a prompt skill. A published agent needs confirm APPLY LIVE.", SkillLinkArgs, _detach_skill),
        _w("setod_attach_code_skill", "Attach a code skill. A published agent needs confirm APPLY LIVE.", CodeSkillLinkArgs, _attach_code),
        _w("setod_detach_code_skill", "Detach a code skill. A published agent needs confirm APPLY LIVE.", CodeSkillDetachArgs, _detach_code),
        _w("setod_create_code_skill", "Create a draft code skill. Deploy it before an agent can call it.", CreateCodeSkillArgs, _create_code),
        _w("setod_update_code_skill", "Edit a code skill. Deploy again to apply.", UpdateCodeSkillArgs, _update_code),
        _w("setod_deploy_code_skill", "Start deploying a code skill. Poll setod_get_code_skill until ready or failed.", DeployArgs, _deploy_code),
        _w("setod_test_code_skill", "Invoke a ready code skill with a JSON input.", TestCodeArgs, _test_code),
        _w("setod_set_trigger", "Create or replace the schedule trigger. preset or cron. A published agent needs confirm APPLY LIVE.", TriggerArgs, _set_trigger),
        _w("setod_set_memory", "Set one memory key. Same limits as the agent's memory_set.", MemoryArgs, _set_memory),
        _w("setod_delete_memory", "Delete one memory key.", MemoryKeyArgs, _delete_memory, destructive=True),
        _w("setod_validate_agent", "Check an agent for the problems that make a run fail. Fix every blocking problem before the dry run.", ValidateArgs, _validate),
        _w("setod_run_agent", "Run an agent. dry_run defaults to true and use_draft to true. A live run needs confirm RUN LIVE.", RunArgs, _run, destructive=True),
        _w("setod_publish_agent", "Publish the draft. Needs confirm PUBLISH and a succeeded dry run of the current draft in the last 30 minutes.", PublishArgs, _publish, destructive=True),
        _w("setod_unpublish_agent", "Take an agent offline. The draft is kept.", AgentOnly, _unpublish),
        _w("setod_pause_agent", "Pause a published agent. The published snapshot is kept.", AgentOnly, _pause),
        _w("setod_resume_agent", "Resume a paused agent.", AgentOnly, _resume),
        _w("setod_list_publish_history", "Past publishes, newest first.", AgentOnly, _history),
        _w("setod_rollback_agent", "Copy a past snapshot into the draft. Does not publish.", RollbackArgs, _rollback, destructive=True),
        _w("setod_approve_approval", "Approve a pending tool call. The run resumes on the worker.", ApprovalArgs, _approve),
        _w("setod_reject_approval", "Reject a pending tool call.", ApprovalArgs, _reject, destructive=True),
    ]
}
