"""Code skills: user Python deployed as a Lambda and offered to agents as a tool."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import assert_org_member, assert_org_owner, get_current_user
from app.core.billing.entitlements import EntitlementError, _to_http, resolve
from app.core.code_skills import CodeSkillsDisabled, get_backend, service
from app.db.models import (
    Agent,
    AgentCodeSkillLink,
    CodeSkill,
    CodeSkillDeploy,
    CodeSkillDeployStatus,
    User,
)
from app.db.session import get_session
from app.limiter import limiter

router = APIRouter(prefix="/code-skills", tags=["code-skills"])


class CodeSkillOut(BaseModel):
    id: str
    org_id: str
    name: str
    tagline: str
    tool_name: str
    tool_description: str
    input_schema: dict[str, Any]
    source: str
    timeout_seconds: int
    network_access: bool
    read_only: bool
    deploy_status: str
    dirty: bool
    has_secrets: bool
    lambda_function_name: str | None
    last_deploy_error: str | None
    last_deployed_at: str | None
    invocation_count: int
    last_invoked_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


class CodeSkillCreate(BaseModel):
    org_id: UUID
    name: str
    tagline: str = ""
    tool_name: str
    tool_description: str
    input_schema: dict[str, Any]
    source: str
    timeout_seconds: int = 10
    network_access: bool = False
    read_only: bool = False


class CodeSkillUpdate(BaseModel):
    name: str | None = None
    tagline: str | None = None
    tool_name: str | None = None
    tool_description: str | None = None
    input_schema: dict[str, Any] | None = None
    source: str | None = None
    timeout_seconds: int | None = None
    network_access: bool | None = None
    read_only: bool | None = None


class SecretsIn(BaseModel):
    secrets: dict[str, str]


class TestIn(BaseModel):
    input: dict[str, Any] = {}


class AttachIn(BaseModel):
    requires_approval: bool = False


class AgentCodeSkillOut(CodeSkillOut):
    requires_approval: bool


def _out(skill: CodeSkill) -> CodeSkillOut:
    return CodeSkillOut(
        id=str(skill.id),
        org_id=str(skill.org_id),
        name=skill.name,
        tagline=skill.tagline,
        tool_name=skill.tool_name,
        tool_description=skill.tool_description,
        input_schema=skill.input_schema,
        source=skill.source,
        timeout_seconds=skill.timeout_seconds,
        network_access=skill.network_access,
        read_only=skill.read_only,
        deploy_status=skill.deploy_status,
        dirty=skill.dirty,
        has_secrets=bool(skill.secrets_enc),
        lambda_function_name=skill.lambda_function_name,
        last_deploy_error=skill.last_deploy_error,
        last_deployed_at=skill.last_deployed_at.isoformat() if skill.last_deployed_at else None,
        invocation_count=skill.invocation_count,
        last_invoked_at=skill.last_invoked_at.isoformat() if skill.last_invoked_at else None,
        last_error=skill.last_error,
        created_at=skill.created_at.isoformat(),
        updated_at=skill.updated_at.isoformat(),
    )


def _validation(exc: service.CodeSkillValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def _skill(session: AsyncSession, user: User, skill_id: UUID, *, owner: bool) -> CodeSkill:
    skill = await session.get(CodeSkill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Code skill not found")
    if owner:
        await assert_org_owner(session, user, skill.org_id)
    else:
        await assert_org_member(session, user, skill.org_id)
    return skill


async def _agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await assert_org_member(session, user, agent.org_id)
    return agent


def _check_fields(body: CodeSkillCreate | CodeSkillUpdate, *, partial: bool) -> None:
    try:
        if body.tool_name is not None:
            service.validate_tool_name(body.tool_name)
        if body.input_schema is not None:
            service.validate_input_schema(body.input_schema)
        if body.source is not None:
            service.validate_source(body.source)
        if body.timeout_seconds is not None:
            service.validate_timeout(body.timeout_seconds)
        if not partial:
            assert isinstance(body, CodeSkillCreate)
            service.validate_text_fields(
                name=body.name, tagline=body.tagline, tool_description=body.tool_description,
            )
        else:
            if body.name is not None or body.tagline is not None or body.tool_description is not None:
                service.validate_text_fields(
                    name=body.name if body.name is not None else "ok",
                    tagline=body.tagline if body.tagline is not None else "",
                    tool_description=body.tool_description if body.tool_description is not None else "ok",
                )
    except service.CodeSkillValidationError as exc:
        raise _validation(exc) from exc


@router.get("")
async def list_code_skills(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CodeSkillOut]:
    await assert_org_member(session, current_user, org_id)
    rows = (await session.exec(
        select(CodeSkill).where(CodeSkill.org_id == org_id).order_by(CodeSkill.name)
    )).all()
    return [_out(s) for s in rows]


@router.post("", status_code=201)
async def create_code_skill(
    body: CodeSkillCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CodeSkillOut:
    await assert_org_owner(session, current_user, body.org_id)
    _check_fields(body, partial=False)
    ent = await resolve(session, body.org_id)
    try:
        ent.require("code_skills")
        existing = (await session.exec(
            select(CodeSkill.id).where(CodeSkill.org_id == body.org_id)
        )).all()
        ent.require_limit("code_skills", len(existing))
    except EntitlementError as exc:
        raise _to_http(exc) from exc

    skill = CodeSkill(
        org_id=body.org_id,
        created_by_id=current_user.id,
        name=body.name,
        tagline=body.tagline,
        tool_name=body.tool_name,
        tool_description=body.tool_description,
        input_schema=body.input_schema,
        source=body.source,
        source_sha256=service.source_sha(body.source),
        timeout_seconds=body.timeout_seconds,
        network_access=body.network_access,
        read_only=body.read_only,
        deploy_status=CodeSkillDeployStatus.draft.value,
    )
    session.add(skill)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A code skill with this tool name already exists") from exc
    await session.refresh(skill)
    return _out(skill)


@router.get("/agent/{agent_id}")
async def list_agent_code_skills(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AgentCodeSkillOut]:
    agent = await _agent(session, current_user, agent_id)
    rows = (await session.exec(
        select(CodeSkill, AgentCodeSkillLink)
        .join(AgentCodeSkillLink, AgentCodeSkillLink.code_skill_id == CodeSkill.id)
        .where(AgentCodeSkillLink.agent_id == agent.id)
        .order_by(CodeSkill.name)
    )).all()
    out: list[AgentCodeSkillOut] = []
    for skill, link in rows:
        base = _out(skill).model_dump()
        base["requires_approval"] = link.requires_approval
        out.append(AgentCodeSkillOut(**base))
    return out


@router.put("/agent/{agent_id}/{skill_id}", status_code=204)
async def attach_code_skill(
    agent_id: UUID,
    skill_id: UUID,
    body: AttachIn,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    agent = await _agent(session, current_user, agent_id)
    skill = await _skill(session, current_user, skill_id, owner=False)
    if agent.org_id != skill.org_id:
        raise HTTPException(status_code=403, detail="Code skill does not belong to this agent's organisation")
    link = (await session.exec(
        select(AgentCodeSkillLink).where(
            AgentCodeSkillLink.agent_id == agent.id,
            AgentCodeSkillLink.code_skill_id == skill.id,
        )
    )).first()
    if link is None:
        link = AgentCodeSkillLink(agent_id=agent.id, code_skill_id=skill.id, requires_approval=body.requires_approval)
    else:
        link.requires_approval = body.requires_approval
    session.add(link)
    agent.updated_at = datetime.now(UTC)
    session.add(agent)
    await session.commit()


@router.delete("/agent/{agent_id}/{skill_id}", status_code=204)
async def detach_code_skill(
    agent_id: UUID,
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    agent = await _agent(session, current_user, agent_id)
    link = (await session.exec(
        select(AgentCodeSkillLink).where(
            AgentCodeSkillLink.agent_id == agent.id,
            AgentCodeSkillLink.code_skill_id == skill_id,
        )
    )).first()
    if link is not None:
        await session.delete(link)
        agent.updated_at = datetime.now(UTC)
        session.add(agent)
        await session.commit()


@router.get("/{skill_id}")
async def get_code_skill(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CodeSkillOut:
    return _out(await _skill(session, current_user, skill_id, owner=False))


@router.patch("/{skill_id}")
async def update_code_skill(
    skill_id: UUID,
    body: CodeSkillUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CodeSkillOut:
    skill = await _skill(session, current_user, skill_id, owner=True)
    _check_fields(body, partial=True)
    data = body.model_dump(exclude_none=True)
    if "source" in data:
        data["source_sha256"] = service.source_sha(data["source"])
    for field, value in data.items():
        setattr(skill, field, value)
    skill.updated_at = datetime.now(UTC)
    session.add(skill)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A code skill with this tool name already exists") from exc
    await session.refresh(skill)
    return _out(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_code_skill(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    skill = await _skill(session, current_user, skill_id, owner=True)
    backend = None
    if skill.lambda_function_name:
        try:
            backend = get_backend()
        except CodeSkillsDisabled as exc:
            raise HTTPException(status_code=503, detail="Code skills are not enabled on this deployment.") from exc
    await service.delete_skill(session, skill, backend)


async def start_code_skill_deploy(
    session: AsyncSession, current_user: User, skill_id: UUID,
) -> dict:
    """Deploy body, shared by the HTTP route and the MCP tool. The route adds the limiter."""
    skill = await _skill(session, current_user, skill_id, owner=True)
    if skill.deploy_status == CodeSkillDeployStatus.deploying.value:
        raise HTTPException(status_code=409, detail="This code skill is already deploying")
    ent = await resolve(session, skill.org_id)
    try:
        ent.require("code_skills")
    except EntitlementError as exc:
        raise _to_http(exc) from exc
    try:
        backend = get_backend()
    except CodeSkillsDisabled as exc:
        raise HTTPException(status_code=503, detail="Code skills are not enabled on this deployment.") from exc
    try:
        deploy = await service.start_deploy(session, skill, current_user, backend)
    except service.CodeSkillRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"deploy_id": str(deploy.id)}


@router.post("/{skill_id}/deploy", status_code=202)
@limiter.limit("10/minute")
async def deploy_code_skill(
    request: Request,
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return await start_code_skill_deploy(session, current_user, skill_id)


async def run_code_skill_test(
    session: AsyncSession, current_user: User, skill_id: UUID, body: TestIn,
) -> dict:
    skill = await _skill(session, current_user, skill_id, owner=True)
    if skill.deploy_status != CodeSkillDeployStatus.ready.value:
        raise HTTPException(status_code=409, detail="Deploy this code skill before testing it")
    try:
        backend = get_backend()
    except CodeSkillsDisabled as exc:
        raise HTTPException(status_code=503, detail="Code skills are not enabled on this deployment.") from exc
    text, res = await service.invoke(
        session,
        skill,
        args=body.input,
        ctx={
            "org_id": str(skill.org_id),
            "agent_id": None,
            "session_id": None,
            "skill_id": str(skill.id),
            "dry_run": False,
            "invoked_at": datetime.now(UTC).isoformat(),
        },
        backend=backend,
        want_logs=True,
        idempotency_key=f"test:{skill.id}:{uuid4()}",
        agent_id=None,
        session_id=None,
    )
    ok = bool(res and res["payload"] and res["payload"].get("ok"))
    payload = (res or {}).get("payload") or {}
    return {
        "ok": ok,
        "result": payload.get("result") if ok else None,
        "error": None if ok else text.removeprefix("Error: "),
        "duration_ms": res["duration_ms"] if res else 0,
        "log_tail": res["log_tail"] if res else None,
    }


@router.post("/{skill_id}/test")
@limiter.limit("30/minute")
async def test_code_skill(
    request: Request,
    skill_id: UUID,
    body: TestIn,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return await run_code_skill_test(session, current_user, skill_id, body)


@router.get("/{skill_id}/secrets")
async def list_secret_keys(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    skill = await _skill(session, current_user, skill_id, owner=True)
    return {"keys": sorted(service.decrypt_secrets(skill))}


@router.put("/{skill_id}/secrets")
async def replace_secrets(
    skill_id: UUID,
    body: SecretsIn,
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    skill = await _skill(session, current_user, skill_id, owner=True)
    try:
        service.validate_secrets(body.secrets)
    except service.CodeSkillValidationError as exc:
        raise _validation(exc) from exc
    from app.core import crypto
    skill.secrets_enc = crypto.encrypt_json(body.secrets) if body.secrets else None
    skill.updated_at = datetime.now(UTC)
    session.add(skill)
    await session.commit()
    response.headers["X-Setod-Note"] = "redeploy required"
    return {"keys": sorted(body.secrets)}


@router.get("/{skill_id}/deploys")
async def list_deploys(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict]:
    skill = await _skill(session, current_user, skill_id, owner=False)
    rows = (await session.exec(
        select(CodeSkillDeploy)
        .where(CodeSkillDeploy.code_skill_id == skill.id)
        .order_by(CodeSkillDeploy.started_at.desc())  # type: ignore[attr-defined]
        .limit(20)
    )).all()
    return [
        {
            "id": str(row.id),
            "source_sha256": row.source_sha256,
            "network_access": row.network_access,
            "outcome": row.outcome,
            "error": row.error,
            "started_at": row.started_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
        for row in rows
    ]
