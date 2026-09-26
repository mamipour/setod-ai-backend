"""
Pipedrive CRM integration
=========================
Auth: Pipedrive API token — ``Authorization: Bearer {token}``.
API: All endpoints on /api/v2 (v1 deprecated for core objects as of 2026).

Tools (7):
  find_pipedrive_person(email)
  create_pipedrive_person(name, email?, phone?, org_name?)
  update_pipedrive_person(person_id, fields)
  create_pipedrive_deal(title, person_id, value?, stage_id?, pipeline_id?)
  move_pipedrive_deal(deal_id, stage_id)
  log_pipedrive_activity(person_id, deal_id?, note)
  list_pipedrive_stages(pipeline_id?)

All write tools honour dry_run.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_BASE = "https://api.pipedrive.com"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-api-version": "2024-11-28",
    }


async def test_connection(token: str) -> str:
    """Validate the token by listing one person. Returns a status string."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_BASE}/api/v2/persons",
            headers=_headers(token),
            params={"limit": 1},
        )
    if resp.status_code == 401:
        raise IntegrationError("Invalid or expired API token")
    if resp.status_code == 403:
        raise IntegrationError("Token lacks required permissions")
    if resp.status_code != 200:
        raise IntegrationError(f"Pipedrive returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("success"):
        raise IntegrationError(data.get("error", "Pipedrive API error"))
    total = data.get("additional_data", {}).get("total_count", "?")
    return f"Connected — {total} person(s) in account"


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    token: str = raw["api_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    # ── find_pipedrive_person ──────────────────────────────────────────────────

    async def find_pipedrive_person(args: dict[str, Any], dry_run: bool) -> str:
        email = str(args.get("email", "")).strip()
        if not email:
            return "Error: 'email' is required."
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/api/v2/persons/search",
                headers=_headers(token),
                params={"term": email, "fields": "email", "limit": 1},
            )
        if resp.status_code != 200:
            raise IntegrationError(f"Pipedrive search error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            return f"No person found with email {email}."
        p = items[0].get("item", {})
        pid = p.get("id")
        name = p.get("name", "(unnamed)")
        phones = ", ".join(ph.get("value", "") for ph in (p.get("phones") or []) if ph.get("value"))
        org = p.get("organization", {}).get("name", "") if p.get("organization") else ""
        return (
            f"Person found — id: {pid}, name: {name}, email: {email}, "
            f"phone: {phones or '(none)'}, org: {org or '(none)'}"
        )

    # ── create_pipedrive_person ────────────────────────────────────────────────

    async def create_pipedrive_person(args: dict[str, Any], dry_run: bool) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "Error: 'name' is required."
        payload: dict[str, Any] = {"name": name}
        if args.get("email"):
            payload["emails"] = [{"value": str(args["email"]).strip(), "primary": True}]
        if args.get("phone"):
            payload["phones"] = [{"value": str(args["phone"]).strip(), "primary": True}]
        if args.get("org_name"):
            # Pipedrive v2 accepts org_name directly; it creates the org if not found
            payload["org_name"] = str(args["org_name"]).strip()
        if dry_run:
            return f"[simulated] Would create Pipedrive person: {payload}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/api/v2/persons",
                headers=_headers(token),
                json=payload,
            )
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Pipedrive error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        pid = data.get("data", {}).get("id")
        return f"Person created — id: {pid}, name: {name}"

    # ── update_pipedrive_person ────────────────────────────────────────────────

    async def update_pipedrive_person(args: dict[str, Any], dry_run: bool) -> str:
        person_id = str(args.get("person_id", "")).strip()
        fields = args.get("fields", {})
        if not person_id:
            return "Error: 'person_id' is required."
        if not isinstance(fields, dict) or not fields:
            return "Error: 'fields' must be a non-empty dict of Pipedrive field keys to values."
        if dry_run:
            return f"[simulated] Would update person {person_id} with: {fields}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_BASE}/api/v2/persons/{person_id}",
                headers=_headers(token),
                json=fields,
            )
        if resp.status_code == 404:
            return f"Person {person_id} not found."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Pipedrive error {resp.status_code}: {resp.text[:300]}")
        return f"Person {person_id} updated."

    # ── create_pipedrive_deal ──────────────────────────────────────────────────

    async def create_pipedrive_deal(args: dict[str, Any], dry_run: bool) -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "Error: 'title' is required."
        payload: dict[str, Any] = {"title": title}
        if args.get("person_id"):
            payload["person_id"] = int(args["person_id"])
        if args.get("value") is not None:
            payload["value"] = args["value"]
        if args.get("stage_id"):
            payload["stage_id"] = int(args["stage_id"])
        if args.get("pipeline_id"):
            payload["pipeline_id"] = int(args["pipeline_id"])
        if dry_run:
            return f"[simulated] Would create Pipedrive deal '{title}'" + (
                f" linked to person {args.get('person_id')}" if args.get("person_id") else ""
            )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/api/v2/deals",
                headers=_headers(token),
                json=payload,
            )
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Pipedrive error {resp.status_code}: {resp.text[:300]}")
        deal_id = resp.json().get("data", {}).get("id")
        return f"Deal created — id: {deal_id}, title: '{title}'"

    # ── move_pipedrive_deal ────────────────────────────────────────────────────

    async def move_pipedrive_deal(args: dict[str, Any], dry_run: bool) -> str:
        deal_id = str(args.get("deal_id", "")).strip()
        stage_id = str(args.get("stage_id", "")).strip()
        if not deal_id or not stage_id:
            return "Error: 'deal_id' and 'stage_id' are required."
        if dry_run:
            return f"[simulated] Would move deal {deal_id} to stage {stage_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_BASE}/api/v2/deals/{deal_id}",
                headers=_headers(token),
                json={"stage_id": int(stage_id)},
            )
        if resp.status_code == 404:
            return f"Deal {deal_id} not found."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Pipedrive error {resp.status_code}: {resp.text[:300]}")
        return f"Deal {deal_id} moved to stage {stage_id}."

    # ── log_pipedrive_activity ─────────────────────────────────────────────────

    async def log_pipedrive_activity(args: dict[str, Any], dry_run: bool) -> str:
        person_id = args.get("person_id")
        deal_id = args.get("deal_id")
        note = str(args.get("note", "")).strip()
        if not note:
            return "Error: 'note' is required."
        payload: dict[str, Any] = {
            "subject": note[:255],
            "type": "note",
            "note": note,
            "done": 1,
        }
        if person_id:
            payload["person_id"] = int(person_id)
        if deal_id:
            payload["deal_id"] = int(deal_id)
        if dry_run:
            return f"[simulated] Would log Pipedrive activity for person {person_id}: {note[:80]}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/api/v2/activities",
                headers=_headers(token),
                json=payload,
            )
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Pipedrive error {resp.status_code}: {resp.text[:300]}")
        act_id = resp.json().get("data", {}).get("id")
        return f"Activity logged — id: {act_id}"

    # ── list_pipedrive_stages ──────────────────────────────────────────────────

    async def list_pipedrive_stages(args: dict[str, Any], dry_run: bool) -> str:
        pipeline_id = args.get("pipeline_id")
        params: dict[str, Any] = {}
        if pipeline_id:
            params["pipeline_id"] = int(pipeline_id)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/api/v2/stages",
                headers=_headers(token),
                params=params,
            )
        if resp.status_code != 200:
            raise IntegrationError(f"Pipedrive stages error {resp.status_code}: {resp.text[:300]}")
        stages = resp.json().get("data", []) or []
        if not stages:
            return "No stages found."
        lines = [f"Stage: {s.get('name', '?')} (id: {s.get('id', '?')}, pipeline_id: {s.get('pipeline_id', '?')})" for s in stages]
        return "\n".join(lines)

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("find_pipedrive_person"),
                description=(
                    "Look up a Pipedrive person by email address. "
                    "Returns the person id, name, phone, and organisation — or 'not found'. "
                    "Use the id with create_pipedrive_deal or log_pipedrive_activity."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "The person's email address."},
                    },
                    "required": ["email"],
                },
            ),
            handler=find_pipedrive_person,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_pipedrive_person"),
                description=(
                    "Create a new person in Pipedrive. Returns the new person id. "
                    "If org_name is given, Pipedrive will link to an existing organisation "
                    "or create a new one."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Full name (required)."},
                        "email": {"type": "string", "description": "Email address."},
                        "phone": {"type": "string", "description": "Phone number."},
                        "org_name": {"type": "string", "description": "Organisation name."},
                    },
                    "required": ["name"],
                },
            ),
            handler=create_pipedrive_person,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("update_pipedrive_person"),
                description=(
                    "Update one or more fields on an existing Pipedrive person. "
                    "Pass the person id from find_pipedrive_person or create_pipedrive_person. "
                    "Fields is a dict of Pipedrive field keys to new values."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "person_id": {"type": "string", "description": "Pipedrive person id."},
                        "fields": {
                            "type": "object",
                            "description": "Field keys and new values.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["person_id", "fields"],
                },
            ),
            handler=update_pipedrive_person,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_pipedrive_deal"),
                description=(
                    "Create a new deal in Pipedrive and optionally link it to a person. "
                    "Use list_pipedrive_stages to see valid stage_id and pipeline_id values. "
                    "Returns the new deal id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Deal title (required)."},
                        "person_id": {"type": "integer", "description": "Pipedrive person id to link."},
                        "value": {"type": "number", "description": "Deal value."},
                        "stage_id": {"type": "integer", "description": "Stage id (from list_pipedrive_stages)."},
                        "pipeline_id": {"type": "integer", "description": "Pipeline id (from list_pipedrive_stages)."},
                    },
                    "required": ["title"],
                },
            ),
            handler=create_pipedrive_deal,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("move_pipedrive_deal"),
                description=(
                    "Move a Pipedrive deal to a different stage. "
                    "Use list_pipedrive_stages to see valid stage ids."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "deal_id": {"type": "string", "description": "Pipedrive deal id."},
                        "stage_id": {"type": "string", "description": "Target stage id."},
                    },
                    "required": ["deal_id", "stage_id"],
                },
            ),
            handler=move_pipedrive_deal,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("log_pipedrive_activity"),
                description=(
                    "Log a note/activity on a Pipedrive person or deal. "
                    "Marked as done immediately. Use for call logs, conversation summaries, "
                    "or any freetext record."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "person_id": {"type": "integer", "description": "Pipedrive person id."},
                        "deal_id": {"type": "integer", "description": "Pipedrive deal id (optional)."},
                        "note": {"type": "string", "description": "Activity note text."},
                    },
                    "required": ["note"],
                },
            ),
            handler=log_pipedrive_activity,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_pipedrive_stages"),
                description=(
                    "List Pipedrive pipeline stages with their ids. "
                    "Call this before create_pipedrive_deal or move_pipedrive_deal "
                    "to find the correct stage_id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pipeline_id": {
                            "type": "integer",
                            "description": "Filter to a single pipeline. Omit to list all stages.",
                        },
                    },
                    "required": [],
                },
            ),
            handler=list_pipedrive_stages,
        ),
    ]
