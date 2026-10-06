"""MCP server: guide extraction, protocol, and the gates that do not need a database."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.agents._assist import _ASSIST_SYSTEM
from app.api.mcp.auth import hash_token, mint_token
from app.api.mcp.protocol import dispatch
from app.api.mcp.tools.write import _attach_connector, _run, publish_refusal, validation_problems
from app.api.mcp.tools.write import AttachConnectorArgs, RunArgs
from app.db.models import Agent, AgentStatus, AgentTool, ApiTokenScope, Connector, ConnectorStatus, ConnectorType, SessionStatus
from app.docs.guide import SECTIONS, load_guide

INTEGRATIONS = Path(__file__).parents[2] / "app" / "integrations"
NAME_RE = re.compile(r'(?:tool_name|n)\(\s*"([a-z0-9_]+)"\s*\)|name\s*=\s*"([a-z0-9_]+)"')
SKIP = {"object", "string", "integer", "boolean", "array", "number"}

ORIGINAL_HEADINGS = [
    "## What you have access to",
    "## The setod ecosystem — what you know",
    "## What each tool actually returns — read this before writing any prompt",
    "## Research tools (you can use these yourself)",
    "## Diagnosing a run",
    "## URL monitoring protocol",
    "## When a goal needs a connector the agent does not have",
    "## When the goal is genuinely not possible",
    "## Your job",
    "## Output rules",
]


class _Principal:
    def __init__(self, *, scope=ApiTokenScope.read, owner=False, org_id=None):
        self.user = SimpleNamespace(id=uuid4())
        self.org_id = org_id or uuid4()
        self.token = SimpleNamespace(id=uuid4(), token_prefix="abcd1234")
        self.scope = scope
        self.is_owner = owner


class _Session:
    def __init__(self, agent=None):
        self.agent = agent
        self.refreshed = False

    async def get(self, model, ident):
        return self.agent

    async def refresh(self, obj):
        self.refreshed = True


def _rpc(method, params=None, req_id=1, drop_id=False):
    body = {"jsonrpc": "2.0", "method": method}
    if not drop_id:
        body["id"] = req_id
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode()


async def _call(raw, principal=None, session=None, header="2025-03-26"):
    return await dispatch(raw, principal or _Principal(), session or _Session(), header)


def test_assist_prompt_keeps_every_heading():
    for heading in ORIGINAL_HEADINGS:
        assert _ASSIST_SYSTEM.count(heading) == 1
    assert "code_" in _ASSIST_SYSTEM


def test_load_guide_sections():
    assert "read_unread_emails" in load_guide("tools")
    with pytest.raises(KeyError):
        load_guide("nope")
    whole = load_guide("all")
    for key, filename in SECTIONS.items():
        first = (Path(__file__).parents[2] / "app/docs/guide" / filename).read_text().splitlines()[0]
        assert first in whole


def test_guide_names_every_connector_tool():
    tools = load_guide("tools")
    platform = load_guide("platform")
    from app.integrations.registry import BUILDERS
    for connector_type in BUILDERS:
        assert connector_type.value in platform
    for path in INTEGRATIONS.rglob("*.py"):
        if path.name in {"mcp.py", "__init__.py", "base.py", "registry.py"}:
            continue
        text = path.read_text()
        if "ToolSpec" not in text:
            continue
        for match in NAME_RE.finditer(text):
            name = match.group(1) or match.group(2)
            if name and name not in SKIP:
                assert name in tools, name


def test_http_rejects_disabled_missing_auth_and_oversized_body():
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    client = TestClient(app)
    was = settings.mcp_server_enabled
    settings.mcp_server_enabled = False
    try:
        disabled = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert disabled.status_code == 503
        settings.mcp_server_enabled = True
        missing = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert missing.status_code == 401
        assert missing.headers.get("www-authenticate") == 'Bearer realm="setod"'
        huge = client.post("/mcp", content=b"x" * 300_000, headers={"authorization": "Bearer setod_pat_x"})
        assert huge.status_code == 413
        assert client.get("/mcp").status_code == 405
    finally:
        settings.mcp_server_enabled = was


def test_mint_token_shape():
    raw, digest, prefix = mint_token()
    assert raw.startswith("setod_pat_")
    assert len(raw) == 53
    assert digest == hash_token(raw)
    assert len(digest) == 64
    assert raw[len("setod_pat_"):].startswith(prefix)


@pytest.mark.asyncio
async def test_initialize_negotiates_version():
    resp = await _call(_rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}), header="2025-06-18")
    body = json.loads(resp.body)
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["instructions"]
    resp = await _call(_rpc("initialize", {"protocolVersion": "1999-01-01"}), header="1999-01-01")
    assert json.loads(resp.body)["result"]["protocolVersion"] == "2025-03-26"


@pytest.mark.asyncio
async def test_tools_list_hides_writes_from_read_tokens():
    resp = await _call(_rpc("tools/list"))
    tools = json.loads(resp.body)["result"]["tools"]
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)
    assert all("setod_publish_agent" != t["name"] for t in tools)
    owner = _Principal(scope=ApiTokenScope.write, owner=True)
    resp = await _call(_rpc("tools/list"), owner)
    publish = next(t for t in json.loads(resp.body)["result"]["tools"] if t["name"] == "setod_publish_agent")
    assert publish["annotations"]["destructiveHint"] is True


@pytest.mark.asyncio
async def test_write_tool_refused_for_read_token():
    raw = _rpc("tools/call", {"name": "setod_publish_agent", "arguments": {
        "agent_id": str(uuid4()), "confirm": "PUBLISH",
    }})
    body = json.loads((await _call(raw)).body)
    assert body["error"]["code"] == -32003


@pytest.mark.asyncio
async def test_guide_tool_version_and_unknown_section():
    raw = _rpc("tools/call", {"name": "setod_get_guide", "arguments": {"section": "tools", "skill_version": 0}})
    text = json.loads((await _call(raw)).body)["result"]["content"][0]["text"]
    assert text.startswith("<!-- guide_version:")
    assert "current is v" in text
    raw = _rpc("tools/call", {"name": "setod_get_guide", "arguments": {"section": "x"}})
    result = json.loads((await _call(raw)).body)["result"]
    assert result["isError"] is True
    assert "platform" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_resources_round_trip():
    listed = json.loads((await _call(_rpc("resources/list"))).body)["result"]["resources"]
    assert len(listed) == len(SECTIONS)
    assert all(item["uri"].startswith("setod://guide/") for item in listed)
    uri = listed[0]["uri"]
    got = json.loads((await _call(_rpc("resources/read", {"uri": uri}))).body)["result"]["contents"][0]["text"]
    assert got


@pytest.mark.asyncio
async def test_protocol_edges():
    batch = json.loads((await _call(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode())).body)
    assert batch["error"]["code"] == -32600
    unknown = json.loads((await _call(_rpc("nope"))).body)
    assert unknown["error"]["code"] == -32601
    bad = json.loads((await _call(_rpc("tools/call", {"name": 1}))).body)
    assert bad["error"]["code"] == -32602
    note = await _call(_rpc("notifications/initialized", drop_id=True))
    assert note.status_code == 202
    assert note.body == b""
    prompts = json.loads((await _call(_rpc("prompts/list"))).body)
    assert prompts["result"] == {"prompts": []}
    raw = _rpc("tools/call", {"name": "setod_get_guide"})
    assert json.loads((await _call(raw)).body)["result"]["isError"] is False


@pytest.mark.asyncio
async def test_publish_refusal_requires_succeeded_dry_run():
    now = datetime.now(UTC)
    agent = Agent(org_id=uuid4(), created_by=uuid4(), name="t", instructions="x" * 50)
    agent.updated_at = now - timedelta(minutes=10)
    stale = SimpleNamespace(dry_run=True, status=SessionStatus.succeeded, started_at=now - timedelta(minutes=20))
    assert "Refused" in await publish_refusal(agent, [stale], agent.updated_at, now)
    fresh = SimpleNamespace(dry_run=True, status=SessionStatus.succeeded, started_at=now - timedelta(minutes=5))
    assert await publish_refusal(agent, [fresh], agent.updated_at, now) is None
    # A failed run, even a recent dry run, does not open the gate.
    failed = SimpleNamespace(dry_run=True, status=SessionStatus.error, started_at=now - timedelta(minutes=5))
    assert await publish_refusal(agent, [failed], agent.updated_at, now)


def test_validation_flags_missing_model_and_empty_tools():
    agent = Agent(org_id=uuid4(), created_by=uuid4(), name="t", instructions="")
    problems = validation_problems(agent, [], [], {}, [], {})
    assert "no_model" in {p["code"] for p in problems}
    connector = Connector(org_id=agent.org_id, type=ConnectorType.twilio, name="sms", status=ConnectorStatus.active)
    tool = AgentTool(agent_id=agent.id, connector_id=connector.id, enabled_tools=[])
    agent.instructions = "x" * 50
    agent.model_connector_id = uuid4()
    problems = validation_problems(agent, [tool], [], {}, [], {connector.id: connector})
    assert "empty_enabled_tools" in {p["code"] for p in problems}
    healthy = Agent(org_id=uuid4(), created_by=uuid4(), name="t", instructions="y" * 50, model_connector_id=uuid4())
    healthy.settings = {"web_search": True}
    problems = validation_problems(healthy, [], [], {}, [SimpleNamespace(enabled=True)], {})
    assert problems == [] or not any(p["blocks"] for p in problems)
    assert not any(p["blocks"] for p in problems)


@pytest.mark.asyncio
async def test_published_attach_requires_apply_live():
    org = uuid4()
    agent = Agent(org_id=org, created_by=uuid4(), name="t", instructions="x" * 50, status=AgentStatus.published)
    principal = _Principal(scope=ApiTokenScope.write, owner=True, org_id=org)
    with pytest.raises(Exception) as caught:
        await _attach_connector(principal, _Session(agent), AttachConnectorArgs(agent_id=agent.id, connector_id=uuid4()))
    assert "APPLY LIVE" in str(caught.value)


@pytest.mark.asyncio
async def test_run_defaults_to_dry_run(monkeypatch):
    org = uuid4()
    agent = Agent(org_id=org, created_by=uuid4(), name="t", instructions="x" * 50, published_config={"instructions": "x"})
    seen = {}

    async def fake_run(session, agent, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(id=uuid4(), status=SessionStatus.succeeded, dry_run=kwargs["dry_run"], total_tokens=3, error=None)

    async def fake_trace(*args, **kwargs):
        return "TRACE"

    monkeypatch.setattr("app.api.mcp.tools.write.run_agent", fake_run)
    monkeypatch.setattr("app.api.mcp.tools.write.run_trace", fake_trace)
    principal = _Principal(scope=ApiTokenScope.write, owner=True, org_id=org)
    session = _Session(agent)
    text = await _run(principal, session, RunArgs(agent_id=agent.id))
    assert seen["dry_run"] is True
    assert seen["use_published"] is False
    assert "TRACE" in text

    seen.clear()
    with pytest.raises(Exception) as caught:
        await _run(principal, session, RunArgs(agent_id=agent.id, dry_run=False))
    assert "RUN LIVE" in str(caught.value)
    assert seen == {}

    await _run(principal, session, RunArgs(agent_id=agent.id, dry_run=False, confirm="RUN LIVE"))
    assert seen["dry_run"] is False
