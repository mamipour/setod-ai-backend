"""
Twilio integration
==================
SMS only for now. Twilio charges per message and a runaway agent is an expensive bug, so the
send tool leans on the runner's dry-run flag and reports cost-bearing actions plainly in its
result text.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

API = "https://api.twilio.com/2010-04-01"


async def validate(account_sid: str, auth_token: str) -> str:
    """Friendly account name if the credentials work, else raise."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API}/Accounts/{account_sid}.json", auth=(account_sid, auth_token), timeout=15
        )
    if resp.status_code == 401:
        raise IntegrationError("Invalid Twilio Account SID or Auth Token.")
    if resp.status_code != 200:
        raise IntegrationError(f"Twilio returned {resp.status_code}.")
    return resp.json().get("friendly_name", account_sid)


async def send_sms(
    account_sid: str, auth_token: str, from_number: str, to: str, body: str
) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API}/Accounts/{account_sid}/Messages.json",
            auth=(account_sid, auth_token),
            data={"From": from_number, "To": to, "Body": body},
            timeout=20,
        )
    if resp.status_code >= 400:
        detail = resp.json().get("message", resp.text[:200])
        raise IntegrationError(f"Twilio error: {detail}")
    return resp.json()


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    config = decrypt_json(ctx.connector.config)
    sid, token = config["account_sid"], config["auth_token"]
    from_number = config["phone_number"]

    async def send(args: dict[str, Any], dry_run: bool) -> str:
        to = str(args.get("to", "")).strip()
        body = str(args.get("message", "")).strip()
        if not (to and body):
            return "Error: 'to' and 'message' are required."
        if not to.startswith("+"):
            return "Error: 'to' must be in E.164 format, e.g. +15551234567."
        if dry_run:
            return f"Would have texted {to}: {body[:320]}"
        result = await send_sms(sid, token, from_number, to, body)
        return f"SMS sent to {to} (sid {result.get('sid')})."

    return [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("send_sms"),
                ctx.describe(
                    f"Send an SMS from {from_number}. Costs money per message — use only when "
                    "the task calls for it, and keep messages under 320 characters."
                ),
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "E.164 number, e.g. +15551234567."},
                        "message": {"type": "string"},
                    },
                    "required": ["to", "message"],
                },
            ),
            send,
        )
    ]
