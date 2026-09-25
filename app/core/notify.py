"""Platform-level owner notifications.

Events
------
- approval_pending   An agent paused waiting for the owner to approve a tool call.
- run_failed         A session finished with an error. Debounced: first failure fires
                     immediately, then at most once per DEBOUNCE_H hours per agent.
- budget_reached     An agent stopped because it hit its daily token budget.
- connector_revoked  A connector was marked revoked (token refresh failed, etc.).

Channels (in priority order)
-----------------------------
1. Telegram Client connector -> send_message("me", text) -> owner's Saved Messages.
   Requires the org to have a telegram_client connector and the owner to have
   set it as their preferred notification channel in workspace settings.
2. Resend email -> owner's login email address (settings.resend_api_key).
   Falls back silently when the key is blank (dev / test environments).

Both channels are attempted independently.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.core.crypto import decrypt_json
from app.core.workspace import load_notify_settings
from app.db.models import (
    Agent,
    Connector,
    ConnectorType,
    MemberRole,
    Organization,
    OrganizationMember,
    User,
)

log = logging.getLogger(__name__)

DEBOUNCE_H = 1  # minimum hours between run_failed notifications per agent
FROM_EMAIL = "Setod <notifications@setod.com>"  # overridden at runtime by settings.resend_from_email


# -- Internal helpers ----------------------------------------------------------

async def _owner(db: AsyncSession, org_id: UUID) -> tuple[User | None, Organization | None]:
    """Return the workspace owner and their org row."""
    org = await db.get(Organization, org_id)
    if org is None:
        return None, None
    result = await db.exec(
        select(User)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.role == MemberRole.owner,
        )
        .limit(1)
    )
    return result.first(), org


async def _send_resend(to: str, subject: str, body_text: str) -> None:
    """Send a plain-text email via Resend. Raises on failure so callers can surface errors."""
    if not settings.resend_api_key:
        raise RuntimeError(
            "RESEND_API_KEY is not configured. Add it to the server .env file to enable email notifications."
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.resend_from_email,
                    "to": [to],
                    "subject": subject,
                    "text": body_text,
                },
            )
            if resp.status_code not in (200, 201):
                log.error("Resend error %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.error("Resend send failed: %s", exc)


async def _send_telegram(db: AsyncSession, connector_id: UUID, message: str) -> None:
    """Send a message to the owner's Telegram Saved Messages via their Client connector."""
    connector = await db.get(Connector, connector_id)
    if connector is None or connector.type != ConnectorType.telegram_client or not connector.config:
        log.warning("Telegram notify: connector %s not usable", connector_id)
        return
    try:
        config = decrypt_json(connector.config)
        # Local import avoids a circular dependency at module load time.
        from app.integrations.telegram import client_send
        await client_send(config, "me", message)
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram notify failed (connector %s): %s", connector_id, exc)


def _link(path: str) -> str:
    """Absolute URL pointing at the platform frontend."""
    origin = settings.frontend_origin.rstrip("/")
    return f"{origin}{path}"


# -- Public API ----------------------------------------------------------------

async def notify_approval_pending(
    db: AsyncSession,
    org_id: UUID,
    agent_name: str,
    tool_summary: str,
) -> None:
    """Fire when an agent pauses waiting for approval."""
    owner, org = await _owner(db, org_id)
    if owner is None or org is None:
        return

    subject = f'[Action required] {agent_name} is waiting for your approval'
    body = (
        f'Your agent "{agent_name}" paused and needs you to approve an action '
        f'before it can continue.\n\n'
        f'Action: {tool_summary}\n\n'
        f'Review it here: {_link("/approvals")}'
    )
    ns = load_notify_settings(org)

    if tg_id := ns.get("telegram_connector_id"):
        await _send_telegram(db, UUID(tg_id), body)

    await _send_resend(owner.email, subject, body)


async def notify_run_failed(
    db: AsyncSession,
    org_id: UUID,
    agent: Agent,
    error: str,
) -> None:
    """Fire when a run finishes with an error. Debounced to at most once per DEBOUNCE_H hours."""
    now = datetime.now(UTC)
    if agent.last_failure_notified_at is not None:
        elapsed = now - agent.last_failure_notified_at
        if elapsed < timedelta(hours=DEBOUNCE_H):
            log.debug(
                "notify_run_failed suppressed for agent %s (last sent %s ago)",
                agent.id, elapsed,
            )
            return

    owner, org = await _owner(db, org_id)
    if owner is None or org is None:
        return

    subject = f'[Warning] {agent.name} run failed'
    body = (
        f'Your agent "{agent.name}" encountered an error during its last run.\n\n'
        f'Error: {error}\n\n'
        f'Check the run log: {_link(f"/agents/{agent.id}?tab=Runs")}'
    )
    ns = load_notify_settings(org)

    if tg_id := ns.get("telegram_connector_id"):
        await _send_telegram(db, UUID(tg_id), body)

    await _send_resend(owner.email, subject, body)

    # Stamp *after* sending so a send failure still retries on the next run.
    agent.last_failure_notified_at = now
    db.add(agent)
    await db.commit()


async def notify_budget_reached(
    db: AsyncSession,
    org_id: UUID,
    agent: Agent,
    budget: int,
) -> None:
    """Fire when an agent stops because it hit its daily token budget."""
    owner, org = await _owner(db, org_id)
    if owner is None or org is None:
        return

    subject = f'[Warning] {agent.name} hit its daily token budget'
    body = (
        f'Your agent "{agent.name}" stopped because it reached its daily limit '
        f'of {budget:,} tokens.\n\n'
        f'It will not run again until midnight UTC. '
        f'To increase the limit:\n{_link(f"/agents/{agent.id}?tab=Settings")}'
    )
    ns = load_notify_settings(org)

    if tg_id := ns.get("telegram_connector_id"):
        await _send_telegram(db, UUID(tg_id), body)

    await _send_resend(owner.email, subject, body)


async def notify_connector_revoked(
    db: AsyncSession,
    org_id: UUID,
    connector_name: str,
) -> None:
    """Fire when a connector's token refresh fails and its status becomes revoked."""
    owner, org = await _owner(db, org_id)
    if owner is None or org is None:
        return

    subject = f'[Action required] {connector_name} connector needs to be reconnected'
    body = (
        f'The "{connector_name}" connector lost access and all agents using it '
        f'have stopped receiving its tools.\n\n'
        f'Reconnect it here: {_link("/connectors")}'
    )
    ns = load_notify_settings(org)

    if tg_id := ns.get("telegram_connector_id"):
        await _send_telegram(db, UUID(tg_id), body)

    await _send_resend(owner.email, subject, body)
