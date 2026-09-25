"""ToolContext and slug — unit tests.

Tests the in-memory parts of ToolContext (note_seen, tool_name aliasing, seen list)
and the slug() helper that keeps tool names provider-safe.  No database required.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.integrations.base import ToolContext, slug


# ── slug ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("Sales Team", "sales_team"),
    ("support@acme.com", "support_acme_com"),
    ("My  Double  Space", "my_double_space"),
    ("123numbers", "123numbers"),
    ("", ""),
    ("trailing_", "trailing"),
    ("__leading", "leading"),
])
def test_slug(value, expected):
    assert slug(value) == expected


def test_slug_truncates_to_24():
    long = "a" * 100
    assert len(slug(long)) <= 24


# ── ToolContext.tool_name ──────────────────────────────────────────────────────

def _make_ctx(alias: str = "") -> ToolContext:
    connector = MagicMock()
    connector.id = uuid4()
    return ToolContext(db=MagicMock(), agent_id=uuid4(), connector=connector, alias=alias)


def test_tool_name_no_alias():
    ctx = _make_ctx(alias="")
    assert ctx.tool_name("send_email") == "send_email"


def test_tool_name_with_alias():
    ctx = _make_ctx(alias="Sales")
    assert ctx.tool_name("send_email") == "send_email_sales"


def test_tool_name_alias_normalised():
    ctx = _make_ctx(alias="My Account!")
    name = ctx.tool_name("send_email")
    # Must match ^[a-zA-Z0-9_-]+$
    assert all(c.isalnum() or c in "_-" for c in name)


# ── ToolContext.note_seen ──────────────────────────────────────────────────────

def test_note_seen_appends_to_seen():
    ctx = _make_ctx()
    assert ctx.seen == []
    ctx.note_seen("msg_001")
    ctx.note_seen("msg_002")
    external_ids = [eid for _, eid in ctx.seen]
    assert external_ids == ["msg_001", "msg_002"]


def test_note_seen_records_connector_id():
    ctx = _make_ctx()
    ctx.note_seen("x")
    connector_id, _ = ctx.seen[0]
    assert connector_id == ctx.connector.id


def test_note_seen_duplicate_still_appends():
    """Deduplication is the responsibility of the flush step, not note_seen."""
    ctx = _make_ctx()
    ctx.note_seen("dup")
    ctx.note_seen("dup")
    assert len(ctx.seen) == 2


def test_multiple_contexts_independent():
    ctx_a = _make_ctx()
    ctx_b = _make_ctx()
    ctx_a.note_seen("a_only")
    assert len(ctx_b.seen) == 0
