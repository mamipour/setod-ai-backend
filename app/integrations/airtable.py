"""
Airtable integration
====================
Auth: Personal Access Token (PAT) — ``Authorization: Bearer {token}``.
Required PAT scopes (set at airtable.com/create/tokens):
  • schema.bases:read
  • data.records:read
  • data.records:write

Tools (5):
  list_airtable_bases()
  list_airtable_records(base_id, table, filter?, limit?)
  find_airtable_record(base_id, table, formula)
  create_airtable_record(base_id, table, fields)
  update_airtable_record(base_id, table, record_id, fields)

All write tools honour dry_run.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_BASE = "https://api.airtable.com"
_META = "https://api.airtable.com/v0/meta"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _format_records(records: list[dict]) -> str:
    if not records:
        return "No records found."
    lines = []
    for rec in records:
        fields = rec.get("fields", {})
        rid = rec.get("id", "")
        summary = f"[{rid}] " + " | ".join(f"{k}: {v}" for k, v in list(fields.items())[:6])
        lines.append(summary)
    return "\n".join(lines)


async def test_connection(token: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_META}/bases?pageSize=1", headers=_headers(token))
    if resp.status_code == 401:
        raise IntegrationError("Invalid Airtable token")
    if resp.status_code == 403:
        raise IntegrationError(
            "Token lacks required scope 'schema.bases:read'. "
            "Regenerate the PAT at airtable.com/create/tokens with the required scopes."
        )
    if resp.status_code != 200:
        raise IntegrationError(f"Airtable error {resp.status_code}: {resp.text[:200]}")
    bases = resp.json().get("bases", [])
    count = len(resp.json().get("bases", []))
    name = bases[0].get("name", "") if bases else ""
    return f"Connected — {count} base(s) accessible" + (f", first: '{name}'" if name else "")


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    token: str = raw["api_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    # ── list_airtable_bases ───────────────────────────────────────────────────

    async def list_airtable_bases(args: dict[str, Any], dry_run: bool) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_META}/bases", headers=_headers(token))
        if resp.status_code != 200:
            raise IntegrationError(f"Airtable error {resp.status_code}: {resp.text[:300]}")
        bases = resp.json().get("bases", [])
        if not bases:
            return "No bases accessible. Grant this PAT access to at least one base."
        return "\n".join(f"• {b['name']} (id: {b['id']})" for b in bases)

    # ── list_airtable_records ─────────────────────────────────────────────────

    async def list_airtable_records(args: dict[str, Any], dry_run: bool) -> str:
        base_id = str(args.get("base_id", "")).strip()
        table = str(args.get("table", "")).strip()
        filter_formula = str(args.get("filter", "")).strip()
        limit = min(int(args.get("limit", 20)), 100)
        if not base_id or not table:
            return "Error: 'base_id' and 'table' are required."
        params: dict[str, Any] = {"maxRecords": limit}
        if filter_formula:
            params["filterByFormula"] = filter_formula
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/v0/{base_id}/{table}",
                headers=_headers(token),
                params=params,
            )
        if resp.status_code == 404:
            return f"Table '{table}' not found in base {base_id}."
        if resp.status_code == 422:
            return f"Invalid filter formula: {resp.json().get('error', {}).get('message', resp.text[:200])}"
        if resp.status_code != 200:
            raise IntegrationError(f"Airtable error {resp.status_code}: {resp.text[:300]}")
        return _format_records(resp.json().get("records", []))

    # ── find_airtable_record ──────────────────────────────────────────────────

    async def find_airtable_record(args: dict[str, Any], dry_run: bool) -> str:
        base_id = str(args.get("base_id", "")).strip()
        table = str(args.get("table", "")).strip()
        formula = str(args.get("formula", "")).strip()
        if not base_id or not table or not formula:
            return "Error: 'base_id', 'table', and 'formula' are required."
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/v0/{base_id}/{table}",
                headers=_headers(token),
                params={"filterByFormula": formula, "maxRecords": 1},
            )
        if resp.status_code == 404:
            return f"Table '{table}' not found in base {base_id}."
        if resp.status_code == 422:
            return f"Invalid formula: {resp.json().get('error', {}).get('message', resp.text[:200])}"
        if resp.status_code != 200:
            raise IntegrationError(f"Airtable error {resp.status_code}: {resp.text[:300]}")
        records = resp.json().get("records", [])
        if not records:
            return f"No record found matching formula: {formula}"
        return _format_records(records)

    # ── create_airtable_record ────────────────────────────────────────────────

    async def create_airtable_record(args: dict[str, Any], dry_run: bool) -> str:
        base_id = str(args.get("base_id", "")).strip()
        table = str(args.get("table", "")).strip()
        fields = args.get("fields", {})
        if not base_id or not table:
            return "Error: 'base_id' and 'table' are required."
        if not isinstance(fields, dict) or not fields:
            return "Error: 'fields' must be a non-empty object."
        if dry_run:
            return f"[simulated] Would create record in {base_id}/{table} with fields: {fields}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/v0/{base_id}/{table}",
                headers=_headers(token),
                json={"records": [{"fields": fields}]},
            )
        if resp.status_code == 422:
            return f"Invalid fields: {resp.json().get('error', {}).get('message', resp.text[:300])}"
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Airtable error {resp.status_code}: {resp.text[:300]}")
        rec = resp.json().get("records", [{}])[0]
        return f"Record created — id: {rec.get('id', '?')}"

    # ── update_airtable_record ────────────────────────────────────────────────

    async def update_airtable_record(args: dict[str, Any], dry_run: bool) -> str:
        base_id = str(args.get("base_id", "")).strip()
        table = str(args.get("table", "")).strip()
        record_id = str(args.get("record_id", "")).strip()
        fields = args.get("fields", {})
        if not base_id or not table or not record_id:
            return "Error: 'base_id', 'table', and 'record_id' are required."
        if not isinstance(fields, dict) or not fields:
            return "Error: 'fields' must be a non-empty object."
        if dry_run:
            return f"[simulated] Would update {record_id} in {base_id}/{table} with: {fields}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_BASE}/v0/{base_id}/{table}/{record_id}",
                headers=_headers(token),
                json={"fields": fields},
            )
        if resp.status_code == 404:
            return f"Record {record_id} not found in {base_id}/{table}."
        if resp.status_code == 422:
            return f"Invalid fields: {resp.json().get('error', {}).get('message', resp.text[:300])}"
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Airtable error {resp.status_code}: {resp.text[:300]}")
        return f"Record {record_id} updated."

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_airtable_bases"),
                description=(
                    "List all Airtable bases accessible to this token. "
                    "Returns base names and ids. Use a base id with other tools."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=list_airtable_bases,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_airtable_records"),
                description=(
                    "List records from an Airtable table. "
                    "Optionally filter using Airtable formula syntax, e.g. '{Status}=\"Open\"'. "
                    "Returns up to 'limit' records (default 20)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "base_id": {"type": "string", "description": "Airtable base id (from list_airtable_bases)."},
                        "table": {"type": "string", "description": "Table name (exact, case-sensitive)."},
                        "filter": {"type": "string", "description": "Airtable filter formula (optional)."},
                        "limit": {"type": "integer", "description": "Max records to return (default 20, max 100)."},
                    },
                    "required": ["base_id", "table"],
                },
            ),
            handler=list_airtable_records,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("find_airtable_record"),
                description=(
                    "Find the first Airtable record matching a formula. "
                    "Example formula: '{Email}=\"hello@example.com\"'. "
                    "Returns the matching record or 'not found'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "base_id": {"type": "string", "description": "Airtable base id."},
                        "table": {"type": "string", "description": "Table name."},
                        "formula": {"type": "string", "description": "Airtable filter formula to match the record."},
                    },
                    "required": ["base_id", "table", "formula"],
                },
            ),
            handler=find_airtable_record,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_airtable_record"),
                description=(
                    "Create a new record in an Airtable table. "
                    "Pass fields as a dict of column name → value."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "base_id": {"type": "string", "description": "Airtable base id."},
                        "table": {"type": "string", "description": "Table name."},
                        "fields": {
                            "type": "object",
                            "description": "Column names and values (e.g. {\"Name\": \"Acme\", \"Status\": \"New\"}).",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["base_id", "table", "fields"],
                },
            ),
            handler=create_airtable_record,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("update_airtable_record"),
                description=(
                    "Update specific fields on an existing Airtable record. "
                    "Only the listed fields are changed; all other fields are preserved. "
                    "Use find_airtable_record to get the record id first."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "base_id": {"type": "string", "description": "Airtable base id."},
                        "table": {"type": "string", "description": "Table name."},
                        "record_id": {"type": "string", "description": "Airtable record id (starts with 'rec')."},
                        "fields": {
                            "type": "object",
                            "description": "Fields to update.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["base_id", "table", "record_id", "fields"],
                },
            ),
            handler=update_airtable_record,
        ),
    ]
