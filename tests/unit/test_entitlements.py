"""Unit tests for Entitlements.included_quantity override lookup (M5 fix)."""

import json
import pytest

from app.core.billing.entitlements import Entitlements


def make_ent(included: dict, overrides: dict) -> Entitlements:
    return Entitlements(
        plan_code="test",
        included=included,
        _overrides={k: json.dumps(v) for k, v in overrides.items()},
    )


class TestIncludedQuantity:
    def test_returns_plan_value_when_no_override(self):
        ent = make_ent(included={"voice_minutes": 100.0}, overrides={})
        assert ent.included_quantity("voice_minutes") == 100.0

    def test_returns_zero_when_nothing_configured(self):
        ent = make_ent(included={}, overrides={})
        assert ent.included_quantity("voice_minutes") == 0.0

    def test_suffixed_override_takes_priority(self):
        ent = make_ent(
            included={"voice_minutes": 100.0},
            overrides={"voice_minutes_included": 500.0},
        )
        assert ent.included_quantity("voice_minutes") == 500.0

    def test_bare_override_used_when_no_suffixed(self):
        ent = make_ent(
            included={"voice_minutes": 100.0},
            overrides={"voice_minutes": 250.0},
        )
        assert ent.included_quantity("voice_minutes") == 250.0

    def test_suffixed_beats_bare_override(self):
        ent = make_ent(
            included={"voice_minutes": 100.0},
            overrides={
                "voice_minutes_included": 500.0,
                "voice_minutes": 250.0,
            },
        )
        assert ent.included_quantity("voice_minutes") == 500.0

    def test_integer_override_coerces_to_float(self):
        ent = make_ent(
            included={},
            overrides={"voice_minutes_included": 300},
        )
        assert ent.included_quantity("voice_minutes") == 300.0
        assert isinstance(ent.included_quantity("voice_minutes"), float)

    def test_different_meter_keys_dont_clash(self):
        ent = make_ent(
            included={"rows": 50000.0, "voice_minutes": 120.0},
            overrides={"rows_included": 999.0},
        )
        assert ent.included_quantity("rows") == 999.0
        assert ent.included_quantity("voice_minutes") == 120.0
