"""
HubSpot CRM integration
=======================
Auth: HubSpot Private App token — ``Authorization: Bearer {token}``.
API versions:
  - Contacts / Deals: /crm/objects/2026-03/{type}   (latest versioned API)
  - Notes:            /crm/v3/objects/notes          (stable legacy)
  - Pipelines:        /crm/v3/pipelines/deals

Tools (7):
  find_hubspot_contact(email)
  create_hubspot_contact(email, firstname, lastname?, phone?, company?)
  update_hubspot_contact(contact_id, fields)
  create_hubspot_deal(title, contact_id, value?, stage?, pipeline?)
  move_hubspot_deal(deal_id, stage)
  log_hubspot_note(contact_id, text)
  list_hubspot_pipeline_stages(pipeline?)

All write tools honour dry_run.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

_BASE = "https://api.hubapi.com"
_CONTACTS_API = f"{_BASE}/crm/objects/2026-03/contacts"
_DEALS_API = f"{_BASE}/crm/objects/2026-03/deals"
_NOTES_API = f"{_BASE}/crm/v3/objects/notes"
_PIPELINES_API = f"{_BASE}/crm/v3/pipelines/deals"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def test_connection(token: str) -> str:
    """Validate the token by listing one contact. Returns a status string."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            _CONTACTS_API,
            headers=_headers(token),
            params={"limit": 1},
        )
    if resp.status_code == 401:
        raise IntegrationError("Invalid or expired Private App token")
    if resp.status_code == 403:
        raise IntegrationError("Token lacks required scopes (crm.objects.contacts.read)")
    if resp.status_code != 200:
        raise IntegrationError(f"HubSpot returned {resp.status_code}: {resp.text[:200]}")
    total = resp.json().get("total", "?")
    return f"Connected — {total} contact(s) in account"


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:  # noqa: C901
    raw = decrypt_json(ctx.connector.config)
    token: str = raw["api_token"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    # ── find_hubspot_contact ───────────────────────────────────────────────────

    async def find_hubspot_contact(args: dict[str, Any], dry_run: bool) -> str:
        email = str(args.get("email", "")).strip()
        if not email:
            return "Error: 'email' is required."
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BASE}/crm/objects/2026-03/contacts/search",
                headers=_headers(token),
                json={
                    "filterGroups": [{
                        "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
                    }],
                    "properties": ["firstname", "lastname", "email", "phone", "company"],
                    "limit": 1,
                },
            )
        if resp.status_code != 200:
            raise IntegrationError(f"HubSpot search error {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("results", [])
        if not results:
            return f"No contact found with email {email}."
        c = results[0]
        p = c.get("properties", {})
        name = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip() or "(unnamed)"
        return (
            f"Contact found — id: {c['id']}, name: {name}, email: {p.get('email', '')}, "
            f"phone: {p.get('phone', '')}, company: {p.get('company', '')}"
        )

    # ── create_hubspot_contact ─────────────────────────────────────────────────

    async def create_hubspot_contact(args: dict[str, Any], dry_run: bool) -> str:
        email = str(args.get("email", "")).strip()
        firstname = str(args.get("firstname", "")).strip()
        if not email:
            return "Error: 'email' is required."
        props: dict[str, str] = {"email": email}
        if firstname:
            props["firstname"] = firstname
        if args.get("lastname"):
            props["lastname"] = str(args["lastname"]).strip()
        if args.get("phone"):
            props["phone"] = str(args["phone"]).strip()
        if args.get("company"):
            props["company"] = str(args["company"]).strip()
        if dry_run:
            return f"[simulated] Would create HubSpot contact: {props}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _CONTACTS_API,
                headers=_headers(token),
                json={"properties": props},
            )
        if resp.status_code == 409:
            return f"Contact with email {email} already exists."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"HubSpot error {resp.status_code}: {resp.text[:300]}")
        cid = resp.json()["id"]
        return f"Contact created — id: {cid}, email: {email}"

    # ── update_hubspot_contact ─────────────────────────────────────────────────

    async def update_hubspot_contact(args: dict[str, Any], dry_run: bool) -> str:
        contact_id = str(args.get("contact_id", "")).strip()
        fields = args.get("fields", {})
        if not contact_id:
            return "Error: 'contact_id' is required."
        if not isinstance(fields, dict) or not fields:
            return "Error: 'fields' must be a non-empty dict of HubSpot property names to values."
        if dry_run:
            return f"[simulated] Would update contact {contact_id} with: {fields}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_CONTACTS_API}/{contact_id}",
                headers=_headers(token),
                json={"properties": {k: str(v) for k, v in fields.items()}},
            )
        if resp.status_code == 404:
            return f"Contact {contact_id} not found."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"HubSpot error {resp.status_code}: {resp.text[:300]}")
        return f"Contact {contact_id} updated."

    # ── create_hubspot_deal ────────────────────────────────────────────────────

    async def create_hubspot_deal(args: dict[str, Any], dry_run: bool) -> str:
        title = str(args.get("title", "")).strip()
        contact_id = str(args.get("contact_id", "")).strip()
        if not title:
            return "Error: 'title' is required."
        props: dict[str, str] = {"dealname": title}
        if args.get("value"):
            props["amount"] = str(args["value"])
        if args.get("stage"):
            props["dealstage"] = str(args["stage"])
        if args.get("pipeline"):
            props["pipeline"] = str(args["pipeline"])
        payload: dict[str, Any] = {"properties": props}
        if contact_id:
            payload["associations"] = [{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}],
            }]
        if dry_run:
            return f"[simulated] Would create deal '{title}'" + (f" linked to contact {contact_id}" if contact_id else "")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _DEALS_API,
                headers=_headers(token),
                json=payload,
            )
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"HubSpot error {resp.status_code}: {resp.text[:300]}")
        deal_id = resp.json()["id"]
        return f"Deal created — id: {deal_id}, title: '{title}'"

    # ── move_hubspot_deal ──────────────────────────────────────────────────────

    async def move_hubspot_deal(args: dict[str, Any], dry_run: bool) -> str:
        deal_id = str(args.get("deal_id", "")).strip()
        stage = str(args.get("stage", "")).strip()
        if not deal_id or not stage:
            return "Error: 'deal_id' and 'stage' are required."
        if dry_run:
            return f"[simulated] Would move deal {deal_id} to stage '{stage}'"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_DEALS_API}/{deal_id}",
                headers=_headers(token),
                json={"properties": {"dealstage": stage}},
            )
        if resp.status_code == 404:
            return f"Deal {deal_id} not found."
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"HubSpot error {resp.status_code}: {resp.text[:300]}")
        return f"Deal {deal_id} moved to stage '{stage}'."

    # ── log_hubspot_note ───────────────────────────────────────────────────────

    async def log_hubspot_note(args: dict[str, Any], dry_run: bool) -> str:
        contact_id = str(args.get("contact_id", "")).strip()
        text = str(args.get("text", "")).strip()
        if not contact_id or not text:
            return "Error: 'contact_id' and 'text' are required."
        if dry_run:
            return f"[simulated] Would log note on contact {contact_id}: {text[:80]}"
        async with httpx.AsyncClient(timeout=15) as client:
            # Create the note
            create_resp = await client.post(
                _NOTES_API,
                headers=_headers(token),
                json={
                    "properties": {
                        "hs_note_body": text,
                        "hs_timestamp": str(int(__import__("time").time() * 1000)),
                    }
                },
            )
            if create_resp.status_code not in (200, 201):
                raise IntegrationError(f"HubSpot note create error {create_resp.status_code}: {create_resp.text[:300]}")
            note_id = create_resp.json()["id"]

            # Associate with the contact (associationTypeId 202 = note_to_contact)
            assoc_resp = await client.put(
                f"{_NOTES_API}/{note_id}/associations/contact/{contact_id}/202",
                headers=_headers(token),
            )
        if assoc_resp.status_code not in (200, 201):
            return f"Note {note_id} created but association failed ({assoc_resp.status_code})."
        return f"Note logged on contact {contact_id} (note id: {note_id})."

    # ── list_hubspot_pipeline_stages ──────────────────────────────────────────

    async def list_hubspot_pipeline_stages(args: dict[str, Any], dry_run: bool) -> str:
        pipeline_filter = str(args.get("pipeline", "")).strip().lower()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_PIPELINES_API, headers=_headers(token))
        if resp.status_code != 200:
            raise IntegrationError(f"HubSpot pipelines error {resp.status_code}: {resp.text[:300]}")
        pipelines = resp.json().get("results", [])
        lines: list[str] = []
        for pl in pipelines:
            pl_label = pl.get("label", pl.get("id", "?"))
            if pipeline_filter and pipeline_filter not in pl_label.lower():
                continue
            lines.append(f"Pipeline: {pl_label} (id: {pl.get('id', '?')})")
            for stage in pl.get("stages", []):
                lines.append(f"  Stage: {stage.get('label', '?')} (id: {stage.get('id', '?')})")
        return "\n".join(lines) if lines else "No pipelines found."

    # ── tool specs ─────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("find_hubspot_contact"),
                description=(
                    "Look up a HubSpot contact by email address. "
                    "Returns the contact id, name, phone, and company — or 'not found'. "
                    "Use the id with create_hubspot_deal or log_hubspot_note."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "The contact's email address."},
                    },
                    "required": ["email"],
                },
            ),
            handler=find_hubspot_contact,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_hubspot_contact"),
                description=(
                    "Create a new HubSpot contact. Returns the new contact id. "
                    "If the email already exists, returns a message saying so (does not error)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Contact's email (required)."},
                        "firstname": {"type": "string", "description": "First name."},
                        "lastname": {"type": "string", "description": "Last name."},
                        "phone": {"type": "string", "description": "Phone number."},
                        "company": {"type": "string", "description": "Company name."},
                    },
                    "required": ["email"],
                },
            ),
            handler=create_hubspot_contact,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("update_hubspot_contact"),
                description=(
                    "Update one or more properties on an existing HubSpot contact. "
                    "Pass the contact id from find_hubspot_contact or create_hubspot_contact. "
                    "Fields is a dict of HubSpot property names to new values, "
                    "e.g. {\"lifecyclestage\": \"lead\", \"hs_lead_status\": \"IN_PROGRESS\"}."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "HubSpot contact id."},
                        "fields": {
                            "type": "object",
                            "description": "Property names and new values to set.",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["contact_id", "fields"],
                },
            ),
            handler=update_hubspot_contact,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("create_hubspot_deal"),
                description=(
                    "Create a new deal in HubSpot and optionally link it to a contact. "
                    "Use list_hubspot_pipeline_stages to see valid stage ids before setting stage. "
                    "Returns the new deal id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Deal name / title."},
                        "contact_id": {"type": "string", "description": "HubSpot contact id to associate."},
                        "value": {"type": "number", "description": "Deal value (amount)."},
                        "stage": {"type": "string", "description": "Deal stage id (from list_hubspot_pipeline_stages)."},
                        "pipeline": {"type": "string", "description": "Pipeline id (defaults to the default pipeline)."},
                    },
                    "required": ["title"],
                },
            ),
            handler=create_hubspot_deal,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("move_hubspot_deal"),
                description=(
                    "Move a HubSpot deal to a different pipeline stage. "
                    "Use list_hubspot_pipeline_stages to see valid stage ids."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "deal_id": {"type": "string", "description": "HubSpot deal id."},
                        "stage": {"type": "string", "description": "Target stage id."},
                    },
                    "required": ["deal_id", "stage"],
                },
            ),
            handler=move_hubspot_deal,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("log_hubspot_note"),
                description=(
                    "Log a note on a HubSpot contact. The note is timestamped and appears "
                    "on the contact's activity timeline. Use for call summaries, conversation "
                    "logs, or any free-text record."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "HubSpot contact id."},
                        "text": {"type": "string", "description": "Note body (plain text)."},
                    },
                    "required": ["contact_id", "text"],
                },
            ),
            handler=log_hubspot_note,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("list_hubspot_pipeline_stages"),
                description=(
                    "List all HubSpot deal pipelines and their stages with their ids. "
                    "Call this before create_hubspot_deal or move_hubspot_deal so you know "
                    "which stage ids are valid."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pipeline": {
                            "type": "string",
                            "description": "Optional pipeline name filter. Omit to list all.",
                        },
                    },
                    "required": [],
                },
            ),
            handler=list_hubspot_pipeline_stages,
        ),
    ]
