"""HTTP entry for the MCP server and the token-management routes the UI calls."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import assert_org_member, assert_org_owner, get_current_user
from app.api.mcp.auth import (
    ALLOWED_EXPIRY_DAYS,
    MAX_ACTIVE_TOKENS,
    McpAuthError,
    McpForbidden,
    authenticate,
    mint_token,
)
from app.api.mcp.protocol import DEFAULT_VERSION, dispatch
from app.config import settings
from app.db.models import ApiToken, ApiTokenScope, User
from app.db.session import AsyncSessionLocal, get_session
from app.docs.guide import GUIDE_VERSION
from slowapi.util import get_remote_address

from app.limiter import limiter

router = APIRouter(tags=["mcp"])


def _token_or_ip(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        from app.api.mcp.auth import hash_token
        return hash_token(header.split(" ", 1)[1].strip())
    return get_remote_address(request)


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="setod"', "MCP-Protocol-Version": DEFAULT_VERSION},
    )


def _forbidden() -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32003, "message": "Forbidden"}},
        status_code=403,
        headers={"MCP-Protocol-Version": DEFAULT_VERSION},
    )


@router.post("/mcp")
@limiter.limit("240/minute", key_func=_token_or_ip)
async def mcp_endpoint(request: Request):
    if not settings.mcp_server_enabled:
        raise HTTPException(status_code=503, detail="MCP server is disabled")
    body = await request.body()
    if len(body) > 262_144:
        raise HTTPException(status_code=413, detail="Request body too large")
    if not request.headers.get("authorization"):
        return _unauthorized()
    async with AsyncSessionLocal() as session:
        try:
            principal = await authenticate(session, request.headers.get("authorization"))
        except McpAuthError:
            return _unauthorized()
        except McpForbidden:
            return _forbidden()
        return await dispatch(body, principal, session, request.headers.get("mcp-protocol-version"))


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405)


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405)


# ── Token management (cookie auth, same as the rest of the API) ──────────────

class TokenCreate(BaseModel):
    org_id: UUID
    name: str
    scope: ApiTokenScope = ApiTokenScope.read
    expires_in_days: int | None = 90


class TokenOut(BaseModel):
    id: str
    name: str
    token_prefix: str
    scope: str
    expires_at: str | None
    last_used_at: str | None
    created_at: str


def _token_out(row: ApiToken) -> TokenOut:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None
    return TokenOut(
        id=str(row.id),
        name=row.name,
        token_prefix=row.token_prefix,
        scope=row.scope.value if isinstance(row.scope, ApiTokenScope) else str(row.scope),
        expires_at=iso(row.expires_at),
        last_used_at=iso(row.last_used_at),
        created_at=row.created_at.isoformat(),
    )


def _active(row: ApiToken, now: datetime) -> bool:
    if row.revoked_at is not None:
        return False
    return row.expires_at is None or row.expires_at > now


@router.get("/mcp/info")
async def mcp_info(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_member(session, current_user, org_id)
    return {"url": settings.mcp_public_url, "enabled": settings.mcp_server_enabled, "guide_version": GUIDE_VERSION}


@router.get("/mcp/tokens", response_model=list[TokenOut])
async def list_tokens(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_member(session, current_user, org_id)
    rows = (await session.exec(
        select(ApiToken).where(
            ApiToken.org_id == org_id,
            ApiToken.user_id == current_user.id,
            ApiToken.revoked_at.is_(None),
        ).order_by(ApiToken.created_at.desc())
    )).all()
    return [_token_out(r) for r in rows]


@router.post("/mcp/tokens", status_code=201)
@limiter.limit("10/hour")
async def create_token(
    request: Request,
    body: TokenCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_member(session, current_user, body.org_id)
    name = body.name.strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=422, detail="Token name must be 1–80 characters")
    if body.expires_in_days not in ALLOWED_EXPIRY_DAYS:
        raise HTTPException(status_code=422, detail="expires_in_days must be 30, 90, 365, or null")
    if body.scope == ApiTokenScope.write:
        await assert_org_owner(session, current_user, body.org_id)

    now = datetime.now(UTC)
    existing = (await session.exec(
        select(ApiToken).where(ApiToken.user_id == current_user.id, ApiToken.org_id == body.org_id)
    )).all()
    if sum(1 for row in existing if _active(row, now)) >= MAX_ACTIVE_TOKENS:
        raise HTTPException(status_code=409, detail="At most 10 active tokens per workspace")

    raw, digest, prefix = mint_token()
    row = ApiToken(
        user_id=current_user.id,
        org_id=body.org_id,
        name=name,
        token_hash=digest,
        token_prefix=prefix,
        scope=body.scope,
        token_version_at_creation=current_user.token_version,
        expires_at=None if body.expires_in_days is None else now + timedelta(days=body.expires_in_days),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    out = _token_out(row).model_dump()
    out["token"] = raw
    return out


@router.delete("/mcp/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    row = await session.get(ApiToken, token_id)
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Token not found")
    row.revoked_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
