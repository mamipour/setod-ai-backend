"""Workspace-level settings not tied to a specific connector or agent.

Access is org-member level, matching the connectors API: LLM keys are already
manageable by any member, so the search key follows the same rule.
"""
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.core.workspace import load_web_settings, save_web_settings
from app.db.models import Organization, OrganizationMember, User
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
