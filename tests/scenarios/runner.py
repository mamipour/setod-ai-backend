"""Scenario harness — runs YAML test cases against the agent loop with a fake LLM.

Each YAML file in tests/scenarios/*.yaml describes:
  - name:           human-readable label
  - template:       key matching a Template in app.core.agents.templates
  - input:          trigger message injected as user_input
  - expected_tools: ordered list of tool names the loop must call

The harness wires up stub integrations (dry_run=True, no real DB, no real API
keys) and drives the agent loop with FakeLLMClient scripted to call exactly the
expected tools in order.  The test passes when the loop completes without error
and every expected tool was invoked.

Run from the platform root::

    pytest tests/scenarios/ -v

Or to run a single scenario file::

    pytest tests/scenarios/test_harness.py::test_scenario[support_triage] -v
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import yaml
import pytest

from app.core.agents.base import RegisteredTool, run_agent
from app.core.agents.templates import TEMPLATES as _TEMPLATES

TEMPLATE_REGISTRY = {t.key: t for t in _TEMPLATES}
from app.core.llm.client import LLMResponse, LLMError, ToolCall, ToolSpec
from app.db.models import (
    Agent,
    AgentSession,
    AgentStatus,
    Connector,
    ConnectorStatus,
    ConnectorType,
    DEFAULT_AGENT_SETTINGS,
    TriggerType,
)
from tests.scenarios.fake_llm import FakeLLMClient

# ── Load all scenario YAML files ──────────────────────────────────────────────

SCENARIOS_DIR = Path(__file__).parent


def _load_scenarios() -> list[dict[str, Any]]:
    result = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        with open(path) as f:
            data = yaml.safe_load(f)
        data["_path"] = path.stem
        result.append(data)
    return result


ALL_SCENARIOS = _load_scenarios()
SCENARIO_IDS = [s["_path"] for s in ALL_SCENARIOS]


# ── Stub tool builders ────────────────────────────────────────────────────────

def _stub_tool(name: str, description: str = "") -> RegisteredTool:
    """Create a no-op tool that records calls for assertion."""
    from app.core.llm.client import ToolSpec

    async def handler(args: dict[str, Any], dry_run: bool) -> str:
        return f"[stub] {name} called with {args}"

    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=description or f"Stub tool: {name}",
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        handler=handler,
    )


def _build_stub_tools(expected_tools: list[str]) -> list[RegisteredTool]:
    """Build a stub tool for every tool in expected_tools plus a few extras."""
    # Common extras so the agent's system prompt features don't cause unknown-tool errors
    extras = ["web_search", "recall_memory", "resolve_note"]
    all_names = list(dict.fromkeys(expected_tools + extras))
    return [_stub_tool(name) for name in all_names]


# ── Fake DB session ───────────────────────────────────────────────────────────

def _make_db() -> AsyncMock:
    """Minimal AsyncSession stub that satisfies run_agent's DB calls."""
    db = AsyncMock()

    # run_agent calls db.add(), db.commit(), db.refresh() — these must all be awaitable.
    db.add = MagicMock()  # synchronous in SQLModel
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.exec = AsyncMock(return_value=AsyncMock(all=MagicMock(return_value=[])))
    db.get = AsyncMock(return_value=None)

    # After db.refresh(session), the session needs an id.
    async def _refresh(obj: Any) -> None:
        if isinstance(obj, AgentSession) and not obj.id:
            obj.id = uuid.uuid4()
        if isinstance(obj, AgentSession) and not obj.started_at:
            obj.started_at = datetime.now(UTC)

    db.refresh.side_effect = _refresh
    return db


# ── Agent fixture ─────────────────────────────────────────────────────────────

def _make_agent(template_key: str) -> Agent:
    org_id = uuid.uuid4()
    agent = Agent(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"test_{template_key}",
        instructions="",
        status=AgentStatus.draft,
    )
    agent.published_config = None  # force draft mode
    return agent


# ── Scenario test ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=SCENARIO_IDS)
@pytest.mark.asyncio
async def test_scenario(scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a YAML scenario through the agent loop with a fake LLM and stub tools."""
    expected = scenario.get("expected_tools", [])
    fake_client = FakeLLMClient(tool_sequence=list(expected))
    stub_tools = _build_stub_tools(expected)
    db = _make_db()
    agent = _make_agent(scenario["template"])

    # Patch _build_client_for so run_agent uses our fake client instead of hitting the DB.
    import app.core.agents.base as base_module
    monkeypatch.setattr(base_module, "_build_client_for", AsyncMock(return_value=fake_client))

    # Patch build_tools_for_agent so no real connector lookups happen.
    import app.integrations.registry as reg_module
    monkeypatch.setattr(
        reg_module,
        "build_tools_for_agent",
        AsyncMock(return_value=(stub_tools, [], set())),
    )

    # Patch internal helpers that query the DB so the fake DB never needs to be
    # realistic for those calls.
    monkeypatch.setattr(base_module, "_tokens_used_today", AsyncMock(return_value=0))
    monkeypatch.setattr(base_module, "_recall", AsyncMock(return_value=""))
    monkeypatch.setattr(base_module, "_record", AsyncMock(return_value=0))
    monkeypatch.setattr(base_module, "_finish", AsyncMock())

    # Patch knowledge / notes / websearch so nothing tries to talk to a DB.
    import app.core.knowledge as knowledge_module
    import app.core.notes as notes_module
    monkeypatch.setattr(knowledge_module, "build_tool", AsyncMock(return_value=None))
    monkeypatch.setattr(notes_module, "build_tool", AsyncMock(return_value=None))
    monkeypatch.setattr(notes_module, "live_notes", AsyncMock(return_value=[]))

    # Patch the skills DB query — return empty skill list.
    monkeypatch.setattr(
        base_module,
        "_run_skills_query",
        AsyncMock(return_value=[]),
    ) if hasattr(base_module, "_run_skills_query") else None

    # Make db.exec().all() return [] for skills and notes queries.
    db.exec.return_value = AsyncMock(all=MagicMock(return_value=[]), one=MagicMock(return_value=0), first=MagicMock(return_value=None))

    session = await run_agent(
        db,
        agent,
        trigger_type=TriggerType.manual,
        user_input=scenario["input"],
        dry_run=True,
        use_published=False,
    )

    assert session is not None, "run_agent returned None"
    # Every expected tool must have been called by the fake client.
    assert fake_client.calls_made == expected, (
        f"Tool call mismatch for '{scenario['name']}':\n"
        f"  expected: {expected}\n"
        f"  got:      {fake_client.calls_made}"
    )
