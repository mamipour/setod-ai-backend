"""
DuckDB sandbox for org-table SQL queries
==========================================
Loads org table rows into an in-memory DuckDB instance (isolated per call),
then runs one read-only SQL statement with hard safety constraints:
  - enable_external_access = false   (no file/network access from SQL)
  - lock_configuration = true        (statement cannot flip those settings)
  - One statement per call
  - Memory cap, timeout, row/char result cap

Only the tables the agent has _search access to are loaded.
This prevents model-written SQL from bypassing per-table permissions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import duckdb
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import OrgTableRow

log = logging.getLogger(__name__)

QUERY_MEMORY_LIMIT   = "256MB"
QUERY_TIMEOUT_SECONDS = 20
MAX_RESULT_ROWS      = 200
MAX_RESULT_CHARS     = 32_000
MAX_CELL_CHARS       = 200


# ── Sandbox runner (synchronous — runs in thread pool) ─────────────────────────

def _format_result(columns: list[str], rows: list[tuple], *, truncated: bool) -> str:
    def cell(v: Any) -> str:
        s = "" if v is None else str(v)
        s = s.replace("\n", " ").replace("|", "\\|")
        return s if len(s) <= MAX_CELL_CHARS else s[: MAX_CELL_CHARS - 1] + "…"

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows:
        lines.append("| " + " | ".join(cell(v) for v in r) + " |")
    out = "\n".join(lines)
    if len(out) > MAX_RESULT_CHARS:
        out = out[:MAX_RESULT_CHARS] + "\n… (output truncated)"
    if truncated:
        out += (
            f"\n\nShowing the first {MAX_RESULT_ROWS} rows. "
            "Aggregate (COUNT, GROUP BY) or add a tighter WHERE / LIMIT."
        )
    return out + f"\n\n({len(rows)} row{'s' if len(rows) != 1 else ''})"


def _run_query_sync(
    table_data: dict[str, list[dict]],
    sql: str,
    handle: dict | None = None,
) -> str:
    """Execute one statement against in-memory tables built from row data dicts.

    `table_data` maps table slug → list of row dicts (agent-visible columns only).
    Returns formatted result text. Never raises for SQL errors.
    """
    import json as _json

    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return "Provide a SQL SELECT statement."

    con = duckdb.connect(config={"memory_limit": QUERY_MEMORY_LIMIT, "threads": 2})
    if handle is not None:
        handle["con"] = con

    try:
        for slug, rows in table_data.items():
            if not rows:
                # Create empty table so the query can still reference it
                con.execute(
                    f'CREATE TABLE "{slug}" (id VARCHAR, created_at VARCHAR)'
                )
            else:
                # Write rows to a temp JSON-lines file so DuckDB can infer schema.
                # This happens BEFORE we lock external access; the file is deleted
                # immediately after table creation.
                import os as _os
                import tempfile as _tempfile
                ndjson = "\n".join(_json.dumps(r) for r in rows)
                with _tempfile.NamedTemporaryFile(
                    suffix=".jsonl", mode="w", delete=False
                ) as tf:
                    tf.write(ndjson)
                    tmp_path = tf.name
                try:
                    con.execute(
                        f"CREATE TABLE \"{slug}\" AS SELECT * FROM read_json_auto(?)",
                        [tmp_path],
                    )
                finally:
                    _os.unlink(tmp_path)

        # Lock down after data is loaded
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")

        # Parse — reject multi-statement or non-SELECT
        try:
            stmts = con.extract_statements(sql)
            if len(stmts) != 1:
                return "Only one SQL statement per call."
            # Only SELECT is permitted (reject DDL/DML)
            first_keyword = sql.lstrip().split()[0].upper() if sql.lstrip() else ""
            if first_keyword not in ("SELECT", "WITH"):
                return "Only SELECT (or WITH … SELECT) queries are allowed."
        except duckdb.Error as exc:
            return f"SQL error: {str(exc).splitlines()[0]}"

        # Execute
        try:
            cur = con.execute(sql)
            if cur.description is None:
                return "The statement produced no output. Use SELECT to read data."
            columns = [d[0] for d in cur.description]
            rows_out = cur.fetchmany(MAX_RESULT_ROWS + 1)
        except duckdb.InterruptException:
            return (
                f"Query stopped after {QUERY_TIMEOUT_SECONDS}s. "
                "Narrow with WHERE or use aggregates."
            )
        except duckdb.Error as exc:
            return f"SQL error: {str(exc).splitlines()[0]}"

        truncated = len(rows_out) > MAX_RESULT_ROWS
        return _format_result(columns, rows_out[:MAX_RESULT_ROWS], truncated=truncated)
    finally:
        con.close()


async def run_query(table_data: dict[str, list[dict]], sql: str) -> str:
    """Async wrapper: runs _run_query_sync in a thread with a wall-clock timeout."""
    handle: dict = {}
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, lambda: _run_query_sync(table_data, sql, handle))
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=QUERY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        con = handle.get("con")
        if con is not None:
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001
                pass
        try:
            return await fut
        except Exception:  # noqa: BLE001
            return f"Query stopped after {QUERY_TIMEOUT_SECONDS}s."


# ── Data loader ────────────────────────────────────────────────────────────────

async def load_permitted_tables(
    db: AsyncSession,
    org_id: UUID,
    permitted_slugs: set[str],
) -> dict[str, list[dict]]:
    """Load row data only for tables the agent is permitted to search.

    Returns a dict of {slug: [row_dict, ...]} with hidden_from_agents columns already
    stripped.  Isolation: only rows for this org are ever loaded.
    """
    from app.db.models import OrgTable
    from app.core.tables.schema import strip_hidden

    if not permitted_slugs:
        return {}

    tables_result = await db.exec(
        select(OrgTable).where(
            OrgTable.org_id == org_id,
            OrgTable.slug.in_(permitted_slugs),
            OrgTable.deleted_at.is_(None),
        )
    )
    tables = list(tables_result.all())

    out: dict[str, list[dict]] = {}
    for tbl in tables:
        rows_result = await db.exec(
            select(OrgTableRow).where(
                OrgTableRow.org_id == org_id,
                OrgTableRow.table_id == tbl.id,
                OrgTableRow.deleted_at.is_(None),
            ).limit(50_000)
        )
        rows = rows_result.all()
        out[tbl.slug] = [
            {"id": str(r.id), "created_at": r.created_at.isoformat(),
             **strip_hidden(tbl.columns, r.data)}
            for r in rows
        ]

    return out
