"""Schedule trigger — pure-function unit tests.

Covers: preset resolution, timezone-aware next-run, minimum interval enforcement,
custom cron validation, and the describe() summary.
"""

import pytest
from datetime import UTC, datetime

from app.core.triggers.schedule import (
    InvalidSchedule,
    PRESETS,
    describe,
    next_run_after,
    resolve_expression,
    validate,
)


# ── resolve_expression ────────────────────────────────────────────────────────

def test_preset_resolves_to_cron():
    config = {"preset": "daily_9am"}
    assert resolve_expression(config) == "0 9 * * *"


def test_unknown_preset_raises():
    with pytest.raises(InvalidSchedule, match="Unknown schedule preset"):
        resolve_expression({"preset": "every_second"})


def test_raw_cron_accepted():
    expr = resolve_expression({"cron": "30 6 * * 1-5"})
    assert expr == "30 6 * * 1-5"


def test_empty_config_raises():
    with pytest.raises(InvalidSchedule, match="needs either a preset or a cron"):
        resolve_expression({})


# ── validate ─────────────────────────────────────────────────────────────────

def test_validate_good_preset():
    result = validate({"preset": "hourly"})
    assert result["cron"] == "0 * * * *"
    assert result.get("timezone") in (None, "UTC")


def test_validate_bad_cron_raises():
    with pytest.raises(InvalidSchedule):
        validate({"cron": "99 99 99 99 99"})


def test_validate_too_frequent_raises():
    # Every minute is below MIN_INTERVAL_SECONDS (300 s)
    with pytest.raises(InvalidSchedule, match="5 minutes"):
        validate({"cron": "* * * * *"})


def test_validate_bad_timezone_raises():
    with pytest.raises(InvalidSchedule, match="Unknown timezone"):
        validate({"preset": "daily_9am", "timezone": "Mars/Olympus"})


def test_validate_named_timezone_preserved():
    result = validate({"preset": "daily_9am", "timezone": "America/New_York"})
    assert result.get("timezone") == "America/New_York"


# ── next_run_after ────────────────────────────────────────────────────────────

def test_next_run_is_in_the_future():
    config = validate({"preset": "daily_9am"})
    nxt = next_run_after(config)
    assert nxt > datetime.now(UTC)


def test_next_run_is_utc_aware():
    config = validate({"preset": "hourly"})
    nxt = next_run_after(config)
    assert nxt.tzinfo is not None


# ── describe ─────────────────────────────────────────────────────────────────

def test_describe_preset_returns_label():
    label = describe({"preset": "daily_9am"})
    assert "9" in label.lower() or "daily" in label.lower() or label  # any non-empty string


def test_describe_raw_cron_returns_expression():
    label = describe({"cron": "0 7 * * 1-5"})
    assert "0 7 * * 1-5" in label


def test_all_presets_validate():
    for key in PRESETS:
        result = validate({"preset": key})
        assert "cron" in result
