import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.api.auth.dependencies import create_access_token, decode_access_token
from app.config import settings
from app.core.agents.skills import DEFAULT_SKILLS
from app.db.models import Invitation, MemberRole, Organization, OrganizationMember, Skill, User
from app.db.session import get_session

router = APIRouter(prefix="/auth", tags=["auth"])

# ── OAuth client setup ────────────────────────────────────────────────────────

def _get_oauth() -> OAuth:
    """Build OAuth client at request time so credentials are always current."""
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


async def _get_or_create_user(session: AsyncSession, google_user: dict) -> User:
    result = await session.exec(select(User).where(User.google_id == google_user["sub"]))
    user = result.first()

    if user:
        user.name = google_user.get("name", user.name)
        user.avatar_url = google_user.get("picture", user.avatar_url)
        user.updated_at = datetime.now(UTC)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    # New user — create user + default org
    user = User(
        google_id=google_user["sub"],
        email=google_user["email"],
        name=google_user.get("name", google_user["email"]),
        avatar_url=google_user.get("picture"),
    )
    session.add(user)
    await session.flush()  # get user.id before org creation

    org_name = f"{user.name.split()[0]}'s Workspace"
    base_slug = _slugify(org_name)

    # Ensure slug uniqueness
    slug = base_slug
    suffix = 1
    while True:
        exists = await session.exec(select(Organization).where(Organization.slug == slug))
        if not exists.first():
            break
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    org = Organization(name=org_name, slug=slug)
    session.add(org)
    await session.flush()

    # Seed default skills for the new org so users have a ready-made library.
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

    membership = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role=MemberRole.owner,
    )
    session.add(membership)

    await session.commit()
    await session.refresh(user)
    return user


async def _accept_pending_invitations(session: AsyncSession, user: User) -> None:
    """Auto-accept pending invitations matching the user's email on sign-in."""
    result = await session.exec(
        select(Invitation).where(
            Invitation.email == user.email,
            Invitation.accepted_at.is_(None),  # type: ignore[attr-defined]
        )
    )
    invitations = result.all()

    for inv in invitations:
        if inv.is_expired:
            continue

        already_member = await session.exec(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == inv.organization_id,
                OrganizationMember.user_id == user.id,
            )
        )
        if already_member.first():
            continue

        session.add(OrganizationMember(
            organization_id=inv.organization_id,
            user_id=user.id,
            role=inv.role,
            invited_by_id=inv.invited_by_id,
        ))
        inv.accepted_at = datetime.now(UTC)
        session.add(inv)

    await session.commit()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/google/login")
async def google_login(request: Request):
    return await _get_oauth().google.authorize_redirect(request, settings.google_redirect_uri)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    token = await _get_oauth().google.authorize_access_token(request)
    google_user = token.get("userinfo")

    if not google_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google auth failed")

    user = await _get_or_create_user(session, google_user)
    await _accept_pending_invitations(session, user)

    access_token = create_access_token(user.id)

    response = Response(status_code=status.HTTP_302_FOUND)
    response.headers["location"] = f"{settings.frontend_origin}/dashboard"
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        domain=settings.cookie_domain,
        max_age=60 * settings.jwt_access_token_expire_minutes,
    )
    return response


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(
        "access_token",
        domain=settings.cookie_domain,
        samesite="lax",
        secure=settings.is_production,
    )
    return {"ok": True}


@router.get("/me")
async def me(
    access_token: Annotated[str | None, Cookie()] = None,
    session: AsyncSession = Depends(get_session),
):
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user_id: UUID = decode_access_token(access_token)
    user = await session.get(User, user_id)

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    result = await session.exec(
        select(OrganizationMember, Organization)
        .join(Organization, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
    )
    memberships = result.all()

    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "organizations": [
            {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "role": member.role,
            }
            for member, org in memberships
        ],
    }
