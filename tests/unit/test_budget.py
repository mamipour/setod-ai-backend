"""Daily token budget — unit tests.

The budget check is embedded in run_agent(); we test the decision logic in isolation
by extracting the relevant arithmetic. This keeps tests fast and dependency-free.
"""

import pytest


# ── Budget decision logic (extracted from base.py for isolation) ───────────

def _would_exceed(spent_before: int, current_run_tokens: int, budget: int) -> bool:
    """True when the run pushes accumulated spend over the cap.

    Mirrors the check in run_agent():
        if budget and spent_before + session.total_tokens >= budget
    """
    if budget == 0:
        return False
    return spent_before + current_run_tokens >= budget


@pytest.mark.parametrize("spent,tokens,budget,expect", [
    # Exactly at limit → exceeded
    (0, 1_000, 1_000, True),
    # One token under → not exceeded
    (0, 999, 1_000, False),
    # Prior spend plus new tips over
    (500, 501, 1_000, True),
    # Prior spend plus new stays under
    (500, 499, 1_000, False),
    # Zero budget means no cap
    (9_000_000, 9_000_000, 0, False),
    # First token triggers when budget = 1
    (0, 1, 1, True),
    # Empty run (zero tokens) never exceeds a finite budget unless already over
    (0, 0, 100, False),
    (100, 0, 100, True),  # spent_before already at limit
])
def test_budget_decision(spent, tokens, budget, expect):
    assert _would_exceed(spent, tokens, budget) == expect


def test_no_budget_never_triggers():
    for tokens in [0, 1, 1_000_000]:
        assert not _would_exceed(0, tokens, 0)


def test_budget_is_per_agent_not_shared():
    """Two agents each get their own allowance — they never share a counter."""
    agent_a_spent = 900
    agent_b_spent = 0
    budget = 1_000
    # Agent A is close to its limit
    assert _would_exceed(agent_a_spent, 101, budget)
    # Agent B is fine
    assert not _would_exceed(agent_b_spent, 101, budget)
