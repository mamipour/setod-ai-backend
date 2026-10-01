"""
Org-table CRUD service
=======================
Every public function takes `org_id` and scopes all DB access to that org.
Never call these from outside the tables router / agent tools without an org_id check.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.tables.schema import (
    ColumnError,
    MAX_COLUMNS_PER_TABLE,
    MAX_ROWS_PER_TABLE,
    slugify,
    strip_hidden,
    validate_column_def,
    validate_row_data,
)
from app.db.models import (
    Connector,
    ConnectorStatus,
    ConnectorType,
    OrgTable,
    OrgTableEvent,
    OrgTableRow,
)

log = logging.getLogger(__name__)

# Cap on agent writes per session to prevent runaway loops
AGENT_WRITES_PER_SESSION = 200


class TableError(ValueError):
    """Surfaced as HTTP 422 to the caller."""


class WriteCapExceeded(TableError):
    """Raised when an agent exceeds AGENT_WRITES_PER_SESSION in a single session."""


class RowConflict(Exception):
    """Raised when create_row hits a unique_on collision.  Carries the existing row."""
    def __init__(self, existing_row: OrgTableRow):
        self.existing_row = existing_row


def _check_write_cap(session_write_counter: dict) -> None:
    """Increment the write counter and raise WriteCapExceeded if over the limit."""
    if session_write_counter["count"] >= AGENT_WRITES_PER_SESSION:
        raise WriteCapExceeded(
            f"Write limit of {AGENT_WRITES_PER_SESSION} per session exceeded. "
            "Stop and ask a human to continue."
        )
    session_write_counter["count"] += 1


class VersionConflict(Exception):
    """Raised when update_row expected_version does not match."""


# ── Connector auto-provision ───────────────────────────────────────────────────

TABLES_CONNECTOR_NAME = "Tables"


async def get_or_create_tables_connector(db: AsyncSession, org_id: UUID, created_by: UUID) -> Connector:
    """Return the built-in 'tables' connector for this org, creating it if absent.

    The name is not user-editable, so an existing row with a stale name (earlier builds
    called it "Business data") is renamed in place to keep it matching the Tables page."""
    row = await db.exec(
        select(Connector).where(
            Connector.org_id == org_id,
            Connector.type == ConnectorType.tables,
        )
    )
    connector = row.first()
    if connector is not None:
        if connector.name != TABLES_CONNECTOR_NAME:
            connector.name = TABLES_CONNECTOR_NAME
            db.add(connector)
            await db.commit()
            await db.refresh(connector)
        return connector

    connector = Connector(
        id=uuid4(),
        org_id=org_id,
        created_by=created_by,
        name=TABLES_CONNECTOR_NAME,
        type=ConnectorType.tables,
        status=ConnectorStatus.active,
        config=None,
    )
    db.add(connector)
    await db.commit()
    await db.refresh(connector)
    log.info("auto-provisioned tables connector %s for org %s", connector.id, org_id)
    return connector


# ── Table CRUD ─────────────────────────────────────────────────────────────────

async def list_tables(db: AsyncSession, org_id: UUID) -> list[OrgTable]:
    rows = await db.exec(
        select(OrgTable)
        .where(OrgTable.org_id == org_id, OrgTable.deleted_at.is_(None))
        .order_by(OrgTable.created_at)
    )
    return list(rows.all())


async def get_table(db: AsyncSession, org_id: UUID, table_id: UUID) -> OrgTable:
    tbl = await db.get(OrgTable, table_id)
    if tbl is None or tbl.org_id != org_id or tbl.deleted_at is not None:
        raise TableError("Table not found")
    return tbl


async def get_table_by_slug(db: AsyncSession, org_id: UUID, slug: str) -> OrgTable | None:
    row = await db.exec(
        select(OrgTable).where(
            OrgTable.org_id == org_id,
            OrgTable.slug == slug,
            OrgTable.deleted_at.is_(None),
        )
    )
    return row.first()


async def create_table(
    db: AsyncSession,
    org_id: UUID,
    created_by: UUID,
    *,
    name: str,
    columns: list[dict[str, Any]],
    description: str = "",
    unique_on: list[str] | None = None,
) -> OrgTable:
    if len(columns) > MAX_COLUMNS_PER_TABLE:
        raise TableError(f"Maximum {MAX_COLUMNS_PER_TABLE} columns per table")

    table_slug = slugify(name)

    # Check slug uniqueness
    existing = await get_table_by_slug(db, org_id, table_slug)
    if existing is not None:
        raise TableError(f"A table named '{name}' (slug '{table_slug}') already exists in this org")

    validated_cols = [validate_column_def(c) for c in columns]
    _check_unique_keys(validated_cols)
    uon = unique_on or []
    _check_unique_on(validated_cols, uon)

    tbl = OrgTable(
        id=uuid4(),
        org_id=org_id,
        created_by=created_by,
        name=name,
        slug=table_slug,
        description=description,
        columns=validated_cols,
        unique_on=uon,
    )
    db.add(tbl)
    await db.commit()
    await db.refresh(tbl)

    await _log_event(db, org_id, tbl.id, action="schema", after={"name": name, "columns": validated_cols},
                     actor_user_id=created_by)
    return tbl


async def update_table_meta(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    actor_user_id: UUID | None = None,
) -> OrgTable:
    tbl = await get_table(db, org_id, table_id)
    if name is not None:
        tbl.name = name
    if description is not None:
        tbl.description = description
    tbl.updated_at = datetime.now(UTC)
    db.add(tbl)
    await db.commit()
    await db.refresh(tbl)
    await _log_event(db, org_id, table_id, action="schema", after={"name": tbl.name},
                     actor_user_id=actor_user_id)
    return tbl


async def add_column(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    col: dict[str, Any],
    *,
    actor_user_id: UUID | None = None,
) -> OrgTable:
    tbl = await get_table(db, org_id, table_id)
    if len(tbl.columns) >= MAX_COLUMNS_PER_TABLE:
        raise TableError(f"Maximum {MAX_COLUMNS_PER_TABLE} columns per table")
    validated = validate_column_def(col)
    keys = [c["key"] for c in tbl.columns]
    if validated["key"] in keys:
        raise TableError(f"Column key '{validated['key']}' already exists")
    tbl.columns = tbl.columns + [validated]
    tbl.updated_at = datetime.now(UTC)
    db.add(tbl)
    await db.commit()
    await db.refresh(tbl)
    await _log_event(db, org_id, table_id, action="schema", after={"added_column": validated["key"]},
                     actor_user_id=actor_user_id)
    return tbl


async def update_column(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    col_key: str,
    updates: dict[str, Any],
    *,
    actor_user_id: UUID | None = None,
) -> OrgTable:
    """Update a column definition.  Name changes are safe; type changes are validated."""
    tbl = await get_table(db, org_id, table_id)
    cols = list(tbl.columns)
    idx  = next((i for i, c in enumerate(cols) if c["key"] == col_key), None)
    if idx is None:
        raise TableError(f"Column '{col_key}' not found")
    merged = {**cols[idx], **updates, "key": col_key}  # key is immutable
    cols[idx] = validate_column_def(merged)
    tbl.columns = cols
    tbl.updated_at = datetime.now(UTC)
    db.add(tbl)
    await db.commit()
    await db.refresh(tbl)
    await _log_event(db, org_id, table_id, action="schema",
                     after={"updated_column": col_key, **updates},
                     actor_user_id=actor_user_id)
    return tbl


async def remove_column(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    col_key: str,
    *,
    actor_user_id: UUID | None = None,
) -> OrgTable:
    """Soft-remove a column: drop from schema, keep data (JSONB keys remain)."""
    tbl = await get_table(db, org_id, table_id)
    cols = [c for c in tbl.columns if c["key"] != col_key]
    if len(cols) == len(tbl.columns):
        raise TableError(f"Column '{col_key}' not found")
    tbl.columns = cols
    tbl.unique_on = [k for k in tbl.unique_on if k != col_key]
    tbl.updated_at = datetime.now(UTC)
    db.add(tbl)
    await db.commit()
    await db.refresh(tbl)
    await _log_event(db, org_id, table_id, action="schema",
                     after={"removed_column": col_key},
                     actor_user_id=actor_user_id)
    return tbl


async def delete_table(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    *,
    actor_user_id: UUID | None = None,
) -> None:
    """Soft-delete a table (sets deleted_at)."""
    tbl = await get_table(db, org_id, table_id)
    tbl.deleted_at = datetime.now(UTC)
    db.add(tbl)
    await db.commit()
    await _log_event(db, org_id, table_id, action="schema",
                     after={"deleted": True},
                     actor_user_id=actor_user_id)


# ── Row CRUD ───────────────────────────────────────────────────────────────────

async def list_rows(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    *,
    filters: dict[str, Any] | None = None,
    search: str | None = None,
    order_by: str | None = None,
    desc: bool = False,
    offset: int = 0,
    limit: int = 100,
    include_deleted: bool = False,
) -> tuple[list[OrgTableRow], int]:
    """Return (rows, total_count).  `filters` is a dict of {col_key: value}."""
    tbl = await get_table(db, org_id, table_id)

    stmt = select(OrgTableRow).where(
        OrgTableRow.org_id == org_id,
        OrgTableRow.table_id == table_id,
    )
    if not include_deleted:
        stmt = stmt.where(OrgTableRow.deleted_at.is_(None))

    # Apply equality filters via JSONB
    if filters:
        for k, v in filters.items():
            if v is not None:
                stmt = stmt.where(
                    OrgTableRow.data[k].astext == str(v)
                )

    # Simple text search across all JSONB values (cast to text)
    if search:
        from sqlalchemy import cast, Text
        stmt = stmt.where(cast(OrgTableRow.data, Text).ilike(f"%{search}%"))

    # Count
    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.exec(count_stmt)).one()

    # Order
    if order_by == "created_at":
        stmt = stmt.order_by(OrgTableRow.created_at.desc() if desc else OrgTableRow.created_at)
    elif order_by == "updated_at":
        stmt = stmt.order_by(OrgTableRow.updated_at.desc() if desc else OrgTableRow.updated_at)
    else:
        stmt = stmt.order_by(OrgTableRow.created_at.desc())

    stmt = stmt.offset(offset).limit(min(limit, 500))
    rows = await db.exec(stmt)
    return list(rows.all()), total


async def get_row(db: AsyncSession, org_id: UUID, table_id: UUID, row_id: UUID) -> OrgTableRow:
    row = await db.get(OrgTableRow, row_id)
    if row is None or row.org_id != org_id or row.table_id != table_id:
        raise TableError("Row not found")
    return row


async def create_row(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    data: dict[str, Any],
    *,
    actor_user_id: UUID | None = None,
    actor_session_id: UUID | None = None,
    agent_id: UUID | None = None,
    session_write_counter: dict | None = None,
) -> OrgTableRow:
    """Create a row.  If unique_on columns match an existing row, raises RowConflict."""
    tbl = await get_table(db, org_id, table_id)

    # Enforce per-session write cap for agents
    if session_write_counter is not None:
        _check_write_cap(session_write_counter)

    # Row count cap
    from sqlalchemy import func
    count = (await db.exec(
        select(func.count()).where(
            OrgTableRow.table_id == table_id,
            OrgTableRow.deleted_at.is_(None),
        )
    )).one()
    if count >= MAX_ROWS_PER_TABLE:
        raise TableError(f"Table has reached the maximum of {MAX_ROWS_PER_TABLE} rows")

    validated = validate_row_data(tbl.columns, data)

    # Dedup via unique_on
    if tbl.unique_on:
        conflict = await _find_unique_match(db, org_id, table_id, tbl.unique_on, validated)
        if conflict is not None:
            raise RowConflict(conflict)

    row = OrgTableRow(
        id=uuid4(),
        org_id=org_id,
        table_id=table_id,
        data=validated,
        version=1,
        created_by_user_id=actor_user_id,
        created_by_session_id=actor_session_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _log_event(db, org_id, table_id, row_id=row.id, action="create",
                     after=validated,
                     actor_user_id=actor_user_id,
                     actor_session_id=actor_session_id,
                     agent_id=agent_id)
    return row


async def update_row(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    row_id: UUID,
    data: dict[str, Any],
    *,
    expected_version: int | None = None,
    actor_user_id: UUID | None = None,
    actor_session_id: UUID | None = None,
    agent_id: UUID | None = None,
    session_write_counter: dict | None = None,
) -> OrgTableRow:
    """Partial update. Only provided keys are touched. expected_version enables optimistic locking."""
    tbl = await get_table(db, org_id, table_id)
    row = await get_row(db, org_id, table_id, row_id)
    if row.deleted_at is not None:
        raise TableError("Cannot update a deleted row")

    if expected_version is not None and row.version != expected_version:
        raise VersionConflict(f"Version mismatch: expected {expected_version}, got {row.version}")

    if session_write_counter is not None:
        _check_write_cap(session_write_counter)

    before_data = dict(row.data)
    patch = validate_row_data(tbl.columns, data, partial=True)
    row.data = {**row.data, **patch}
    row.version += 1
    row.updated_at = datetime.now(UTC)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _log_event(db, org_id, table_id, row_id=row_id, action="update",
                     before=before_data, after=dict(row.data),
                     actor_user_id=actor_user_id,
                     actor_session_id=actor_session_id,
                     agent_id=agent_id)
    return row


async def delete_row(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    row_id: UUID,
    *,
    actor_user_id: UUID | None = None,
    actor_session_id: UUID | None = None,
) -> OrgTableRow:
    """Soft-delete."""
    row = await get_row(db, org_id, table_id, row_id)
    if row.deleted_at is not None:
        raise TableError("Row is already deleted")
    row.deleted_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _log_event(db, org_id, table_id, row_id=row_id, action="delete",
                     before=dict(row.data),
                     actor_user_id=actor_user_id,
                     actor_session_id=actor_session_id)
    return row


async def restore_row(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    row_id: UUID,
    *,
    actor_user_id: UUID | None = None,
) -> OrgTableRow:
    row = await get_row(db, org_id, table_id, row_id)
    if row.deleted_at is None:
        raise TableError("Row is not deleted")
    row.deleted_at = None
    row.updated_at = datetime.now(UTC)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _log_event(db, org_id, table_id, row_id=row_id, action="restore",
                     after=dict(row.data),
                     actor_user_id=actor_user_id)
    return row


async def get_row_history(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    row_id: UUID,
    *,
    limit: int = 50,
) -> list[OrgTableEvent]:
    rows = await db.exec(
        select(OrgTableEvent)
        .where(
            OrgTableEvent.org_id == org_id,
            OrgTableEvent.table_id == table_id,
            OrgTableEvent.row_id == row_id,
        )
        .order_by(OrgTableEvent.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _find_unique_match(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    unique_on: list[str],
    data: dict[str, Any],
) -> OrgTableRow | None:
    """Find an existing non-deleted row where all unique_on columns match."""
    stmt = select(OrgTableRow).where(
        OrgTableRow.org_id == org_id,
        OrgTableRow.table_id == table_id,
        OrgTableRow.deleted_at.is_(None),
    )
    for key in unique_on:
        val = data.get(key)
        if val is None:
            return None  # can't match on null — no conflict
        stmt = stmt.where(OrgTableRow.data[key].astext == str(val))

    result = await db.exec(stmt.limit(1))
    return result.first()


async def _log_event(
    db: AsyncSession,
    org_id: UUID,
    table_id: UUID,
    *,
    action: str,
    row_id: UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    actor_user_id: UUID | None = None,
    actor_session_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> None:
    event = OrgTableEvent(
        id=uuid4(),
        org_id=org_id,
        table_id=table_id,
        row_id=row_id,
        action=action,
        before=before,
        after=after,
        actor_user_id=actor_user_id,
        actor_session_id=actor_session_id,
        agent_id=agent_id,
    )
    db.add(event)
    await db.commit()


def _check_unique_keys(cols: list[dict[str, Any]]) -> None:
    keys = [c["key"] for c in cols]
    if len(keys) != len(set(keys)):
        raise TableError("Duplicate column keys in column list")


def _check_unique_on(cols: list[dict[str, Any]], unique_on: list[str]) -> None:
    keys = {c["key"] for c in cols}
    for k in unique_on:
        if k not in keys:
            raise TableError(f"unique_on references unknown column key '{k}'")


def row_to_dict(row: OrgTableRow, columns: list[dict[str, Any]], *, agent_view: bool = False) -> dict[str, Any]:
    """Serialise a row to a plain dict for API/tool output."""
    data = strip_hidden(columns, row.data) if agent_view else dict(row.data)
    return {
        "id":         str(row.id),
        "version":    row.version,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "deleted":    row.deleted_at is not None,
        **data,
    }
