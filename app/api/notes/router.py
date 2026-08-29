from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.core.notes import MAX_BODY, MAX_LIVE_PER_ORG
from app.db.models import OrganizationMember, OwnerNote, User
from app.db.session import get_session

router = APIRouter(prefix="/notes", tags=["notes"])


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


async def _get_note(session: AsyncSession, user: User, note_id: UUID) -> OwnerNote:
    note = await session.get(OwnerNote, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await _assert_org_member(session, user, note.org_id)
    return note


# ── Schemas ───────────────────────────────────────────────────────────────────

class NoteOut(BaseModel):
    id: str
    org_id: str
    created_by: str
    body: str
    agent_ids: list[str] | None
    expires_at: str | None
    agent_resolvable: bool
    resolved_at: str | None
    resolved_by: str | None
    resolution: str
    created_at: str
    updated_at: str


class NoteCreate(BaseModel):
    org_id: UUID
    body: str
    agent_ids: list[str] | None = None
    expires_at: datetime | None = None
    agent_resolvable: bool = False


class NoteUpdate(BaseModel):
    body: str | None = None
    agent_ids: list[str] | None = None
    expires_at: datetime | None = None
    agent_resolvable: bool | None = None


def _to_out(n: OwnerNote) -> NoteOut:
    return NoteOut(
        id=str(n.id),
        org_id=str(n.org_id),
        created_by=str(n.created_by),
        body=n.body,
        agent_ids=n.agent_ids,
        expires_at=n.expires_at.isoformat() if n.expires_at else None,
        agent_resolvable=n.agent_resolvable,
        resolved_at=n.resolved_at.isoformat() if n.resolved_at else None,
        resolved_by=str(n.resolved_by) if n.resolved_by else None,
        resolution=n.resolution,
        created_at=n.created_at.isoformat(),
        updated_at=n.updated_at.isoformat(),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_notes(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[NoteOut]:
    """Return all notes for this org (live, resolved, and expired), newest first."""
    await _assert_org_member(session, current_user, org_id)
    result = await session.exec(
        select(OwnerNote)
        .where(OwnerNote.org_id == org_id)
        .order_by(OwnerNote.created_at.desc())
    )
    return [_to_out(n) for n in result.all()]


@router.post("", status_code=201)
async def create_note(
    body: NoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NoteOut:
    await _assert_org_member(session, current_user, body.org_id)

    if len(body.body.strip()) == 0:
        raise HTTPException(status_code=422, detail="Note body cannot be empty")
    if len(body.body) > MAX_BODY:
        raise HTTPException(status_code=422, detail=f"Note body exceeds {MAX_BODY} characters")

    # Cap live (unresolved + unexpired) notes per org.
    now = datetime.now(UTC)
    live_count_result = await session.exec(
        select(func.count(OwnerNote.id)).where(
            OwnerNote.org_id == body.org_id,
            OwnerNote.resolved_at.is_(None),
            (OwnerNote.expires_at.is_(None)) | (OwnerNote.expires_at > now),
        )
    )
    live_count = live_count_result.one()
    if live_count >= MAX_LIVE_PER_ORG:
        raise HTTPException(
            status_code=422,
            detail=f"Workspace has reached the limit of {MAX_LIVE_PER_ORG} live notes. "
                   "Delete or expire some before adding more.",
        )

    note = OwnerNote(
        org_id=body.org_id,
        created_by=current_user.id,
        body=body.body.strip(),
        agent_ids=body.agent_ids or None,
        expires_at=body.expires_at,
        agent_resolvable=body.agent_resolvable,
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return _to_out(note)


@router.patch("/{note_id}")
async def update_note(
    note_id: UUID,
    body: NoteUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NoteOut:
    note = await _get_note(session, current_user, note_id)

    if body.body is not None:
        stripped = body.body.strip()
        if not stripped:
            raise HTTPException(status_code=422, detail="Note body cannot be empty")
        if len(stripped) > MAX_BODY:
            raise HTTPException(status_code=422, detail=f"Note body exceeds {MAX_BODY} characters")
        note.body = stripped
    if body.agent_ids is not None:
        note.agent_ids = body.agent_ids or None
    if body.expires_at is not None:
        note.expires_at = body.expires_at
    if body.agent_resolvable is not None:
        note.agent_resolvable = body.agent_resolvable

    note.updated_at = datetime.now(UTC)
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return _to_out(note)


@router.post("/{note_id}/reopen", status_code=200)
async def reopen_note(
    note_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NoteOut:
    """Clear resolved fields so the note becomes live again."""
    note = await _get_note(session, current_user, note_id)
    note.resolved_at = None
    note.resolved_by = None
    note.resolution = ""
    note.updated_at = datetime.now(UTC)
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return _to_out(note)


@router.delete("/{note_id}", status_code=204)
async def delete_note(
    note_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    note = await _get_note(session, current_user, note_id)
    await session.delete(note)
    await session.commit()
