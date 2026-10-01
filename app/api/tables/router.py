"""
Tables API
==========
CRUD for org-level typed tables and their rows.

Routes
------
GET    /tables/                                  list tables for an org
POST   /tables/                                  create table
GET    /tables/presets                           column preset definitions (e.g. "leads")
GET    /tables/{table_id}                        table detail with schema
PATCH  /tables/{table_id}                        update name / description (owner)
DELETE /tables/{table_id}                        soft-delete table (owner)

POST   /tables/{table_id}/columns               add column (owner)
PATCH  /tables/{table_id}/columns/{key}          update column (owner)
DELETE /tables/{table_id}/columns/{key}          remove column (owner)

GET    /tables/{table_id}/rows                   list rows (filter/sort/paginate)
POST   /tables/{table_id}/rows                   create row
GET    /tables/{table_id}/rows/{row_id}          get single row
PATCH  /tables/{table_id}/rows/{row_id}          partial update row (owner or API)
DELETE /tables/{table_id}/rows/{row_id}          soft-delete row (owner)
POST   /tables/{table_id}/rows/{row_id}/restore  restore soft-deleted row (owner)
GET    /tables/{table_id}/rows/{row_id}/history  audit history for row

GET    /tables/{table_id}/export                 CSV export of all rows (owner)
POST   /tables/{table_id}/import/preview         parse file, return inferred columns + sample
POST   /tables/{table_id}/import/commit          create or append rows from uploaded file
"""

import csv
import io
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.auth.dependencies import assert_org_owner, get_current_user
from app.core.tables import service as svc
from app.core.tables.schema import (
    ColumnError,
    COLUMN_TYPES,
    slugify,
    validate_column_def,
)
from app.db.models import OrganizationMember, User
from app.db.session import get_session

log = logging.getLogger(__name__)
router = APIRouter(prefix="/tables", tags=["tables"])


# ── Auth helpers ───────────────────────────────────────────────────────────────

async def _assert_member(session: AsyncSession, user: User, org_id: UUID) -> None:
    row = await session.exec(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if not row.first():
        raise HTTPException(status_code=403, detail="Not a member of this workspace")


# ── Output schemas ─────────────────────────────────────────────────────────────

class ColumnOut(BaseModel):
    key: str
    name: str
    type: str
    options: list[str] | None = None
    link_table_id: str | None = None
    required: bool = False
    hidden_from_agents: bool = False


class TableOut(BaseModel):
    id: str
    org_id: str
    name: str
    slug: str
    description: str
    columns: list[ColumnOut]
    unique_on: list[str]
    row_count: int = 0
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class RowOut(BaseModel):
    id: str
    version: int
    created_at: str
    updated_at: str
    deleted: bool
    data: dict[str, Any]


class EventOut(BaseModel):
    id: str
    action: str
    before: dict | None
    after: dict | None
    actor_user_id: str | None
    actor_session_id: str | None
    agent_id: str | None
    created_at: str


class RowListOut(BaseModel):
    rows: list[RowOut]
    total: int
    offset: int
    limit: int


# ── Input schemas ──────────────────────────────────────────────────────────────

class TableCreate(BaseModel):
    org_id: UUID
    name: str
    description: str = ""
    columns: list[dict[str, Any]] = []
    unique_on: list[str] = []


class TablePatch(BaseModel):
    name: str | None = None
    description: str | None = None


class ColumnPatch(BaseModel):
    name: str | None = None
    type: str | None = None
    options: list[str] | None = None
    required: bool | None = None
    hidden_from_agents: bool | None = None


class RowCreate(BaseModel):
    data: dict[str, Any]


class RowPatch(BaseModel):
    data: dict[str, Any]
    expected_version: int | None = None


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _table_out(tbl, row_count: int = 0) -> TableOut:
    return TableOut(
        id=str(tbl.id),
        org_id=str(tbl.org_id),
        name=tbl.name,
        slug=tbl.slug,
        description=tbl.description,
        columns=[ColumnOut(**{k: v for k, v in c.items() if k in ColumnOut.model_fields}) for c in tbl.columns],
        unique_on=tbl.unique_on or [],
        row_count=row_count,
        created_at=tbl.created_at.isoformat(),
        updated_at=tbl.updated_at.isoformat(),
        deleted_at=tbl.deleted_at.isoformat() if tbl.deleted_at else None,
    )


def _row_out(row) -> RowOut:
    return RowOut(
        id=str(row.id),
        version=row.version,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        deleted=row.deleted_at is not None,
        data=row.data or {},
    )


def _event_out(ev) -> EventOut:
    return EventOut(
        id=str(ev.id),
        action=ev.action,
        before=ev.before,
        after=ev.after,
        actor_user_id=str(ev.actor_user_id) if ev.actor_user_id else None,
        actor_session_id=str(ev.actor_session_id) if ev.actor_session_id else None,
        agent_id=str(ev.agent_id) if ev.agent_id else None,
        created_at=ev.created_at.isoformat(),
    )


async def _row_count(db: AsyncSession, table_id: UUID) -> int:
    from sqlalchemy import func
    from app.db.models import OrgTableRow
    result = await db.exec(
        select(func.count()).where(
            OrgTableRow.table_id == table_id,
            OrgTableRow.deleted_at.is_(None),
        )
    )
    return result.one()


# ── Presets ────────────────────────────────────────────────────────────────────

PRESETS: dict[str, dict] = {
    "leads": {
        "name": "Leads",
        "description": "Prospective customers and inbound contacts",
        "columns": [
            {"key": "name",         "name": "Name",         "type": "text",      "required": True},
            {"key": "contact",      "name": "Contact",      "type": "phone",     "required": False},
            {"key": "email",        "name": "Email",        "type": "email",     "required": False},
            {"key": "source",       "name": "Source",       "type": "select",    "options": ["instagram", "telegram", "website", "referral", "other"]},
            {"key": "status",       "name": "Status",       "type": "select",    "options": ["new", "contacted", "qualified", "lost"]},
            {"key": "notes",        "name": "Notes",        "type": "long_text"},
            {"key": "last_contact", "name": "Last Contact", "type": "datetime"},
        ],
        "unique_on": ["contact"],
    },
}


@router.get("/presets")
async def list_presets():
    return {"presets": list(PRESETS.keys()), "definitions": PRESETS}


@router.get("/presets/{key}")
async def get_preset(key: str):
    p = PRESETS.get(key)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Preset '{key}' not found")
    return p


# ── Table endpoints ────────────────────────────────────────────────────────────

@router.get("/")
async def list_tables(
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[TableOut]:
    await _assert_member(db, current_user, org_id)
    tables = await svc.list_tables(db, org_id)
    out = []
    for tbl in tables:
        rc = await _row_count(db, tbl.id)
        out.append(_table_out(tbl, rc))
    return out


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_table(
    body: TableCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> TableOut:
    await _assert_member(db, current_user, body.org_id)
    try:
        tbl = await svc.create_table(
            db, body.org_id, current_user.id,
            name=body.name,
            columns=body.columns,
            description=body.description,
            unique_on=body.unique_on,
        )
    except (ColumnError, svc.TableError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    # Auto-provision the tables connector so agents can attach it immediately
    await svc.get_or_create_tables_connector(db, body.org_id, current_user.id)
    return _table_out(tbl)


@router.get("/{table_id}")
async def get_table(
    table_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> TableOut:
    await _assert_member(db, current_user, org_id)
    try:
        tbl = await svc.get_table(db, org_id, table_id)
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))
    rc = await _row_count(db, tbl.id)
    return _table_out(tbl, rc)


@router.patch("/{table_id}")
async def update_table(
    table_id: UUID,
    body: TablePatch,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> TableOut:
    await assert_org_owner(db, current_user, org_id)
    try:
        tbl = await svc.update_table_meta(
            db, org_id, table_id,
            name=body.name,
            description=body.description,
            actor_user_id=current_user.id,
        )
    except svc.TableError as e:
        raise HTTPException(status_code=422, detail=str(e))
    rc = await _row_count(db, tbl.id)
    return _table_out(tbl, rc)


@router.delete("/{table_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_table(
    table_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    await assert_org_owner(db, current_user, org_id)
    try:
        await svc.delete_table(db, org_id, table_id, actor_user_id=current_user.id)
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Column endpoints ───────────────────────────────────────────────────────────

@router.post("/{table_id}/columns", status_code=status.HTTP_201_CREATED)
async def add_column(
    table_id: UUID,
    body: dict,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> TableOut:
    await assert_org_owner(db, current_user, org_id)
    try:
        tbl = await svc.add_column(db, org_id, table_id, body, actor_user_id=current_user.id)
    except (ColumnError, svc.TableError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    rc = await _row_count(db, tbl.id)
    return _table_out(tbl, rc)


@router.patch("/{table_id}/columns/{col_key}")
async def update_column(
    table_id: UUID,
    col_key: str,
    body: ColumnPatch,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> TableOut:
    await assert_org_owner(db, current_user, org_id)
    updates = body.model_dump(exclude_none=True)
    try:
        tbl = await svc.update_column(db, org_id, table_id, col_key, updates, actor_user_id=current_user.id)
    except (ColumnError, svc.TableError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    rc = await _row_count(db, tbl.id)
    return _table_out(tbl, rc)


@router.delete("/{table_id}/columns/{col_key}", status_code=status.HTTP_200_OK)
async def remove_column(
    table_id: UUID,
    col_key: str,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> TableOut:
    await assert_org_owner(db, current_user, org_id)
    try:
        tbl = await svc.remove_column(db, org_id, table_id, col_key, actor_user_id=current_user.id)
    except svc.TableError as e:
        raise HTTPException(status_code=422, detail=str(e))
    rc = await _row_count(db, tbl.id)
    return _table_out(tbl, rc)


# ── Row endpoints ──────────────────────────────────────────────────────────────

@router.get("/{table_id}/rows")
async def list_rows(
    table_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    order_by: str | None = Query(None),
    desc: bool = Query(False),
    search: str | None = Query(None),
    include_deleted: bool = Query(False),
) -> RowListOut:
    await _assert_member(db, current_user, org_id)
    try:
        rows, total = await svc.list_rows(
            db, org_id, table_id,
            search=search,
            order_by=order_by,
            desc=desc,
            offset=offset,
            limit=limit,
            include_deleted=include_deleted,
        )
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return RowListOut(
        rows=[_row_out(r) for r in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post("/{table_id}/rows", status_code=status.HTTP_201_CREATED)
async def create_row(
    table_id: UUID,
    body: RowCreate,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> RowOut:
    await _assert_member(db, current_user, org_id)
    try:
        row = await svc.create_row(
            db, org_id, table_id, body.data,
            actor_user_id=current_user.id,
        )
    except svc.RowConflict as e:
        # Return 200 with existing row rather than error
        return _row_out(e.existing_row)
    except (ColumnError, svc.TableError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _row_out(row)


@router.get("/{table_id}/rows/{row_id}")
async def get_row(
    table_id: UUID,
    row_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> RowOut:
    await _assert_member(db, current_user, org_id)
    try:
        row = await svc.get_row(db, org_id, table_id, row_id)
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _row_out(row)


@router.patch("/{table_id}/rows/{row_id}")
async def update_row(
    table_id: UUID,
    row_id: UUID,
    body: RowPatch,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> RowOut:
    await _assert_member(db, current_user, org_id)
    try:
        row = await svc.update_row(
            db, org_id, table_id, row_id, body.data,
            expected_version=body.expected_version,
            actor_user_id=current_user.id,
        )
    except svc.VersionConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ColumnError, svc.TableError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _row_out(row)


@router.delete("/{table_id}/rows/{row_id}", status_code=status.HTTP_200_OK)
async def delete_row(
    table_id: UUID,
    row_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> RowOut:
    await assert_org_owner(db, current_user, org_id)
    try:
        row = await svc.delete_row(db, org_id, table_id, row_id, actor_user_id=current_user.id)
    except svc.TableError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _row_out(row)


@router.post("/{table_id}/rows/{row_id}/restore")
async def restore_row(
    table_id: UUID,
    row_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> RowOut:
    await assert_org_owner(db, current_user, org_id)
    try:
        row = await svc.restore_row(db, org_id, table_id, row_id, actor_user_id=current_user.id)
    except svc.TableError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _row_out(row)


@router.get("/{table_id}/rows/{row_id}/history")
async def row_history(
    table_id: UUID,
    row_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[EventOut]:
    await _assert_member(db, current_user, org_id)
    try:
        await svc.get_table(db, org_id, table_id)  # access check
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))
    events = await svc.get_row_history(db, org_id, table_id, row_id)
    return [_event_out(ev) for ev in events]


# ── Export ─────────────────────────────────────────────────────────────────────

@router.get("/{table_id}/export")
async def export_csv(
    table_id: UUID,
    org_id: Annotated[UUID, Query()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_member(db, current_user, org_id)
    try:
        tbl = await svc.get_table(db, org_id, table_id)
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))

    rows, _ = await svc.list_rows(db, org_id, table_id, limit=50_000)

    buf = io.StringIO()
    headers = ["id", "created_at"] + [c["key"] for c in tbl.columns]
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        vals = [str(row.id), row.created_at.isoformat()] + [
            _formula_escape(str(row.data.get(c["key"], "") or ""))
            for c in tbl.columns
        ]
        writer.writerow(vals)

    buf.seek(0)
    filename = f"{tbl.slug}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _formula_escape(s: str) -> str:
    """Prefix cells that would be interpreted as formulas by spreadsheet apps."""
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


# ── Import ─────────────────────────────────────────────────────────────────────

@router.post("/{table_id}/import/preview")
async def import_preview(
    table_id: UUID,
    org_id: Annotated[UUID, Query()],
    file: UploadFile,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_member(db, current_user, org_id)
    try:
        tbl = await svc.get_table(db, org_id, table_id)
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))

    from app.core.tables.importer import preview_import
    raw = await file.read(10 * 1024 * 1024)  # 10 MB cap
    try:
        result = preview_import(raw, filename=file.filename or "", columns=tbl.columns)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result


@router.post("/{table_id}/import/commit", status_code=status.HTTP_201_CREATED)
async def import_commit(
    table_id: UUID,
    org_id: Annotated[UUID, Query()],
    file: UploadFile,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    await _assert_member(db, current_user, org_id)
    try:
        tbl = await svc.get_table(db, org_id, table_id)
    except svc.TableError as e:
        raise HTTPException(status_code=404, detail=str(e))

    from app.core.tables.importer import parse_rows
    raw = await file.read(10 * 1024 * 1024)
    try:
        parsed_rows = parse_rows(raw, filename=file.filename or "", columns=tbl.columns)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    created = 0
    conflicts = 0
    errors = 0
    for row_data in parsed_rows:
        try:
            await svc.create_row(
                db, org_id, table_id, row_data,
                actor_user_id=current_user.id,
            )
            created += 1
        except svc.RowConflict:
            conflicts += 1
        except Exception as e:
            log.warning("import row error: %s", e)
            errors += 1

    return {"created": created, "conflicts": conflicts, "errors": errors, "total": len(parsed_rows)}
