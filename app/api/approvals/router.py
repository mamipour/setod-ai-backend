"""
Approvals API
=============
Routes
------
GET  /approvals/           list pending (and recently resolved) requests for an org
POST /approvals/{id}/approve   approve — resumes the paused session via the worker's next tick
POST /approvals/{id}/reject    reject with an optional reason
GET  /approvals/count          just the pending count, for the sidebar badge
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.db.models import (
    Agent,
    ApprovalRequest,
    ApprovalStatus,
    OrganizationMember,
    SessionStatus,
    User,
)
from app.db.session import get_session

router = APIRouter(prefix="/approvals", tags=["approvals"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class ApprovalRequestOut(BaseModel):
    id: UUID
    session_id: UUID
    agent_id: UUID
    agent_name: str = ""
    agent_icon: str = ""
    tool_name: str
    tool_args: dict
    summary: str
    status: ApprovalStatus
    response_note: str | None
    created_at: datetime
    resolved_at: datetime | None
    expires_at: datetime

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    note: str = ""


class PendingCount(BaseModel):
    count: int


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _assert_org_member(session: AsyncSession, user: User, org_id: UUID) -> None:
    row = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if row.first() is None:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")


async def _get_owned_request(
    session: AsyncSession, user: User, request_id: UUID
) -> ApprovalRequest:
    req = await session.get(ApprovalRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    await _assert_org_member(session, user, req.org_id)
    return req


async def _enrich(session: AsyncSession, req: ApprovalRequest) -> ApprovalRequestOut:
    agent = await session.get(Agent, req.agent_id)
    out = ApprovalRequestOut.model_validate(req)
    if agent:
        out.agent_name = agent.name
        out.agent_icon = agent.icon
    return out


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/count", response_model=PendingCount)
async def pending_count(
    org_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Badge endpoint: just the number, polled from the sidebar."""
    await _assert_org_member(session, current_user, org_id)
    rows = await session.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.org_id == org_id,
            ApprovalRequest.status == ApprovalStatus.pending,
        )
    )
    return {"count": len(rows.all())}


@router.get("/", response_model=list[ApprovalRequestOut])
async def list_approvals(
    org_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    resolved: bool = False,
):
    """List approval requests. By default only pending ones; pass resolved=true for history."""
    await _assert_org_member(session, current_user, org_id)
    statuses = (
        [ApprovalStatus.approved, ApprovalStatus.rejected, ApprovalStatus.expired]
        if resolved
        else [ApprovalStatus.pending]
    )
    rows = await session.exec(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.org_id == org_id,
            ApprovalRequest.status.in_(statuses),
        )
        .order_by(ApprovalRequest.created_at.desc())
        .limit(50)
    )
    return [await _enrich(session, r) for r in rows.all()]


@router.post("/{request_id}/approve", response_model=ApprovalRequestOut)
async def approve(
    request_id: UUID,
    body: ApprovalDecision,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    req = await _get_owned_request(session, current_user, request_id)
    if req.status != ApprovalStatus.pending:
        raise HTTPException(status_code=409, detail=f"Request is already {req.status.value}.")
    req.status = ApprovalStatus.approved
    req.response_note = body.note or None
    req.resolved_at = datetime.now(UTC)
    session.add(req)
    await session.commit()
    await session.refresh(req)
    return await _enrich(session, req)


@router.post("/{request_id}/reject", response_model=ApprovalRequestOut)
async def reject(
    request_id: UUID,
    body: ApprovalDecision,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    req = await _get_owned_request(session, current_user, request_id)
    if req.status != ApprovalStatus.pending:
        raise HTTPException(status_code=409, detail=f"Request is already {req.status.value}.")
    req.status = ApprovalStatus.rejected
    req.response_note = body.note or None
    req.resolved_at = datetime.now(UTC)
    session.add(req)
    await session.commit()
    await session.refresh(req)
    return await _enrich(session, req)
