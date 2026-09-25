"""
Google Sheets integration
=========================
Uses a platform-level service account stored in the connector's config (the service-account
JSON is encrypted at rest). The sheet owner must share the spreadsheet with the service-account
email — the connector card displays that email so it's easy to copy.

Tools:
  read_rows(spreadsheet_id, range)   — read a rectangular block, returned as CSV-style text
  append_row(spreadsheet_id, values) — append one row to the first sheet
  update_cell(spreadsheet_id, a1, value) — overwrite a single cell

All write tools honour dry_run.
"""

import json
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build
from googleapiclient.errors import HttpError

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _creds(sa_json: dict) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_info(sa_json, scopes=SCOPES)


def _service(sa_json: dict):
    return google_build("sheets", "v4", credentials=_creds(sa_json), cache_discovery=False)


def _sheet_id(spreadsheet_id_or_url: str) -> str:
    """Accept a spreadsheet URL or a bare ID."""
    if "/" in spreadsheet_id_or_url:
        parts = spreadsheet_id_or_url.split("/")
        try:
            d_idx = parts.index("d")
            return parts[d_idx + 1]
        except (ValueError, IndexError):
            pass
    return spreadsheet_id_or_url.strip()


async def validate(sa_json_str: str) -> str:
    """Parse the JSON and return the service-account email."""
    try:
        sa = json.loads(sa_json_str)
    except json.JSONDecodeError as exc:
        raise IntegrationError(f"Invalid JSON: {exc}") from exc
    email = sa.get("client_email", "")
    if not email:
        raise IntegrationError("Service account JSON missing 'client_email'")
    # Build the service to verify credentials parse without a real network call
    try:
        _creds(sa)
    except Exception as exc:
        raise IntegrationError(f"Could not load credentials: {exc}") from exc
    return email


def _rows_to_text(rows: list[list[Any]]) -> str:
    if not rows:
        return "(empty range)"
    lines = ["\t".join(str(cell) for cell in row) for row in rows]
    if len(lines) > 200:
        lines = lines[:200]
        lines.append(f"… ({len(rows) - 200} more rows truncated)")
    return "\n".join(lines)


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    raw = decrypt_json(ctx.connector.config)
    sa_json: dict = raw["sa_json"]

    # ── read_rows ──────────────────────────────────────────────────────────────

    async def read_rows(args: dict[str, Any], dry_run: bool) -> str:
        sid = _sheet_id(str(args.get("spreadsheet_id", raw.get("default_spreadsheet_id", ""))))
        rng = str(args.get("range", "Sheet1!A1:Z100")).strip() or "Sheet1!A1:Z100"
        if not sid:
            return "Error: 'spreadsheet_id' is required."
        try:
            svc = _service(sa_json)
            result = svc.spreadsheets().values().get(
                spreadsheetId=sid, range=rng
            ).execute()
            return _rows_to_text(result.get("values", []))
        except HttpError as exc:
            raise IntegrationError(f"Sheets API error: {exc.reason}") from exc

    # ── append_row ─────────────────────────────────────────────────────────────

    async def append_row(args: dict[str, Any], dry_run: bool) -> str:
        sid = _sheet_id(str(args.get("spreadsheet_id", raw.get("default_spreadsheet_id", ""))))
        values = args.get("values", [])
        if not sid:
            return "Error: 'spreadsheet_id' is required."
        if not isinstance(values, list):
            return "Error: 'values' must be a list of cell values."
        row = [str(v) for v in values]
        if dry_run:
            return f"[simulated] Would append row to {sid}: {row}"
        try:
            svc = _service(sa_json)
            svc.spreadsheets().values().append(
                spreadsheetId=sid,
                range="A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
            return f"Row appended to {sid}."
        except HttpError as exc:
            raise IntegrationError(f"Sheets API error: {exc.reason}") from exc

    # ── update_cell ────────────────────────────────────────────────────────────

    async def update_cell(args: dict[str, Any], dry_run: bool) -> str:
        sid = _sheet_id(str(args.get("spreadsheet_id", raw.get("default_spreadsheet_id", ""))))
        a1 = str(args.get("cell", "")).strip()
        value = str(args.get("value", ""))
        if not sid:
            return "Error: 'spreadsheet_id' is required."
        if not a1:
            return "Error: 'cell' is required (e.g. 'B3' or 'Sheet2!C5')."
        if dry_run:
            return f"[simulated] Would set {a1} = {value!r} in {sid}"
        try:
            svc = _service(sa_json)
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=a1,
                valueInputOption="USER_ENTERED",
                body={"values": [[value]]},
            ).execute()
            return f"Cell {a1} updated to {value!r}."
        except HttpError as exc:
            raise IntegrationError(f"Sheets API error: {exc.reason}") from exc

    # ── tool specs ─────────────────────────────────────────────────────────────

    def n(base: str) -> str:
        return ctx.tool_name(base)

    sheet_id_prop = {
        "spreadsheet_id": {
            "type": "string",
            "description": (
                "The Google Sheets spreadsheet ID or full URL. "
                "If omitted, the connector's default spreadsheet is used."
            ),
        }
    }

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("read_rows"),
                description=(
                    "Read a rectangular block of cells from a Google Sheet. "
                    "Returns tab-separated rows. Use for lookups, inventory checks, "
                    "reading a client list, or pulling data to summarise."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        **sheet_id_prop,
                        "range": {
                            "type": "string",
                            "description": "A1 notation range, e.g. 'Sheet1!A1:D50'. Defaults to Sheet1!A1:Z100.",
                        },
                    },
                    "required": [],
                },
            ),
            handler=read_rows,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("append_row"),
                description=(
                    "Append a new row at the bottom of a Google Sheet. "
                    "Use to log entries, add a new record, or write results."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        **sheet_id_prop,
                        "values": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of cell values for the new row, left to right.",
                        },
                    },
                    "required": ["values"],
                },
            ),
            handler=append_row,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("update_cell"),
                description=(
                    "Overwrite a single cell in a Google Sheet. "
                    "Use to mark a row as processed, update a status, or set a value."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        **sheet_id_prop,
                        "cell": {
                            "type": "string",
                            "description": "A1 notation cell address, e.g. 'B3' or 'Sheet2!C5'.",
                        },
                        "value": {
                            "type": "string",
                            "description": "The value to write into the cell.",
                        },
                    },
                    "required": ["cell", "value"],
                },
            ),
            handler=update_cell,
        ),
    ]
