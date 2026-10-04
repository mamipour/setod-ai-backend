"""
Conversations API
=================
Endpoints for the conversation thread view and human takeover.

Phase 2: media serving endpoint only.
Phase 3: list, thread, patch (status), and manual reply.
"""
from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.db.models import (
    Conversation,
    ConversationMessage,
    ConversationStatus,
    MessageAuthor,
    MessageDirection,
    MessageKind,
    OrganizationMember,
    User,
)
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _assert_org_access(
    db: AsyncSession,
    user: User,
    org_id: UUID,
) -> None:
    member = await db.exec(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.organization_id == org_id,
        )
    )
    if member.first() is None:
        raise HTTPException(status_code=403, detail="Access denied")


# ── Phase 2: media serving ────────────────────────────────────────────────────

@router.get(
    "/{conversation_id}/media/{message_id}/{idx}",
    summary="Serve a stored media attachment",
)
async def serve_media(
    conversation_id: UUID,
    message_id: UUID,
    idx: int,
    db: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Return the raw bytes of a stored media attachment.

    The response Content-Type is set from the attachment's `mime` field.
    Returns 404 when the conversation/message does not exist or the file is not stored.
    """
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await _assert_org_access(db, current_user, conv.org_id)

    msg = await db.get(ConversationMessage, message_id)
    if msg is None or msg.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found")

    attachments = msg.attachments or []
    if idx < 0 or idx >= len(attachments):
        raise HTTPException(status_code=404, detail="Attachment index out of range")

    att = attachments[idx]
    stored_path = att.get("stored_path", "")
    if not stored_path:
        raise HTTPException(status_code=404, detail="Media not yet stored")

    try:
        from app.core import media_store
        data = media_store.get(stored_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Media file not found on disk")

    mime = att.get("mime", "application/octet-stream")
    return Response(content=data, media_type=mime)


# ── Phase 3: list + thread + patch + reply (stubs) ────────────────────────────

@router.get("", summary="List conversations for an org")
async def list_conversations(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = 50,
    offset: int = 0,
):
    await _assert_org_access(db, current_user, org_id)
    rows = await db.exec(
        select(Conversation)
        .where(Conversation.org_id == org_id)
        .order_by(Conversation.last_inbound_at.desc())
        .offset(offset)
        .limit(limit)
    )
    convs = rows.all()
    return [
        {
            "id": str(c.id),
            "channel": c.channel,
            "peer_id": c.peer_id,
            "peer_name": c.peer_name,
            "thread_key": c.thread_key,
            "status": c.status.value,
            "last_inbound_at": c.last_inbound_at.isoformat() if c.last_inbound_at else None,
            "last_outbound_at": c.last_outbound_at.isoformat() if c.last_outbound_at else None,
            "created_at": c.created_at.isoformat(),
        }
        for c in convs
    ]


@router.get("/{conversation_id}", summary="Get a conversation thread")
async def get_conversation(
    conversation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = 50,
    offset: int = 0,
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await _assert_org_access(db, current_user, conv.org_id)

    rows = await db.exec(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    messages = rows.all()

    return {
        "conversation": {
            "id": str(conv.id),
            "channel": conv.channel,
            "peer_id": conv.peer_id,
            "peer_name": conv.peer_name,
            "status": conv.status.value,
            "summary": conv.summary,
            "created_at": conv.created_at.isoformat(),
        },
        "messages": [
            {
                "id": str(m.id),
                "direction": m.direction.value,
                "author": m.author.value,
                "kind": m.kind.value,
                "text": m.text,
                "attachments": m.attachments,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
    }


@router.patch("/{conversation_id}", summary="Update conversation status (human takeover / reopen)")
async def patch_conversation(
    conversation_id: UUID,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await _assert_org_access(db, current_user, conv.org_id)

    new_status = body.get("status")
    if new_status:
        try:
            conv.status = ConversationStatus(new_status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid status: {new_status!r}")

    db.add(conv)
    await db.commit()
    return {"id": str(conv.id), "status": conv.status.value}


@router.post("/{conversation_id}/messages", summary="Send a manual reply (human takeover)")
async def send_manual_reply(
    conversation_id: UUID,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Post a reply to the conversation as the human owner.

    This does NOT send the message via the provider channel — it records it in the thread so
    the owner can document manual replies made directly in the channel app.
    Sending is out of scope for this endpoint.
    """
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await _assert_org_access(db, current_user, conv.org_id)

    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    from app.core.conversations import record_outbound
    from app.db.models import Connector

    connector = await db.get(Connector, conv.connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    msg_id = await record_outbound(
        db,
        connector=connector,
        conversation_id=conversation_id,
        peer_id=conv.peer_id,
        text=text,
        author=MessageAuthor.human,
        dry_run=False,
    )
    return {"id": str(msg_id) if msg_id else None, "status": "recorded"}
