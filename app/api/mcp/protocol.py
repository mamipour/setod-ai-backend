"""JSON-RPC for the six MCP methods we implement, plus the two empty ones clients probe."""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.mcp.auth import McpPrincipal
from app.api.mcp.tools.base import ToolError, annotations, input_schema
from app.db.models import ApiTokenScope, McpAuditEvent

log = logging.getLogger("setod.mcp")

SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_VERSION = "2025-03-26"
RESULT_CAP = 60_000
AUDIT_ARG_CAP = 8_000
WRITE_LIMIT_PER_HOUR = 60

INSTRUCTIONS = (
    "You are connected to a Setod workspace. Setod runs scheduled AI agents that use connectors "
    "(Gmail, Telegram, Twilio, MCP servers), prompt skills, and code skills. Before building or "
    "diagnosing anything, call setod_get_guide once (section \"all\" the first time, specific "
    "sections after). Use setod_get_agent and setod_read_run to see real state; never guess. "
    "Always dry-run (setod_run_agent with dry_run=true) before setod_publish_agent."
)


def negotiate(header: str | None) -> str:
    if header in SUPPORTED_VERSIONS:
        return header
    return DEFAULT_VERSION


def rpc_result(req_id, result, version: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "result": result},
        headers={"MCP-Protocol-Version": version},
    )


def rpc_error(req_id, code: int, message: str, *, status: int = 200, version: str = DEFAULT_VERSION) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status,
        headers={"MCP-Protocol-Version": version},
    )


def _cap(text: str) -> str:
    if len(text) <= RESULT_CAP:
        return text
    return text[:RESULT_CAP] + "\n…[truncated]"


def _audit_args(arguments: dict) -> dict:
    raw = json.dumps(arguments, default=str)
    if len(raw) <= AUDIT_ARG_CAP:
        return arguments
    return {"_truncated": True, "preview": raw[:2000]}


async def _over_write_limit(session: AsyncSession, principal: McpPrincipal) -> bool:
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    count = (await session.exec(
        select(func.count(McpAuditEvent.id)).where(
            McpAuditEvent.token_id == principal.token.id,
            McpAuditEvent.created_at >= cutoff,
        )
    )).one()
    return int(count or 0) >= WRITE_LIMIT_PER_HOUR


async def _audit(session: AsyncSession, principal: McpPrincipal, tool: str, arguments: dict, ok: bool, error: str | None, duration_ms: int) -> None:
    session.add(McpAuditEvent(
        token_id=principal.token.id,
        user_id=principal.user.id,
        org_id=principal.org_id,
        tool=tool,
        arguments=_audit_args(arguments),
        ok=ok,
        error=(error or "")[:2000] or None,
        duration_ms=duration_ms,
    ))
    await session.commit()


async def dispatch(raw: bytes, principal: McpPrincipal, session: AsyncSession, protocol_header: str | None) -> Response:
    from app.api.mcp.tools import REGISTRY

    version = negotiate(protocol_header)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return rpc_error(None, -32700, "Parse error", version=version)
    if isinstance(payload, list):
        return rpc_error(None, -32600, "Batches not supported", version=version)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
        req_id = payload.get("id") if isinstance(payload, dict) else None
        return rpc_error(req_id, -32600, "Invalid request", version=version)

    notification = "id" not in payload
    req_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params") if payload.get("params") is not None else {}

    if notification:
        return Response(status_code=202, headers={"MCP-Protocol-Version": version})

    if not isinstance(params, dict):
        return rpc_error(req_id, -32602, "Invalid params", version=version)

    if method == "initialize":
        asked = params.get("protocolVersion")
        chosen = asked if asked in SUPPORTED_VERSIONS else version
        return rpc_result(req_id, {
            "protocolVersion": chosen,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "setod", "version": "1"},
            "instructions": INSTRUCTIONS,
        }, chosen)

    if method == "ping":
        return rpc_result(req_id, {}, version)

    if method == "prompts/list":
        return rpc_result(req_id, {"prompts": []}, version)

    if method == "logging/setLevel":
        return rpc_result(req_id, {}, version)

    if method == "tools/list":
        visible = [
            t for t in REGISTRY.values()
            if not t.write or (principal.scope == ApiTokenScope.write and principal.is_owner)
        ]
        return rpc_result(req_id, {"tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": input_schema(t),
                "annotations": annotations(t),
            }
            for t in visible
        ]}, version)

    if method == "resources/list":
        from app.docs.guide import SECTIONS
        return rpc_result(req_id, {"resources": [
            {"uri": f"setod://guide/{key}", "name": key, "mimeType": "text/markdown"}
            for key in SECTIONS
        ]}, version)

    if method == "resources/read":
        from app.docs.guide import SECTIONS, load_guide
        uri = str(params.get("uri") or "")
        key = uri.removeprefix("setod://guide/")
        if key not in SECTIONS:
            return rpc_error(req_id, -32602, "Unknown resource", version=version)
        return rpc_result(req_id, {"contents": [{
            "uri": uri, "mimeType": "text/markdown", "text": load_guide(key),
        }]}, version)

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if params.get("arguments") is not None else {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return rpc_error(req_id, -32602, "Invalid params", version=version)
        tool = REGISTRY.get(name)
        if tool is None:
            return rpc_error(req_id, -32602, f"Unknown tool {name}", version=version)
        if tool.write and (principal.scope != ApiTokenScope.write or not principal.is_owner):
            return rpc_error(req_id, -32003, "This tool requires a write token held by a workspace owner", version=version)
        try:
            parsed: BaseModel = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            return rpc_error(req_id, -32602, exc.errors()[0].get("msg", "Invalid params"), version=version)
        if tool.audited and await _over_write_limit(session, principal):
            return rpc_error(req_id, -32029, "Rate limited", version=version)

        started = time.monotonic()
        ok, error, text = True, None, ""
        try:
            text = await tool.handler(principal, session, parsed)
        except ToolError as exc:
            ok, error, text = False, str(exc), str(exc)
        except Exception as exc:  # noqa: BLE001 — the model has to see the failure, not a 500
            log.exception("mcp tool %s failed", name)
            ok, error, text = False, str(exc), str(exc)
        duration_ms = int((time.monotonic() - started) * 1000)
        text = _cap(text)
        if tool.audited:
            await _audit(session, principal, name, arguments, ok, error, duration_ms)
        log.info("mcp tool=%s token=%s ms=%s error=%s", name, principal.token.token_prefix, duration_ms, not ok)
        return rpc_result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not ok,
        }, version)

    return rpc_error(req_id, -32601, "Method not found", version=version)
