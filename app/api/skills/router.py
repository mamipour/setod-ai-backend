from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.db.models import Agent, AgentSkillLink, OrganizationMember, Skill, User
from app.db.session import get_session

router = APIRouter(prefix="/skills", tags=["skills"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _assert_org_member(session: AsyncSession, user: User, org_id: UUID) -> None:
    row = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if not row.first():
        raise HTTPException(status_code=403, detail="Not a member of this organisation")


async def _get_skill(session: AsyncSession, user: User, skill_id: UUID) -> Skill:
    skill = await session.get(Skill, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    await _assert_org_member(session, user, skill.org_id)
    return skill


async def _get_owned_agent(session: AsyncSession, user: User, agent_id: UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _assert_org_member(session, user, agent.org_id)
    return agent


# ── Schemas ───────────────────────────────────────────────────────────────────

class SkillOut(BaseModel):
    id: str
    org_id: str
    key: str | None
    name: str
    tagline: str
    category: str
    content: str
    is_default: bool
    created_at: str
    updated_at: str


class SkillCreate(BaseModel):
    org_id: UUID
    name: str
    tagline: str = ""
    category: str = "Custom"
    content: str


class SkillUpdate(BaseModel):
    name: str | None = None
    tagline: str | None = None
    category: str | None = None
    content: str | None = None


def _to_out(s: Skill) -> SkillOut:
    return SkillOut(
        id=str(s.id),
        org_id=str(s.org_id),
        key=s.key,
        name=s.name,
        tagline=s.tagline,
        category=s.category,
        content=s.content,
        is_default=s.is_default,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_skills(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SkillOut]:
    await _assert_org_member(session, current_user, org_id)
    result = await session.exec(
        select(Skill)
        .where(Skill.org_id == org_id)
        .order_by(Skill.is_default.desc(), Skill.category, Skill.name)
    )
    return [_to_out(s) for s in result.all()]


@router.post("", status_code=201)
async def create_skill(
    body: SkillCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SkillOut:
    await _assert_org_member(session, current_user, body.org_id)
    skill = Skill(
        org_id=body.org_id,
        name=body.name,
        tagline=body.tagline,
        category=body.category,
        content=body.content,
        is_default=False,
    )
    session.add(skill)
    await session.commit()
    await session.refresh(skill)
    return _to_out(skill)


@router.patch("/{skill_id}")
async def update_skill(
    skill_id: UUID,
    body: SkillUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SkillOut:
    skill = await _get_skill(session, current_user, skill_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(skill, field, value)
    skill.updated_at = datetime.now(UTC)
    session.add(skill)
    await session.commit()
    await session.refresh(skill)
    return _to_out(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    skill = await _get_skill(session, current_user, skill_id)
    await session.delete(skill)
    await session.commit()


# ── Agent attach / detach ─────────────────────────────────────────────────────

@router.get("/agent/{agent_id}")
async def list_agent_skills(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SkillOut]:
    """Return all skills currently attached to this agent."""
    agent = await _get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(Skill)
        .join(AgentSkillLink, AgentSkillLink.skill_id == Skill.id)
        .where(AgentSkillLink.agent_id == agent.id)
        .order_by(Skill.category, Skill.name)
    )
    return [_to_out(s) for s in result.all()]


@router.post("/agent/{agent_id}/{skill_id}", status_code=204)
async def attach_skill(
    agent_id: UUID,
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    agent = await _get_owned_agent(session, current_user, agent_id)
    skill = await _get_skill(session, current_user, skill_id)
    if agent.org_id != skill.org_id:
        raise HTTPException(status_code=403, detail="Skill does not belong to this agent's organisation")
    existing = await session.exec(
        select(AgentSkillLink).where(
            AgentSkillLink.agent_id == agent.id,
            AgentSkillLink.skill_id == skill.id,
        )
    )
    if existing.first():
        return  # idempotent
    session.add(AgentSkillLink(agent_id=agent.id, skill_id=skill.id))
    await session.commit()


@router.delete("/agent/{agent_id}/{skill_id}", status_code=204)
async def detach_skill(
    agent_id: UUID,
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    agent = await _get_owned_agent(session, current_user, agent_id)
    row = await session.exec(
        select(AgentSkillLink).where(
            AgentSkillLink.agent_id == agent.id,
            AgentSkillLink.skill_id == skill_id,
        )
    )
    link = row.first()
    if link:
        await session.delete(link)
        await session.commit()
