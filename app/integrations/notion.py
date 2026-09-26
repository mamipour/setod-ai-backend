"""
Notion integration
==================
Auth: Internal connection token — ``Authorization: Bearer {token}``.
API version: ``Notion-Version: 2026-03-11``.

The user creates an integration at developers.notion.com, then shares
pages/databases with it from the Notion sidebar (⋯ → Add connections).

Tools (6):
  search_notion(query)
  get_notion_page(page_id)
  query_notion_database(database_id, filter_text?, limit?)
  create_notion_page(parent_database_id, title, properties?)
  update_notion_page(page_id, properties)
  append_notion_content(page_id, text)

All write tools honour dry_run.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_BASE = "https://api.notion.com"
_VERSION = "2026-03-11"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _VERSION,
        "Content-Type": "application/json",
    }


def _rich_to_text(rich: list[dict]) -> str:
    """Flatten Notion rich text array to plain string."""
    return "".join(r.get("plain_text", "") for r in rich)


def _page_title(props: dict) -> str:
    for v in props.values():
        if v.get("type") == "title":
            return _rich_to_text(v.get("title", []))
    return "(untitled)"


def _prop_to_text(prop: dict) -> str:
    t = prop.get("type", "")
    if t == "title":
        return _rich_to_text(prop.get("title", []))
    if t == "rich_text":
        return _rich_to_text(prop.get("rich_text", []))
    if t == "number":
        v = prop.get("number")
        return str(v) if v is not None else ""
    if t == "select":
        s = prop.get("select")
        return s.get("name", "") if s else ""
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if t == "checkbox":
        return "✓" if prop.get("checkbox") else "✗"
    if t == "date":
        d = prop.get("date")
        return d.get("start", "") if d else ""
    if t == "status":
        s = prop.get("status")
        return s.get("name", "") if s else ""
    if t == "url":
        return prop.get("url") or ""
    if t == "email":
        return prop.get("email") or ""
    if t == "phone_number":
        return prop.get("phone_number") or ""
    return ""


async def test_connection(token: str) -> str:
    """Validate the token by searching with empty query. Returns workspace name."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_BASE}/v1/search",
            headers=_headers(token),
            json={"page_size": 1},
        )
    if resp.status_code == 401:
        raise IntegrationError("Invalid or expired integration token")
    if resp.status_code == 403:
        raise IntegrationError("Token has no access — share at least one page with this integration in Notion")
    if resp.status_code != 200:
        raise IntegrationError(f"Notion returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    count = len(data.get("results", []))
    return f"Connected — {count} item(s) accessible (share more pages via Notion sidebar)"


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    token: str = raw["api_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    # ── search_notion ─────────────────────────────────────────────────────────

    async def search_notion(args: dict[str, Any], dry_run: bool) -> str:
        query = str(args.get("query", "")).strip()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/v1/search",
                headers=_headers(token),
                json={"query": query, "page_size": 10},
            )
        if resp.status_code != 200:
            raise IntegrationError(f"Notion search error {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("results", [])
        if not results:
            return f"No results for '{query}'."
        lines = []
        for r in results:
            obj = r.get("object", "")
            rid = r.get("id", "")
            if obj == "page":
                title = _page_title(r.get("properties", {})) or r.get("url", "")
                lines.append(f"Page: {title} (id: {rid})")
            elif obj == "database":
                title = _rich_to_text(r.get("title", []))
                lines.append(f"Database: {title} (id: {rid})")
        return "\n".join(lines)

    # ── get_notion_page ───────────────────────────────────────────────────────

    async def get_notion_page(args: dict[str, Any], dry_run: bool) -> str:
        page_id = str(args.get("page_id", "")).strip().replace("-", "")
        if not page_id:
            return "Error: 'page_id' is required."
        async with httpx.AsyncClient(timeout=15) as client:
            page_resp, blocks_resp = await __import__("asyncio").gather(
                client.get(f"{_BASE}/v1/pages/{page_id}", headers=_headers(token)),
                client.get(f"{_BASE}/v1/blocks/{page_id}/children?page_size=50", headers=_headers(token)),
            )
        if page_resp.status_code == 404:
            return f"Page {page_id} not found. Make sure it is shared with your integration."
        if page_resp.status_code != 200:
            raise IntegrationError(f"Notion error {page_resp.status_code}: {page_resp.text[:300]}")
        page = page_resp.json()
        title = _page_title(page.get("properties", {}))
        lines = [f"Title: {title}", f"ID: {page_id}", "---"]
        if blocks_resp.status_code == 200:
            for block in blocks_resp.json().get("results", []):
                bt = block.get("type", "")
                bd = block.get(bt, {})
                text = _rich_to_text(bd.get("rich_text", []))
                if text:
                    lines.append(text)
        return "\n".join(lines)

    # ── query_notion_database ─────────────────────────────────────────────────

    async def query_notion_database(args: dict[str, Any], dry_run: bool) -> str:
        db_id = str(args.get("database_id", "")).strip().replace("-", "")
        filter_text = str(args.get("filter_text", "")).strip()
        limit = min(int(args.get("limit", 10)), 50)
        if not db_id:
            return "Error: 'database_id' is required."

        # Resolve database_id → data_source_id
        async with httpx.AsyncClient(timeout=15) as client:
            db_resp = await client.get(f"{_BASE}/v1/databases/{db_id}", headers=_headers(token))
            if db_resp.status_code == 404:
                return f"Database {db_id} not found. Make sure it is shared with your integration."
            if db_resp.status_code != 200:
                raise IntegrationError(f"Notion error {db_resp.status_code}: {db_resp.text[:300]}")

            db_data = db_resp.json()
            # Prefer data_source_id (new API), fall back to database id
            data_sources = db_data.get("data_sources", [])
            ds_id = data_sources[0].get("id") if data_sources else db_id

            body: dict[str, Any] = {"page_size": limit}
            # Simple text filter across title property if requested
            if filter_text:
                title_prop = next(
                    (k for k, v in db_data.get("properties", {}).items() if v.get("type") == "title"),
                    None,
                )
                if title_prop:
                    body["filter"] = {
                        "property": title_prop,
                        "title": {"contains": filter_text},
                    }

            query_resp = await client.post(
                f"{_BASE}/v1/data_sources/{ds_id}/query",
                headers=_headers(token),
                json=body,
            )
        if query_resp.status_code != 200:
            # Fall back to old database query endpoint
            async with httpx.AsyncClient(timeout=15) as client:
                query_resp = await client.post(
                    f"{_BASE}/v1/databases/{db_id}/query",
                    headers=_headers(token),
                    json=body,
                )
            if query_resp.status_code != 200:
                raise IntegrationError(f"Notion query error {query_resp.status_code}: {query_resp.text[:300]}")

        results = query_resp.json().get("results", [])
        if not results:
            return "No rows found."
        lines = []
        for row in results:
            props = row.get("properties", {})
            title = _page_title(props)
            other = {k: _prop_to_text(v) for k, v in props.items() if _prop_to_text(v) and v.get("type") != "title"}
            summary = f"• {title} (id: {row['id']})"
            if other:
                summary += " | " + " | ".join(f"{k}: {v}" for k, v in list(other.items())[:4])
            lines.append(summary)
        return "\n".join(lines)

    # ── create_notion_page ────────────────────────────────────────────────────

    async def create_notion_page(args: dict[str, Any], dry_run: bool) -> str:
        parent_id = str(args.get("parent_database_id", "")).strip().replace("-", "")
        title = str(args.get("title", "")).strip()
        properties = args.get("properties", {})
        if not parent_id or not title:
            return "Error: 'parent_database_id' and 'title' are required."
        payload: dict[str, Any] = {
            "parent": {"database_id": parent_id},
            "properties": {
                "title": {"title": [{"text": {"content": title}}]},
            },
        }
        if isinstance(properties, dict):
            for k, v in properties.items():
                payload["properties"][k] = {"rich_text": [{"text": {"content": str(v)}}]}
        if dry_run:
            return f"[simulated] Would create Notion page '{title}' in database {parent_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{_BASE}/v1/pages", headers=_headers(token), json=payload)
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Notion error {resp.status_code}: {resp.text[:300]}")
        page_id = resp.json()["id"]
        return f"Page created — id: {page_id}, title: '{title}'"

    # ── update_notion_page ────────────────────────────────────────────────────

    async def update_notion_page(args: dict[str, Any], dry_run: bool) -> str:
        page_id = str(args.get("page_id", "")).strip().replace("-", "")
        properties = args.get("properties", {})
        if not page_id:
            return "Error: 'page_id' is required."
        if not isinstance(properties, dict) or not properties:
            return "Error: 'properties' must be a non-empty dict."
        payload: dict[str, Any] = {"properties": {}}
        for k, v in properties.items():
            payload["properties"][k] = {"rich_text": [{"text": {"content": str(v)}}]}
        if dry_run:
            return f"[simulated] Would update page {page_id} with: {properties}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(f"{_BASE}/v1/pages/{page_id}", headers=_headers(token), json=payload)
        if resp.status_code == 404:
            return f"Page {page_id} not found."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Notion error {resp.status_code}: {resp.text[:300]}")
        return f"Page {page_id} updated."

    # ── append_notion_content ─────────────────────────────────────────────────

    async def append_notion_content(args: dict[str, Any], dry_run: bool) -> str:
        page_id = str(args.get("page_id", "")).strip().replace("-", "")
        text = str(args.get("text", "")).strip()
        if not page_id or not text:
            return "Error: 'page_id' and 'text' are required."
        if dry_run:
            return f"[simulated] Would append to page {page_id}: {text[:80]}"
        # Split long text into ≤2000-char chunks (Notion block limit)
        chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
        children = [{"object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}}
                    for chunk in chunks]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_BASE}/v1/blocks/{page_id}/children",
                headers=_headers(token),
                json={"children": children},
            )
        if resp.status_code == 404:
            return f"Page {page_id} not found."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Notion error {resp.status_code}: {resp.text[:300]}")
        return f"Content appended to page {page_id}."

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("search_notion"),
                description=(
                    "Search Notion for pages and databases by keyword. "
                    "Returns page/database titles and ids. Use the id with "
                    "get_notion_page or query_notion_database."
                ),
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search term. Can be empty to list recent items."}},
                    "required": [],
                },
            ),
            handler=search_notion,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_notion_page"),
                description=(
                    "Get the content of a Notion page — title and up to 50 content blocks as plain text. "
                    "Use for reading notes, meeting summaries, or knowledge articles."
                ),
                parameters={
                    "type": "object",
                    "properties": {"page_id": {"type": "string", "description": "Notion page id (with or without dashes)."}},
                    "required": ["page_id"],
                },
            ),
            handler=get_notion_page,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("query_notion_database"),
                description=(
                    "List rows from a Notion database. Returns titles and key properties. "
                    "Optionally filter by text that appears in the title. "
                    "Use search_notion first to find the database id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Notion database id."},
                        "filter_text": {"type": "string", "description": "Optional text to filter rows by title."},
                        "limit": {"type": "integer", "description": "Max rows to return (default 10, max 50)."},
                    },
                    "required": ["database_id"],
                },
            ),
            handler=query_notion_database,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_notion_page"),
                description=(
                    "Create a new page (row) in a Notion database. "
                    "Use search_notion to find the database id first. "
                    "Pass optional properties as a dict of column name → value."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "parent_database_id": {"type": "string", "description": "Notion database id to create the page in."},
                        "title": {"type": "string", "description": "Page title."},
                        "properties": {
                            "type": "object",
                            "description": "Optional dict of property name → value (e.g. {\"Status\": \"In Progress\"}).",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["parent_database_id", "title"],
                },
            ),
            handler=create_notion_page,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("update_notion_page"),
                description=(
                    "Update properties on an existing Notion database row. "
                    "Pass the page id and a dict of property name → new value. "
                    "Does not affect the page body content."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "Notion page id."},
                        "properties": {
                            "type": "object",
                            "description": "Property names and new values.",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["page_id", "properties"],
                },
            ),
            handler=update_notion_page,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("append_notion_content"),
                description=(
                    "Append text to a Notion page as a new paragraph block. "
                    "Use for adding meeting notes, summaries, log entries, or any freeform text "
                    "without overwriting existing content."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "Notion page id."},
                        "text": {"type": "string", "description": "Text to append (plain text, no markdown)."},
                    },
                    "required": ["page_id", "text"],
                },
            ),
            handler=append_notion_content,
        ),
    ]
