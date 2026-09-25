"""Unit tests for the data retention / PII scrub logic in prune_sessions.

All tests use in-memory mocks — no database required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _org(retention_days: int | None = 30, scrub_only: bool = False):
    org = MagicMock()
    org.id = uuid.uuid4()
    org.data_retention_days = retention_days
    org.scrub_content_only = scrub_only
    return org


def _session(age_days: int = 40):
    s = MagicMock()
    s.id = uuid.uuid4()
    s.started_at = datetime.now(UTC) - timedelta(days=age_days)
    return s


def _make_db(orgs: list, sessions: list | None = None):
    """Build a minimal AsyncSession mock that returns the given orgs/sessions."""
    db = AsyncMock()

    call_count = 0

    async def _exec(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            # First call: select(Organization)
            result.all = MagicMock(return_value=orgs)
        else:
            # Subsequent calls: select(AgentSession) or select(AgentSession.id)
            result.all = MagicMock(return_value=sessions or [])
        return result

    db.exec = _exec
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    return db


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_orgs_with_policy_does_nothing():
    """When no org has a retention policy, nothing is deleted or committed."""
    db = _make_db(orgs=[])
    from app.core.triggers.dispatch import prune_sessions

    counts = await prune_sessions(db)
    assert counts == {"deleted": 0, "scrubbed": 0}
    db.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_mode_removes_sessions():
    """Hard-delete mode: old sessions are deleted entirely."""
    org = _org(retention_days=30, scrub_only=False)
    old = [_session(age_days=40), _session(age_days=60)]
    db = _make_db(orgs=[org], sessions=old)

    from app.core.triggers.dispatch import prune_sessions

    counts = await prune_sessions(db)
    assert counts["deleted"] == 2
    assert counts["scrubbed"] == 0
    assert db.delete.call_count == 2
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_delete_mode_no_old_sessions():
    """Hard-delete mode with no sessions older than cutoff does nothing."""
    org = _org(retention_days=30, scrub_only=False)
    db = _make_db(orgs=[org], sessions=[])

    from app.core.triggers.dispatch import prune_sessions

    counts = await prune_sessions(db)
    assert counts == {"deleted": 0, "scrubbed": 0}
    db.delete.assert_not_called()


@pytest.mark.asyncio
async def test_scrub_mode_records_scrubbed_count():
    """Scrub mode: counts how many session IDs were found for scrubbing."""
    org = _org(retention_days=7, scrub_only=True)
    old_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    db = AsyncMock()
    call_count = 0

    async def _exec(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.all = MagicMock(return_value=[org])
        else:
            result.all = MagicMock(return_value=old_ids)
        return result

    db.exec = _exec
    db.commit = AsyncMock()

    from app.core.triggers.dispatch import prune_sessions

    counts = await prune_sessions(db)
    assert counts["scrubbed"] == 3
    assert counts["deleted"] == 0
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_keep_forever_org_skipped():
    """An org with data_retention_days=None is skipped even if it has old sessions."""
    # The SELECT only returns orgs WHERE data_retention_days IS NOT NULL,
    # so we model that by returning an empty org list.
    db = _make_db(orgs=[], sessions=[_session(age_days=999)])

    from app.core.triggers.dispatch import prune_sessions

    counts = await prune_sessions(db)
    assert counts == {"deleted": 0, "scrubbed": 0}
    db.delete.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_orgs_aggregated():
    """Counts are aggregated across multiple orgs in the same run."""
    org_a = _org(retention_days=30, scrub_only=False)
    org_b = _org(retention_days=90, scrub_only=False)
    # We'll pretend each org has 1 old session.
    sessions_per_org = [_session(age_days=100)]

    db = AsyncMock()
    call_count = 0

    async def _exec(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.all = MagicMock(return_value=[org_a, org_b])
        else:
            result.all = MagicMock(return_value=sessions_per_org)
        return result

    db.exec = _exec
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    from app.core.triggers.dispatch import prune_sessions

    counts = await prune_sessions(db)
    # 1 session deleted per org = 2 total
    assert counts["deleted"] == 2
    assert db.delete.call_count == 2
