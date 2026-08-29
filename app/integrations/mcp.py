"""
Remote MCP client
=================
Streamable HTTP only. The worker never launches a subprocess and never runs npx.

A connector's frozen `tools` list is the source of truth at run time. Discovery
happens at connect / resync, not on every agent turn.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
REQUEST_TIMEOUT = 30.0
OUTPUT_CAP = 256 * 1024
WRITE_NAME_RE = re.compile(
    r"(create|update|delete|send|write|post|patch|remove|put|publish)",
    re.IGNORECASE,
)

# Catalog presets. URLs are defaults the user can still override for Zapier/custom.
CATALOG: dict[str, dict[str, str]] = {
    "github": {
        "label": "GitHub",
        "url": "https://api.githubcopilot.com/mcp",
        "preferred_auth": "bearer",
        "description": "Issues, pull requests, and repository tools via GitHub's remote MCP server.",
    },
    "linear": {
        "label": "Linear",
        "url": "https://mcp.linear.app/mcp",
        "preferred_auth": "oauth",
        "description": "Search and update Linear issues from an agent.",
    },
    "zapier": {
        "label": "Zapier",
        "url": "",
        "preferred_auth": "bearer",
        "description": "Actions across Zapier's connected apps. Paste the MCP URL Zapier gives you.",
    },
    "notion": {
        "label": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "preferred_auth": "oauth",
        "description": "Read and update Notion pages and databases.",
    },
    "slack": {
        "label": "Slack",
        "url": "https://mcp.slack.com/mcp",
        "preferred_auth": "oauth",
        "description": "Read channels and post messages through Slack's remote MCP server.",
    },
    "atlassian": {
        "label": "Atlassian",
        "url": "https://mcp.atlassian.com/v1/mcp",
        "preferred_auth": "oauth",
        "description": "Jira and Confluence through Atlassian's remote MCP server.",
    },
    "custom": {
        "label": "Custom MCP server",
        "url": "",
        "preferred_auth": "probe",
        "description": "Any HTTPS MCP server you run. We probe for OAuth or a bearer token.",
    },
}


class UnsafeUrlError(IntegrationError):
    """The URL is not safe to fetch from this host."""


class McpAuthRequired(IntegrationError):
    """The server wants credentials. `prm_url` is set when OAuth metadata is advertised."""

    def __init__(self, message: str, prm_url: str | None = None, status_code: int = 401):
        super().__init__(message)
        self.prm_url = prm_url
        self.status_code = status_code


# ── SSRF ──────────────────────────────────────────────────────────────────────

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _host_is_blocked(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {hostname!r}") from exc
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any(addr in net for net in _BLOCKED_NETWORKS):
            return True
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return True
    return False


def validate_mcp_url(url: str) -> str:
    """Return a cleaned HTTPS URL or raise UnsafeUrlError."""
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise UnsafeUrlError("MCP server URLs must use HTTPS")
    if not parsed.hostname:
        raise UnsafeUrlError("MCP server URL is missing a host")
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"}:
        raise UnsafeUrlError("That host is not allowed")
    if _host_is_blocked(host):
        raise UnsafeUrlError("That host resolves to a private or reserved address")
    return cleaned


# ── HTTP / JSON-RPC ───────────────────────────────────────────────────────────

def _parse_sse_json(text: str) -> dict[str, Any] | None:
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            last = json.loads(payload)
        except json.JSONDecodeError:
            continue
    return last


def _extract_www_authenticate_metadata(header: str) -> str | None:
    """Pull resource_metadata="..." from a WWW-Authenticate Bearer challenge."""
    match = re.search(r'resource_metadata=(?:"([^"]+)"|([^\s,]+))', header or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


async def mcp_rpc(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    session_id: str | None = None,
    notification: bool = False,
) -> tuple[Any, str | None]:
    """One JSON-RPC call. Returns (result, session_id)."""
    validate_mcp_url(url)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if not notification:
        body["id"] = secrets.token_hex(8)
    if params is not None:
        body["params"] = params

    async with httpx.AsyncClient(follow_redirects=False, timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"MCP request failed: {exc}") from exc

    if resp.status_code in {301, 302, 303, 307, 308}:
        location = resp.headers.get("location", "")
        if location:
            try:
                validate_mcp_url(urljoin(url, location))
            except UnsafeUrlError as exc:
                raise UnsafeUrlError("Redirect target is not allowed") from exc
        raise IntegrationError("MCP server redirected; refusing to follow automatically")

    if resp.status_code == 401:
        prm = _extract_www_authenticate_metadata(resp.headers.get("www-authenticate", ""))
        raise McpAuthRequired("MCP server requires authentication", prm_url=prm, status_code=401)
    if resp.status_code == 403:
        raise McpAuthRequired("MCP server refused the credentials", status_code=403)
    if resp.status_code >= 400:
        raise IntegrationError(f"MCP server returned HTTP {resp.status_code}")

    new_session = resp.headers.get("mcp-session-id") or session_id
    ctype = resp.headers.get("content-type", "")
    raw = resp.content[: OUTPUT_CAP + 1]
    if len(raw) > OUTPUT_CAP:
        text = raw[:OUTPUT_CAP].decode("utf-8", errors="replace")
    else:
        text = resp.text

    if notification:
        return None, new_session

    parsed: dict[str, Any] | None
    if "text/event-stream" in ctype:
        parsed = _parse_sse_json(text)
    else:
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError as exc:
            raise IntegrationError("MCP server returned invalid JSON") from exc

    if not isinstance(parsed, dict):
        raise IntegrationError("MCP server returned an empty response")
    if parsed.get("error"):
        err = parsed["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise IntegrationError(f"MCP error: {msg}")
    return parsed.get("result"), new_session


async def list_remote_tools(url: str, token: str | None = None) -> list[dict[str, Any]]:
    """initialize + tools/list. Returns frozen-schema shaped dicts."""
    result, session_id = await mcp_rpc(
        url,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "setod", "version": "1.0"},
        },
        token=token,
    )
    try:
        await mcp_rpc(
            url,
            "notifications/initialized",
            {},
            token=token,
            session_id=session_id,
            notification=True,
        )
    except IntegrationError:
        # Some servers ignore the initialized notification.
        pass
    listed, _ = await mcp_rpc(url, "tools/list", {}, token=token, session_id=session_id)
    tools = (listed or {}).get("tools") if isinstance(listed, dict) else None
    if not isinstance(tools, list):
        return []
    frozen = []
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
        frozen.append({
            "name": str(t["name"]),
            "description": str(t.get("description") or t["name"]),
            "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        })
    return frozen


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= OUTPUT_CAP:
        return text
    return encoded[:OUTPUT_CAP].decode("utf-8", errors="replace") + "\n… [truncated]"


async def call_remote_tool(url: str, name: str, arguments: dict[str, Any], token: str | None = None) -> str:
    result, _ = await mcp_rpc(
        url,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        token=token,
    )
    if result is None:
        return "OK"
    if isinstance(result, str):
        return _truncate(result)
    # MCP tools/call returns {content: [{type, text}], isError?}
    if isinstance(result, dict):
        parts = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        if parts:
            text = "\n".join(parts)
            if result.get("isError"):
                return _truncate(f"Tool error: {text}")
            return _truncate(text)
        return _truncate(json.dumps(result, default=str))
    return _truncate(json.dumps(result, default=str))


# ── Probe ─────────────────────────────────────────────────────────────────────

async def discover_oauth(url: str, prm_url: str | None = None) -> dict[str, Any] | None:
    """RFC 9728 PRM → AS metadata, or AS metadata alone when the host skips PRM."""
    candidates = []
    if prm_url:
        candidates.append(prm_url)
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates.append(urljoin(origin, "/.well-known/oauth-protected-resource"))
    # Some hosts nest the well-known path under the MCP path.
    if parsed.path and parsed.path != "/":
        candidates.append(urljoin(origin, f"/.well-known/oauth-protected-resource{parsed.path}"))

    prm: dict[str, Any] | None = None
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        for candidate in candidates:
            try:
                validate_mcp_url(candidate)
            except UnsafeUrlError:
                continue
            try:
                resp = await client.get(candidate, headers={"Accept": "application/json"})
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and (data.get("authorization_servers") or data.get("authorization_server")):
                prm = data
                break

    issuer = None
    if prm:
        issuers = prm.get("authorization_servers") or []
        if not issuers and prm.get("authorization_server"):
            issuers = [prm["authorization_server"]]
        if issuers:
            issuer = str(issuers[0]).rstrip("/")
            try:
                validate_mcp_url(issuer if issuer.startswith("https://") else f"https://{urlparse(issuer).netloc}/")
            except UnsafeUrlError:
                issuer = None

    meta_urls = []
    if issuer:
        meta_urls.extend([
            f"{issuer}/.well-known/oauth-authorization-server",
            f"{issuer}/.well-known/openid-configuration",
        ])
    meta_urls.append(urljoin(origin, "/.well-known/oauth-authorization-server"))
    meta_urls.append(urljoin(origin, "/.well-known/openid-configuration"))
    if parsed.path and parsed.path != "/":
        meta_urls.append(urljoin(origin, f"/.well-known/oauth-authorization-server{parsed.path}"))

    seen: set[str] = set()
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        for meta_url in meta_urls:
            if meta_url in seen:
                continue
            seen.add(meta_url)
            try:
                validate_mcp_url(meta_url)
            except UnsafeUrlError:
                continue
            try:
                resp = await client.get(meta_url, headers={"Accept": "application/json"})
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                meta = resp.json()
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                return {
                    "issuer": issuer or meta.get("issuer") or origin,
                    "authorization_endpoint": meta["authorization_endpoint"],
                    "token_endpoint": meta["token_endpoint"],
                    "registration_endpoint": meta.get("registration_endpoint"),
                    "scopes": (prm or {}).get("scopes_supported") or meta.get("scopes_supported") or [],
                    "resource": (prm or {}).get("resource") or url,
                    "cimd": bool(meta.get("client_id_metadata_document_supported")),
                    "token_auth": (meta.get("token_endpoint_auth_methods_supported") or ["none"])[0],
                }
    return None


async def probe_mcp(url: str, token: str | None = None) -> dict[str, Any]:
    """Classify auth and, when possible, return the tool list."""
    try:
        tools = await list_remote_tools(url, token=token)
        return {
            "auth": "bearer" if token else "none",
            "tools": tools,
            "oauth": None,
        }
    except McpAuthRequired as exc:
        if token:
            raise IntegrationError(str(exc)) from exc
        oauth = await discover_oauth(url, exc.prm_url)
        return {
            "auth": "oauth" if oauth else "bearer",
            "tools": None,
            "oauth": oauth,
        }


def write_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["name"] for t in tools if WRITE_NAME_RE.search(t.get("name") or "")]


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = __import__("base64").urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def register_oauth_client(registration_endpoint: str, redirect_uri: str) -> dict[str, Any]:
    validate_mcp_url(registration_endpoint)
    payload = {
        "client_name": "setod",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
        resp = await client.post(registration_endpoint, json=payload)
    if resp.status_code >= 400:
        raise IntegrationError(
            "This MCP server requires a pre-registered OAuth client and Dynamic Client Registration failed"
        )
    data = resp.json()
    if not data.get("client_id"):
        raise IntegrationError("OAuth registration did not return a client_id")
    return data


def _is_slack_oauth(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "slack.com" or host.endswith(".slack.com")


def authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    resource: str,
    challenge: str,
    state: str,
    scopes: list[str] | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    # Slack's Web API rejects unknown fields. Their v2_user authorize takes
    # comma-separated `scope` (user token only) — not `user_scope` and not `resource`.
    if _is_slack_oauth(authorization_endpoint):
        if scopes:
            params["scope"] = ",".join(scopes)
    else:
        params["resource"] = resource
        if scopes:
            params["scope"] = " ".join(scopes)
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


async def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    verifier: str,
    resource: str,
    client_secret: str | None = None,
) -> dict[str, Any]:
    validate_mcp_url(token_endpoint)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    # Slack oauth.v2.user.access has a closed argument list — `resource` is invalid_arg_name.
    if not _is_slack_oauth(token_endpoint):
        data["resource"] = resource
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
        resp = await client.post(token_endpoint, data=data)
    if resp.status_code >= 400:
        raise IntegrationError(f"OAuth token exchange failed ({resp.status_code})")
    try:
        token = resp.json()
    except json.JSONDecodeError as exc:
        raise IntegrationError("OAuth token response was not JSON") from exc
    # Slack's oauth.v2.user.access uses {ok, error} instead of HTTP status.
    if token.get("ok") is False:
        raise IntegrationError(f"OAuth token exchange failed: {token.get('error') or token}")
    if not token.get("access_token"):
        # Some Slack payloads nest the user token.
        authed = token.get("authed_user") if isinstance(token.get("authed_user"), dict) else None
        if authed and authed.get("access_token"):
            token["access_token"] = authed["access_token"]
            if authed.get("refresh_token"):
                token["refresh_token"] = authed["refresh_token"]
            if authed.get("expires_in"):
                token["expires_in"] = authed["expires_in"]
    if not token.get("access_token"):
        raise IntegrationError("OAuth token response was missing an access_token")
    return token


async def refresh_oauth_token(config: dict[str, Any]) -> dict[str, Any]:
    refresh = config.get("refresh_token")
    token_endpoint = config.get("token_endpoint")
    if not refresh or not token_endpoint:
        return config
    expires_at = config.get("token_expires_at")
    if expires_at:
        try:
            exp = datetime.fromtimestamp(float(expires_at), tz=UTC)
            if exp - datetime.now(UTC) > timedelta(seconds=60):
                return config
        except (TypeError, ValueError):
            pass
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": config.get("client_id") or "",
    }
    if not _is_slack_oauth(token_endpoint):
        data["resource"] = config.get("url") or ""
    if config.get("client_secret"):
        data["client_secret"] = config["client_secret"]
    try:
        validate_mcp_url(token_endpoint)
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
            resp = await client.post(token_endpoint, data=data)
        if resp.status_code >= 400:
            return config
        token = resp.json()
    except (httpx.HTTPError, UnsafeUrlError, json.JSONDecodeError):
        return config
    if token.get("ok") is False or not token.get("access_token"):
        return config
    config["access_token"] = token["access_token"]
    if token.get("refresh_token"):
        config["refresh_token"] = token["refresh_token"]
    expires_in = token.get("expires_in")
    if expires_in:
        config["token_expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(expires_in))).timestamp()
    return config


# ── Agent tools ───────────────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    """Build from the frozen schema on the connector — never live-discover."""
    config = decrypt_json(ctx.connector.config) if ctx.connector.config else {}
    frozen = config.get("tools") or []
    url = config.get("url") or ""
    tools: list[RegisteredTool] = []

    for spec in frozen:
        name = spec.get("name")
        if not name:
            continue
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        parameters = {k: v for k, v in schema.items() if k != "$schema"}
        if "type" not in parameters:
            parameters["type"] = "object"
        if "properties" not in parameters:
            parameters["properties"] = {}

        tool_name = ctx.tool_name(name)
        description = ctx.describe(spec.get("description") or name)
        base_name = name

        async def handler(
            args: dict[str, Any],
            dry_run: bool,
            _url: str = url,
            _base: str = base_name,
        ) -> str:
            if dry_run:
                return f"[simulated] {_base}({json.dumps(args, default=str)})"
            from app.core.crypto import encrypt_json

            cfg = decrypt_json(ctx.connector.config) if ctx.connector.config else {}
            if cfg.get("auth") == "oauth":
                cfg = await refresh_oauth_token(cfg)
                ctx.connector.config = encrypt_json(cfg)
                ctx.db.add(ctx.connector)
                await ctx.db.commit()
            token = cfg.get("access_token") or None
            target = cfg.get("url") or _url
            try:
                return await call_remote_tool(target, _base, args, token=token)
            except McpAuthRequired:
                if cfg.get("auth") == "oauth":
                    cfg["token_expires_at"] = 0
                    updated = await refresh_oauth_token(cfg)
                    ctx.connector.config = encrypt_json(updated)
                    ctx.db.add(ctx.connector)
                    await ctx.db.commit()
                    return await call_remote_tool(target, _base, args, token=updated.get("access_token"))
                raise IntegrationError("MCP credentials were rejected — reconnect the server") from None

        tools.append(
            RegisteredTool(
                spec=ToolSpec(name=tool_name, description=description, parameters=parameters),
                handler=handler,
            )
        )
    return tools
