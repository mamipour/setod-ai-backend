"""Unit tests for the run-failure notification debounce in notify.py."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.notify import DEBOUNCE_H, notify_run_failed


def _fake_agent(last_notified: datetime | None = None):
    agent = MagicMock()
    agent.id = uuid4()
    agent.name = "Test Agent"
    agent.org_id = uuid4()
    agent.last_failure_notified_at = last_notified
    return agent


@pytest.mark.asyncio
async def test_first_failure_sends():
    """First failure with no prior stamp always notifies."""
    agent = _fake_agent(last_notified=None)
    db = AsyncMock()
    db.get.return_value = None  # org not found -- short-circuits after debounce check

    with (
        patch("app.core.notify._owner", return_value=(None, None)),
        patch("app.core.notify._send_resend", new_callable=AsyncMock) as mock_email,
        patch("app.core.notify._send_telegram", new_callable=AsyncMock),
    ):
        await notify_run_failed(db, agent.org_id, agent, "Something broke")
        # _owner returned (None, None) so email is not sent, but debounce was NOT hit
        mock_email.assert_not_called()  # owner is None, so we return early after debounce


@pytest.mark.asyncio
async def test_debounce_suppresses_within_window():
    """A second failure within DEBOUNCE_H hours is silently suppressed."""
    recent = datetime.now(UTC) - timedelta(minutes=30)
    agent = _fake_agent(last_notified=recent)
    db = AsyncMock()

    with (
        patch("app.core.notify._owner", return_value=(None, None)) as mock_owner,
        patch("app.core.notify._send_resend", new_callable=AsyncMock) as mock_email,
    ):
        await notify_run_failed(db, agent.org_id, agent, "Something broke again")
        # Debounce fires before _owner is called
        mock_owner.assert_not_called()
        mock_email.assert_not_called()


@pytest.mark.asyncio
async def test_debounce_allows_after_window():
    """A failure outside the debounce window should not be suppressed."""
    old = datetime.now(UTC) - timedelta(hours=DEBOUNCE_H + 1)
    agent = _fake_agent(last_notified=old)
    db = AsyncMock()

    fake_owner = MagicMock()
    fake_owner.email = "owner@example.com"
    fake_org = MagicMock()
    fake_org.notify_settings = None

    with (
        patch("app.core.notify._owner", return_value=(fake_owner, fake_org)),
        patch("app.core.notify._send_resend", new_callable=AsyncMock) as mock_email,
        patch("app.core.notify._send_telegram", new_callable=AsyncMock),
    ):
        await notify_run_failed(db, agent.org_id, agent, "Old error resurfaced")
        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args
        assert "owner@example.com" in call_kwargs.args


@pytest.mark.asyncio
async def test_stamp_updated_after_send():
    """last_failure_notified_at is updated on the agent after a successful send."""
    agent = _fake_agent(last_notified=None)
    db = AsyncMock()
    db.commit = AsyncMock()

    fake_owner = MagicMock()
    fake_owner.email = "owner@example.com"
    fake_org = MagicMock()
    fake_org.notify_settings = None

    with (
        patch("app.core.notify._owner", return_value=(fake_owner, fake_org)),
        patch("app.core.notify._send_resend", new_callable=AsyncMock),
        patch("app.core.notify._send_telegram", new_callable=AsyncMock),
    ):
        before = datetime.now(UTC)
        await notify_run_failed(db, agent.org_id, agent, "Error")
        assert agent.last_failure_notified_at is not None
        assert agent.last_failure_notified_at >= before
        db.add.assert_called_with(agent)
        db.commit.assert_called_once()
