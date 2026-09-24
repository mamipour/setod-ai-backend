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
from app.core import notify as _notify
from app.core.workspace import load_web_settings, load_notify_settings, save_web_settings, save_notify_settings
from app.db.models import Connector, ConnectorType, Organization, OrganizationMember, User
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
