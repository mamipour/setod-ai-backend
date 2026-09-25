"""
Slack integration
=================
Outgoing webhook only — sends a message to a Slack channel via an Incoming Webhook URL.
No OAuth, no scopes: paste the URL from Slack's *Incoming Webhooks* app page and done.

The tool honours dry_run so a preview run never posts to Slack.
"""

from typing import Any

import httpx

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext


async def validate(webhook_url: str) -> str:
    """Post a silent ping. Slack returns 'ok' for valid URLs, an error string otherwise."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            webhook_url,
            json={"text": "Setod connected ✓"},
        )
    if resp.status_code != 200 or resp.text != "ok":
        raise IntegrationError(
            f"Slack rejected the webhook URL (HTTP {resp.status_code}: {resp.text[:120]})"
        )
    return "ok"


async def post(webhook_url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(webhook_url, json={"text": text})
    if resp.status_code != 200:
        raise IntegrationError(f"Slack error {resp.status_code}: {resp.text[:200]}")


def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    config = decrypt_json(ctx.connector.config)
    url = config["webhook_url"]

    async def send(args: dict[str, Any], dry_run: bool) -> str:
        text = str(args.get("message", "")).strip()
        if not text:
            return "Error: 'message' is required."
        if dry_run:
            return f"[simulated] Would post to Slack: {text[:200]}"
        try:
            await post(url, text)
            return "Message posted to Slack."
        except IntegrationError as exc:
            return f"Error: {exc}"

    name = ctx.tool_name("post_to_slack")
    return [
        RegisteredTool(
            spec=ToolSpec(
                name=name,
                description=(
                    "Post a message to a Slack channel. Use for notifications, alerts, "
                    "or summaries. Keep messages concise; Slack renders plain text and "
                    "basic markdown (*bold*, _italic_, `code`, ```block```)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The message text to post. Plain text or Slack markdown.",
                        }
                    },
                    "required": ["message"],
                },
            ),
            handler=send,
        )
    ]
