"""Approval gate — unit tests.

Tests the logic that determines which tool calls require human approval before
execution. The gate check is a set intersection; we test that here in isolation
and test the aliasing edge case (two same-type connectors).
"""

import pytest

from app.integrations.base import slug


# ── Gate check logic ──────────────────────────────────────────────────────────
#
# From registry.build_tools_for_agent():
#     if agent_tool.approval_tools:
#         gated_bases = set(agent_tool.approval_tools)
#         for t in built:
#             if _base_name(t.spec.name, alias) in gated_bases:
#                 approval_required.add(t.spec.name)
#
# _base_name strips the alias suffix to recover the canonical name:
#     send_email_sales  →  send_email   (alias="Sales")
#     send_email        →  send_email   (no alias)

def _base_name(name: str, alias: str) -> str:
    """Mirror of registry._base_name for isolated testing."""
    suffix = f"_{slug(alias)}" if alias else ""
    return name[: -len(suffix)] if suffix and name.endswith(suffix) else name


# ── _base_name ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,alias,expected_base", [
    ("send_email_sales", "Sales", "send_email"),
    ("send_email", "", "send_email"),
    ("send_email", "Sales", "send_email"),   # doesn't end in _sales — left as-is
    ("send_sms_support", "Support", "send_sms"),
    ("read_telegram_messages_alice", "Alice", "read_telegram_messages"),
    ("send_email_alice_smith", "Alice Smith", "send_email"),
])
def test_base_name(name, alias, expected_base):
    assert _base_name(name, alias) == expected_base


# ── Gate membership ──────────────────────────────────────────────────────────

def _is_gated(tool_name: str, alias: str, gated_bases: set[str]) -> bool:
    return _base_name(tool_name, alias) in gated_bases


def test_gated_tool_requires_approval():
    assert _is_gated("send_email", "", {"send_email"})


def test_ungated_tool_passes():
    assert not _is_gated("read_unread_emails", "", {"send_email", "send_sms"})


def test_aliased_tool_gated_via_base():
    # "send_email" is in gated_bases; "send_email_sales" should be caught
    assert _is_gated("send_email_sales", "Sales", {"send_email"})


def test_aliased_read_not_gated():
    assert not _is_gated("read_unread_emails_sales", "Sales", {"send_email"})


def test_empty_gated_set_nothing_gated():
    for tool in ["send_email", "send_sms", "archive_email"]:
        assert not _is_gated(tool, "", set())


def test_all_write_tools_gated():
    write_tools = ["send_email", "send_sms", "archive_email", "send_telegram_message"]
    gated = set(write_tools)
    for t in write_tools:
        assert _is_gated(t, "", gated)
    # Read tool not in the set
    assert not _is_gated("read_unread_emails", "", gated)


def test_aliased_and_unaliased_same_base():
    """When an agent has two Gmail accounts, both send_email_sales and send_email_support
    should be caught by gating the base name 'send_email'."""
    gated = {"send_email"}
    assert _is_gated("send_email_sales", "Sales", gated)
    assert _is_gated("send_email_support", "Support", gated)
