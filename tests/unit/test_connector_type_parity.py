"""Parity test: Python ConnectorType enum must match the TypeScript union.

This test ensures that when a new connector type is added to the Python enum,
the developer is reminded to update the TypeScript union in platform-ui/src/lib/api.ts
(and vice versa).

If this test fails after adding a new connector, update BOTH:
  - platform/app/db/models.py :: ConnectorType
  - platform-ui/src/lib/api.ts :: ConnectorType (line ~87)
"""

import re
from pathlib import Path

import pytest

from app.db.models import ConnectorType


def _read_ts_union() -> frozenset[str]:
    """Parse the TypeScript ConnectorType union literal from api.ts."""
    api_ts = Path(__file__).parents[3] / "platform-ui" / "src" / "lib" / "api.ts"
    if not api_ts.exists():
        pytest.skip(f"api.ts not found at {api_ts}")

    content = api_ts.read_text()
    # Match: export type ConnectorType = "a" | "b" | ...
    match = re.search(
        r'export\s+type\s+ConnectorType\s*=\s*(["\w\s|]+)',
        content,
    )
    if not match:
        pytest.fail("Could not find ConnectorType type in api.ts")

    raw = match.group(1)
    values = re.findall(r'"([^"]+)"', raw)
    return frozenset(values)


def _python_connector_types() -> frozenset[str]:
    return frozenset(ct.value for ct in ConnectorType)


def test_connector_type_parity():
    """Python ConnectorType enum values must exactly match the TypeScript union."""
    py_types = _python_connector_types()
    ts_types = _read_ts_union()

    only_in_python = py_types - ts_types
    only_in_ts = ts_types - py_types

    messages = []
    if only_in_python:
        messages.append(
            f"In Python but NOT in TypeScript union: {sorted(only_in_python)}\n"
            "  → Add to ConnectorType in platform-ui/src/lib/api.ts"
        )
    if only_in_ts:
        messages.append(
            f"In TypeScript but NOT in Python enum: {sorted(only_in_ts)}\n"
            "  → Add to ConnectorType in platform/app/db/models.py"
        )

    if messages:
        pytest.fail(
            "ConnectorType parity check failed:\n\n" + "\n\n".join(messages)
        )


def test_connector_type_values_are_lowercase_snake_case():
    """Connector type values must be lowercase snake_case to match DB column values."""
    bad = [v for ct in ConnectorType if not (v := ct.value).islower() or v != v.replace("-", "_")]
    assert not bad, f"Non-snake_case connector types: {bad}"
