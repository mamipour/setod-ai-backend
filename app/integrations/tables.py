"""
Tables integration — agent tools for org-level typed tables
============================================================
Produces a set of generated tools from the org's live table schemas:

  {slug}_search(where?, order_by?, limit?)  — filtered row list
  {slug}_get(id)                             — single row
  {slug}_create(data)                        — insert (dedup via unique_on)
  {slug}_update(id, data, expected_version?) — partial update

Plus one org-wide:
  query_tables(sql)  — sandboxed DuckDB SELECT across permitted tables

The builder is registered under ConnectorType.tables in registry.py.
The connector has no credentials — the connector row is auto-provisioned
per org by service.get_or_create_tables_connector().

Agent permissions:
  enabled_tools   — which verbs (slug_search, slug_create, etc.) are on
  approval_tools  — which verbs require human approval before execution

Hidden-from-agents columns are stripped from all inputs and outputs.
A per-session write counter caps agent writes at AGENT_WRITES_PER_SESSION.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from app.core.agents.base import RegisteredTool
from app.core.llm.client import ToolSpec
from app.core.tables import service as svc
from app.core.tables.query import load_permitted_tables, run_query
from app.core.tables.schema import render_tool_description, strip_hidden
from app.db.models import OrgTable
from app.integrations.base import IntegrationError, ToolContext
from sqlmodel import select

log = logging.getLogger(__name__)

# Track writes per session via a mutable dict on the ToolContext (per-run)
_WRITE_CTR_ATTR = "_tables_write_counter"


def _write_counter(ctx: ToolContext) -> dict:
    if not hasattr(ctx, _WRITE_CTR_ATTR):
        object.__setattr__(ctx, _WRITE_CTR_ATTR, {"count": 0})
    return getattr(ctx, _WRITE_CTR_ATTR)


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    """Build tools for every non-deleted table in this org."""
    # We need to do this async but build_tools is called synchronously.
    # Use a lazy pattern: tools are closures that fetch data at call time.
    # The org_id comes from the connector's org_id.
    org_id = ctx.connector.org_id

    # We can't do async here, so we return async handlers that query at call time.
    tools: list[RegisteredTool] = []

    # We'll build the tool list lazily at first call using a sync stub.
    # Instead, use the standard pattern: build closures that capture org_id.
    # Tables list is fetched when tools are first built. We use a synchronous
    # pattern by pre-fetching in an inner async call here... but build_tools is sync.
    #
    # Resolution: build_tools is called from build_tools_for_agent which is async.
    # We return a special "deferred" approach: one async tool that does the list
    # and builds everything, OR we just query tables inside each tool handler.
    # The simplest safe approach: build the static tool specs with descriptions
    # that will be populated at run time. Each handler fetches the table itself.

    # For the tool DESCRIPTIONS to be correct, we need the table list at build time.
    # We capture org_id and query inside each tool. The description will be generic
    # until we can use the async build pattern.
    #
    # Actually, looking at the codebase: build_tools is called from an async context
    # in registry.py (build_tools_for_agent is async and calls builder(ctx) sync).
    # We return the static tools here; descriptions are derived from the table at
    # call time. This is the same pattern as MCP (frozen schema on connector).
    #
    # Better: build per-table tools and query_tables. The registry caller is async;
    # we need to make build_tools async too OR store the table schema on the connector
    # config. Since the connector has no config, we fetch at call time.
    #
    # The cleanest fix: return a single async-capable "dispatcher" set. Each named
    # tool fetches its table at call time. The description is populated from connector
    # config if present, or a generic fallback.
    #
    # We adopt the pattern: build_tables_tools is an async function called from
    # build_tools_for_agent, bypassing the BUILDERS dict for this connector type.
    # See registry.py for the special-case handling.
    #
    # For now, emit one query_tables tool + mark this connector as needing dynamic
    # tool expansion. The async build path (build_tables_tools_async) is called in
    # registry.py.

    return _build_tools_sync(ctx, org_id)


def _build_tools_sync(ctx: ToolContext, org_id: UUID) -> list[RegisteredTool]:
    """Build all table tools.  Called from the async registry path after table fetch."""
    # This is called via the async wrapper in registry.py that fetches tables first.
    # The stored table list is passed via ctx._org_tables.
    tables: list[OrgTable] = getattr(ctx, "_org_tables", [])

    tools: list[RegisteredTool] = []
    permitted_slugs: set[str] = set()  # for query_tables scope

    for tbl in tables:
        slug = tbl.slug
        permitted_slugs.add(slug)
        visible_cols = [c for c in tbl.columns if not c.get("hidden_from_agents")]
        desc = render_tool_description(tbl.name, tbl.columns)
        col_props = {
            c["key"]: {
                "type": "string" if c["type"] not in ("number", "checkbox") else
                        ("number" if c["type"] == "number" else "boolean"),
                "description": f"{c['name']} ({c['type']})" + (
                    f". Options: {', '.join(c.get('options', []))}"
                    if c.get("options") else ""
                ),
            }
            for c in visible_cols
        }

        # ── {slug}_search ─────────────────────────────────────────────────────
        async def search_handler(args: dict[str, Any], dry_run: bool, _tbl=tbl, _org=org_id) -> str:
            if dry_run:
                return f"[simulated] Would search {_tbl.name}."
            where  = args.get("where") or {}
            limit  = min(int(args.get("limit", 20)), 100)
            order  = args.get("order_by") or "created_at"
            is_desc = str(args.get("order_dir", "desc")).lower() == "desc"

            if not isinstance(where, dict):
                return "Error: 'where' must be an object of {column_key: value} filters."

            rows, total = await svc.list_rows(
                ctx.db, _org, _tbl.id,
                filters=where,
                order_by=order,
                desc=is_desc,
                limit=limit,
            )
            if not rows:
                return f"No rows found in '{_tbl.name}'."
            lines = [f"Found {total} row(s) (showing {len(rows)}):"]
            for r in rows:
                data = strip_hidden(_tbl.columns, r.data)
                lines.append(f"  id={r.id}  " + "  ".join(f"{k}={v!r}" for k, v in data.items() if v is not None))
            return "\n".join(lines)

        tools.append(RegisteredTool(
            spec=ToolSpec(
                name=f"{slug}_search",
                description=(
                    f"Search rows in the '{tbl.name}' table. "
                    f"{desc}\n"
                    "Use 'where' to filter, 'order_by' to sort, 'limit' (max 100) to cap results."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "where":    {"type": "object", "description": f"Filter by column values, e.g. {{\"status\": \"new\"}}. Available columns: {', '.join(col_props)}"},
                        "order_by": {"type": "string", "description": "Column key to sort by (default: created_at)"},
                        "order_dir":{"type": "string", "enum": ["asc", "desc"], "description": "Sort direction"},
                        "limit":    {"type": "integer", "description": "Max rows (default 20, max 100)"},
                    },
                    "required": [],
                },
            ),
            handler=search_handler,
        ))

        # ── {slug}_get ────────────────────────────────────────────────────────
        async def get_handler(args: dict[str, Any], dry_run: bool, _tbl=tbl, _org=org_id) -> str:
            row_id_str = str(args.get("id", "")).strip()
            if not row_id_str:
                return "Error: 'id' is required."
            if dry_run:
                return f"[simulated] Would get row {row_id_str} from {_tbl.name}."
            try:
                row = await svc.get_row(ctx.db, _org, _tbl.id, UUID(row_id_str))
            except (svc.TableError, ValueError) as e:
                return f"Error: {e}"
            data = strip_hidden(_tbl.columns, row.data)
            return json.dumps({"id": str(row.id), "version": row.version, **data}, default=str)

        tools.append(RegisteredTool(
            spec=ToolSpec(
                name=f"{slug}_get",
                description=f"Get a single row from '{tbl.name}' by its id.",
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Row id"}},
                    "required": ["id"],
                },
            ),
            handler=get_handler,
        ))

        # ── {slug}_create ─────────────────────────────────────────────────────
        async def create_handler(args: dict[str, Any], dry_run: bool, _tbl=tbl, _org=org_id) -> str:
            data = {k: v for k, v in args.items() if k != "dry_run"}
            if dry_run:
                return f"[simulated] Would create row in '{_tbl.name}': {json.dumps(data, default=str)}"
            try:
                row = await svc.create_row(
                    ctx.db, _org, _tbl.id, data,
                    actor_session_id=ctx.session_id,
                    agent_id=ctx.agent_id,
                    session_write_counter=_write_counter(ctx),
                )
                return f"Row created in '{_tbl.name}'. id={row.id}"
            except svc.RowConflict as e:
                return (
                    f"Row already exists (id={e.existing_row.id}). "
                    f"Use {slug}_update to modify it."
                )
            except (svc.TableError, Exception) as e:
                return f"Error: {e}"

        unique_note = ""
        if tbl.unique_on:
            unique_note = f" If {', '.join(tbl.unique_on)} matches an existing row, returns the existing row id."
        tools.append(RegisteredTool(
            spec=ToolSpec(
                name=f"{slug}_create",
                description=(
                    f"Create a new row in '{tbl.name}'.{unique_note}\n{desc}"
                ),
                parameters={
                    "type": "object",
                    "properties": col_props,
                    "required": [c["key"] for c in visible_cols if c.get("required")],
                },
            ),
            handler=create_handler,
        ))

        # ── {slug}_update ─────────────────────────────────────────────────────
        async def update_handler(args: dict[str, Any], dry_run: bool, _tbl=tbl, _org=org_id) -> str:
            row_id_str = str(args.get("id", "")).strip()
            if not row_id_str:
                return "Error: 'id' is required."
            expected_v = args.get("expected_version")
            data = {k: v for k, v in args.items() if k not in ("id", "expected_version")}
            if not data:
                return "Error: provide at least one column to update."
            if dry_run:
                return f"[simulated] Would update row {row_id_str} in '{_tbl.name}': {json.dumps(data, default=str)}"
            try:
                row = await svc.update_row(
                    ctx.db, _org, _tbl.id, UUID(row_id_str), data,
                    expected_version=int(expected_v) if expected_v is not None else None,
                    actor_session_id=ctx.session_id,
                    agent_id=ctx.agent_id,
                    session_write_counter=_write_counter(ctx),
                )
                return f"Row {row.id} updated (version {row.version})."
            except svc.VersionConflict as e:
                return f"Version conflict: {e}. Fetch the row again and retry."
            except (svc.TableError, ValueError) as e:
                return f"Error: {e}"

        update_props = {"id": {"type": "string", "description": "Row id"}}
        update_props.update(col_props)
        update_props["expected_version"] = {
            "type": "integer",
            "description": "Optional: current version for optimistic locking. Omit to skip check.",
        }
        tools.append(RegisteredTool(
            spec=ToolSpec(
                name=f"{slug}_update",
                description=(
                    f"Partially update a row in '{tbl.name}'. Only provided columns are changed.\n{desc}"
                ),
                parameters={
                    "type": "object",
                    "properties": update_props,
                    "required": ["id"],
                },
            ),
            handler=update_handler,
        ))

    # ── query_tables ──────────────────────────────────────────────────────────
    if permitted_slugs:
        table_list_str = ", ".join(sorted(permitted_slugs))

        async def query_handler(args: dict[str, Any], dry_run: bool) -> str:
            sql = str(args.get("sql", "")).strip()
            if not sql:
                return "Error: 'sql' is required."
            if dry_run:
                return f"[simulated] Would run SQL: {sql[:200]}"
            table_data = await load_permitted_tables(ctx.db, org_id, permitted_slugs)
            return await run_query(table_data, sql)

        tools.append(RegisteredTool(
            spec=ToolSpec(
                name="query_tables",
                description=(
                    f"Run a read-only SQL SELECT across your business data tables: {table_list_str}. "
                    "Use for aggregates, joins, and complex filters that the search tools can't express. "
                    "Only SELECT is allowed. Table names are the slugs listed above."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "A single SQL SELECT statement."},
                    },
                    "required": ["sql"],
                },
            ),
            handler=query_handler,
        ))

    return tools
