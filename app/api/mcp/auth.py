"""Personal-access-token authentication for the MCP server."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import ApiToken, ApiTokenScope, MemberRole, OrganizationMember, User

TOKEN_PREFIX = "setod_pat_"
MAX_ACTIVE_TOKENS = 10
ALLOWED_EXPIRY_DAYS = {30, 90, 365, None}


class McpAuthError(Exception):
    """Missing, unknown, revoked, or expired token. HTTP 401."""


class McpForbidden(Exception):
    """Token is valid but the user is no longer in the workspace. HTTP 403."""


@dataclass
class McpPrincipal:
    user: User
    org_id: object
    token: ApiToken
    scope: ApiTokenScope
    is_owner: bool


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint_token() -> tuple[str, str, str]:
    """Return (plaintext, sha256 hex, 8-char display prefix). Plaintext is shown once."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw), raw[len(TOKEN_PREFIX):][:8]


def hashes_match(stored: str, computed: str) -> bool:
    return hmac.compare_digest(stored, computed)


async def authenticate(session: AsyncSession, authorization: str | None) -> McpPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise McpAuthError("Unauthorized")
    raw = authorization.split(" ", 1)[1].strip()
    if not raw.startswith(TOKEN_PREFIX):
        raise McpAuthError("Unauthorized")
    digest = hash_token(raw)
    row = (await session.exec(select(ApiToken).where(ApiToken.token_hash == digest))).first()
    if row is None or not hashes_match(row.token_hash, digest):
        raise McpAuthError("Unauthorized")
    now = datetime.now(UTC)
    if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
        raise McpAuthError("Unauthorized")

    user = await session.get(User, row.user_id)
    if user is None or user.token_version != row.token_version_at_creation:
        raise McpAuthError("Unauthorized")

    membership = (await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == row.org_id,
            OrganizationMember.user_id == user.id,
        )
    )).first()
    if membership is None:
        raise McpForbidden("Not a member of this workspace")

    cutoff = now - timedelta(seconds=60)
    await session.execute(
        update(ApiToken)
        .where(ApiToken.id == row.id)
        .where(or_(ApiToken.last_used_at.is_(None), ApiToken.last_used_at < cutoff))
        .values(last_used_at=now)
    )
    await session.commit()

    return McpPrincipal(
        user=user,
        org_id=row.org_id,
        token=row,
        scope=row.scope,
        is_owner=membership.role == MemberRole.owner,
    )
