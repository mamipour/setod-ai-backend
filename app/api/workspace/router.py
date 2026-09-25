"""Workspace-level settings not tied to a specific connector or agent.

Access is org-member level, matching the connectors API: LLM keys are already
manageable by any member, so the search key follows the same rule.
"""
from typing import Annotated
from uuid import UUID

import httpx
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user, require_owner
from app.core import notify as _notify
from app.core.workspace import load_web_settings, load_notify_settings, save_web_settings, save_notify_settings
from app.db.models import Connector, ConnectorType, Invitation, MemberRole, Organization, OrganizationMember, User
from app.db.session import get_session
from app.integrations.websearch import TAVILY_SEARCH_URL

router = APIRouter(prefix="/workspace", tags=["workspace"])


async def _get_org_as_member(
    session: AsyncSession, user: User, org_id: str
) -> Organization:
    try:
        oid = UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid org_id")

    mem = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == oid,
            OrganizationMember.user_id == user.id,
        )
    )
    if not mem.first():
        raise HTTPException(status_code=403, detail="Not a member of this organisation")

    org = await session.get(Organization, oid)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return org


# ── Web search settings ────────────────────────────────────────────────────────

class WebSearchSettings(BaseModel):
    provider: str           # "duckduckgo" | "tavily"
    tavily_key_set: bool    # True when a Tavily key is stored (key itself never returned)


class WebSearchUpdate(BaseModel):
    provider: str                      # "duckduckgo" | "tavily"
    tavily_api_key: str | None = None  # omit/None to leave unchanged; "" to clear


class TestResult(BaseModel):
    ok: bool
    detail: str


def _to_response(settings: dict) -> WebSearchSettings:
    return WebSearchSettings(
        provider=settings.get("provider", "duckduckgo"),
        tavily_key_set=bool(settings.get("tavily_api_key")),
    )


@router.get("/{org_id}/web-search", response_model=WebSearchSettings)
async def get_web_search(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    org = await _get_org_as_member(session, current_user, org_id)
    return _to_response(load_web_settings(org))


@router.put("/{org_id}/web-search", response_model=WebSearchSettings)
async def update_web_search(
    org_id: str,
    body: WebSearchUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    if body.provider not in ("duckduckgo", "tavily"):
        raise HTTPException(status_code=400, detail="provider must be 'duckduckgo' or 'tavily'")

    org = await _get_org_as_member(session, current_user, org_id)
    settings = load_web_settings(org)

    # Agent runs treat a stored key as "Tavily active", so provider and key must move
    # together: choosing DuckDuckGo drops the key, and choosing Tavily requires one.
    if body.provider == "duckduckgo":
        settings.pop("tavily_api_key", None)
    elif body.tavily_api_key:
        settings["tavily_api_key"] = body.tavily_api_key
    elif not settings.get("tavily_api_key"):
        raise HTTPException(status_code=400, detail="A Tavily API key is required to use Tavily")
    settings["provider"] = body.provider

    save_web_settings(org, settings)
    session.add(org)
    await session.commit()

    return _to_response(settings)


@router.post("/{org_id}/web-search/test", response_model=TestResult)
async def test_web_search(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    org = await _get_org_as_member(session, current_user, org_id)
    api_key = load_web_settings(org).get("tavily_api_key")

    if not api_key:
        return TestResult(ok=False, detail="No Tavily key configured.")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TAVILY_SEARCH_URL,
                json={"query": "test", "max_results": 1, "search_depth": "basic"},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        if resp.status_code == 401:
            return TestResult(ok=False, detail="Invalid API key — Tavily returned 401.")
        if resp.status_code == 429:
            return TestResult(ok=False, detail="Rate limit reached — key is valid but quota exceeded.")
        if not resp.is_success:
            return TestResult(ok=False, detail=f"Tavily returned HTTP {resp.status_code}.")
        return TestResult(ok=True, detail="Connection successful.")
    except httpx.HTTPError as exc:
        return TestResult(ok=False, detail=f"Request failed: {type(exc).__name__}")


# -- Notification settings ----------------------------------------------------

class NotifySettings(BaseModel):
    telegram_connector_id: str | None = None


@router.get("/{org_id}/notify", response_model=NotifySettings)
async def get_notify_settings(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    org = await _get_org_as_member(session, current_user, org_id)
    ns = load_notify_settings(org)
    return NotifySettings(telegram_connector_id=ns.get("telegram_connector_id"))


@router.patch("/{org_id}/notify", response_model=NotifySettings)
async def update_notify_settings(
    org_id: str,
    body: NotifySettings,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    org = await _get_org_as_member(session, current_user, org_id)

    if body.telegram_connector_id:
        connector = await session.get(Connector, body.telegram_connector_id)
        if connector is None or connector.org_id != org.id:
            raise HTTPException(status_code=404, detail="Connector not found in this workspace")
        if connector.type != ConnectorType.telegram_client:
            raise HTTPException(status_code=422, detail="Only a Telegram Account connector can be used for notifications")

    ns: dict = {"telegram_connector_id": body.telegram_connector_id}
    save_notify_settings(org, ns)
    session.add(org)
    await session.commit()
    return NotifySettings(**ns)


@router.post("/{org_id}/notify/test", response_model=TestResult)
async def test_notify(
    org_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    from uuid import UUID as _UUID
    org = await _get_org_as_member(session, current_user, org_id)
    ns = load_notify_settings(org)

    sent_via: list[str] = []
    errors: list[str] = []

    if tg_id := ns.get("telegram_connector_id"):
        try:
            connector = await session.get(Connector, tg_id)
            if connector and connector.config:
                from app.core.crypto import decrypt_json
                from app.integrations.telegram import client_send
                await client_send(decrypt_json(connector.config), "me", "Setod test notification — your alerts are working.")
                sent_via.append("Telegram")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Telegram: {exc}")

    try:
        await _notify._send_resend(
            current_user.email,
            "Setod test notification",
            "Your notification settings are working. You will receive alerts here for approvals, run failures, and budget limits.",
        )
        sent_via.append("email")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Email: {exc}")

    if errors and not sent_via:
        return TestResult(ok=False, detail="; ".join(errors))
    channels = " and ".join(sent_via) if sent_via else "no channels"
    return TestResult(ok=True, detail=f"Test sent via {channels}.")


# ── Members ───────────────────────────────────────────────────────────────────
# All write operations require owner role; listing is open to all members.

class MemberOut(BaseModel):
    user_id: UUID
    email: str
    name: str
    role: str


class InviteBody(BaseModel):
    email: str
    role: str = "member"  # "owner" | "member"


class RoleBody(BaseModel):
    role: str  # "owner" | "member"


@router.get("/{org_id}/members", response_model=list[MemberOut])
async def list_members(
    org_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[MemberOut]:
    """List all members of the workspace (any member may call this)."""
    # Verify caller is a member
    membership = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == current_user.id,
        )
    )
    if membership.first() is None:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")

    rows = await session.exec(
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == org_id)
        .order_by(OrganizationMember.joined_at)
    )
    return [
        MemberOut(
            user_id=m.user_id,
            email=u.email,
            name=u.name,
            role=m.role.value,
        )
        for m, u in rows.all()
    ]


@router.post("/{org_id}/members/invite", status_code=201)
async def invite_member(
    org_id: UUID,
    body: InviteBody,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Send an invitation email. If the address is already a member, returns 409."""
    # Check not already a member
    existing_user = await session.exec(select(User).where(User.email == body.email))
    target_user = existing_user.first()
    if target_user:
        already = await session.exec(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == target_user.id,
            )
        )
        if already.first():
            raise HTTPException(status_code=409, detail="This address is already a member")

    # Check no pending invitation
    pending = await session.exec(
        select(Invitation).where(
            Invitation.organization_id == org_id,
            Invitation.email == body.email,
            Invitation.accepted_at.is_(None),  # type: ignore[attr-defined]
        )
    )
    if pending.first():
        raise HTTPException(status_code=409, detail="An invitation is already pending for this address")

    role = MemberRole.owner if body.role == "owner" else MemberRole.member
    invite = Invitation(
        organization_id=org_id,
        email=body.email,
        role=role,
        invited_by_id=current_user.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    session.add(invite)
    await session.commit()

    # Send the invitation email
    org = await session.get(Organization, org_id)
    org_name = org.name if org else "your workspace"

    accept_url = f"https://setod.com/accept-invite?token={invite.token}"
    try:
        await _notify._send_resend(
            body.email,
            f"{current_user.name} invited you to {org_name} on Setod",
            (
                f"Hi,\n\n{current_user.name} ({current_user.email}) has invited you to join "
                f"{org_name} on Setod as a {role.value}.\n\n"
                f"Accept your invitation:\n{accept_url}\n\n"
                "This link expires in 7 days. If you don't have an account, "
                "sign up with this email address at https://setod.com first, "
                "then click the link."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — email failure must not block the invite record
        import logging
        logging.getLogger(__name__).warning("Invitation email failed: %s", exc)

    return {"ok": True, "detail": f"Invitation sent to {body.email}"}


@router.patch("/{org_id}/members/{user_id}/role")
async def change_member_role(
    org_id: UUID,
    user_id: UUID,
    body: RoleBody,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MemberOut:
    """Change a member's role. An owner cannot demote themselves."""
    if user_id == current_user.id:
        raise HTTPException(status_code=422, detail="You cannot change your own role")

    membership = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = membership.first()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    new_role = MemberRole.owner if body.role == "owner" else MemberRole.member
    member.role = new_role
    session.add(member)
    await session.commit()

    user = await session.get(User, user_id)
    return MemberOut(user_id=user_id, email=user.email, name=user.name, role=new_role.value)


@router.delete("/{org_id}/members/{user_id}", status_code=204)
async def remove_member(
    org_id: UUID,
    user_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove a member from the workspace. An owner cannot remove themselves."""
    if user_id == current_user.id:
        raise HTTPException(status_code=422, detail="You cannot remove yourself")

    membership = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = membership.first()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    await session.delete(member)
    await session.commit()


# ── Data retention ─────────────────────────────────────────────────────────────

ALLOWED_RETENTION_DAYS = {7, 30, 90, 180, 365}


class RetentionOut(BaseModel):
    data_retention_days: int | None
    scrub_content_only: bool


class RetentionBody(BaseModel):
    data_retention_days: int | None  # None = keep forever
    scrub_content_only: bool = False


@router.get("/{org_id}/retention", response_model=RetentionOut)
async def get_retention(
    org_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Return this workspace's data-retention policy."""
    await _get_org_as_member(session, current_user, org_id)
    org = await session.get(Organization, org_id)
    return RetentionOut(
        data_retention_days=org.data_retention_days,
        scrub_content_only=org.scrub_content_only,
    )


@router.patch("/{org_id}/retention", response_model=RetentionOut)
async def update_retention(
    org_id: UUID,
    body: RetentionBody,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Update this workspace's data-retention policy. Owners only."""
    if body.data_retention_days is not None and body.data_retention_days not in ALLOWED_RETENTION_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"data_retention_days must be one of {sorted(ALLOWED_RETENTION_DAYS)} or null.",
        )
    org = await session.get(Organization, org_id)
    org.data_retention_days = body.data_retention_days
    org.scrub_content_only = body.scrub_content_only
    org.updated_at = datetime.now(UTC)
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return RetentionOut(
        data_retention_days=org.data_retention_days,
        scrub_content_only=org.scrub_content_only,
    )


# ── Workspace CRUD ────────────────────────────────────────────────────────────

class CreateOrgBody(BaseModel):
    name: str


class OrgOut(BaseModel):
    id: UUID
    name: str
    slug: str


class RenameOrgBody(BaseModel):
    name: str


@router.post("/", response_model=OrgOut, status_code=201)
async def create_workspace(
    body: CreateOrgBody,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Create a new organisation. The caller becomes its owner."""
    import re, secrets as _secrets
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be blank")

    # Generate a URL-safe slug, then make it unique with a short suffix.
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    slug = f"{base_slug}-{_secrets.token_hex(3)}"

    from app.db.models import Skill
    from app.core.agents.skills import DEFAULT_SKILLS

    org = Organization(name=name, slug=slug)
    session.add(org)
    await session.flush()  # get org.id

    session.add(OrganizationMember(
        organization_id=org.id,
        user_id=current_user.id,
        role=MemberRole.owner,
    ))

    # Seed default skills like the sign-up flow does.
    for s in DEFAULT_SKILLS:
        session.add(Skill(
            org_id=org.id,
            key=s.key,
            name=s.name,
            tagline=s.tagline,
            category=s.category,
            content=s.content,
            is_default=True,
        ))

    await session.commit()
    await session.refresh(org)
    return OrgOut(id=org.id, name=org.name, slug=org.slug)


@router.patch("/{org_id}", response_model=OrgOut)
async def rename_workspace(
    org_id: UUID,
    body: RenameOrgBody,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Rename a workspace. Owners only."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be blank")
    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    org.name = name
    org.updated_at = datetime.now(UTC)
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return OrgOut(id=org.id, name=org.name, slug=org.slug)


# ── Invitation management ─────────────────────────────────────────────────────

class InvitationOut(BaseModel):
    id: UUID
    email: str
    role: str
    created_at: datetime
    expires_at: datetime
    accepted: bool


@router.get("/{org_id}/invitations", response_model=list[InvitationOut])
async def list_invitations(
    org_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """List all invitations for a workspace (pending and accepted). Owners only."""
    rows = await session.exec(
        select(Invitation)
        .where(Invitation.organization_id == org_id)
        .order_by(Invitation.created_at.desc())
    )
    return [
        InvitationOut(
            id=inv.id,
            email=inv.email,
            role=inv.role.value,
            created_at=inv.created_at,
            expires_at=inv.expires_at,
            accepted=inv.accepted_at is not None,
        )
        for inv in rows.all()
    ]


@router.delete("/{org_id}/invitations/{invitation_id}", status_code=204)
async def withdraw_invitation(
    org_id: UUID,
    invitation_id: UUID,
    current_user: Annotated[User, Depends(require_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Cancel / withdraw a pending invitation. Owners only."""
    inv = await session.get(Invitation, invitation_id)
    if inv is None or inv.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=409, detail="Invitation already accepted")
    await session.delete(inv)
    await session.commit()


# ── Leave workspace ───────────────────────────────────────────────────────────

@router.delete("/{org_id}/members/me", status_code=204)
async def leave_workspace(
    org_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Leave a workspace. The last owner cannot leave (would orphan the org)."""
    membership = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == current_user.id,
        )
    )
    member = membership.first()
    if member is None:
        raise HTTPException(status_code=404, detail="Not a member of this workspace")

    if member.role == MemberRole.owner:
        # Count other owners — can't leave if you're the last one.
        other_owners = await session.exec(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == MemberRole.owner,
                OrganizationMember.user_id != current_user.id,
            )
        )
        if other_owners.first() is None:
            raise HTTPException(
                status_code=409,
                detail="You are the only owner. Transfer ownership or delete the workspace first.",
            )

    await session.delete(member)
    await session.commit()
