"""
Instagram integration (Meta Graph API — Instagram Login path)
=============================================================
Config fields (stored encrypted per connector):
  access_token  — long-lived IGAA… token (60-day, auto-exchanged by the OAuth callback)
  ig_user_id    — numeric Instagram user ID
  username      — @handle, shown in the UI

Auth path: Instagram Login (graph.instagram.com / api.instagram.com).
No Facebook Page required.

Tools:
  get_instagram_posts(limit)                  — list recent media with IDs
  get_instagram_comments(media_id, limit)     — comments on one post
  reply_to_instagram_comment(comment_id, msg) — reply to a comment
  hide_instagram_comment(comment_id)          — hide (or unhide) a comment
  delete_instagram_comment(comment_id)        — permanently delete
  read_instagram_messages(limit)              — unread DMs from InboundEvent rows
  reply_to_instagram_dm(sender_id, message)   — reply within 24-hour window

Inbound flow (comments + DMs):
  GET  /webhooks/instagram          → hub.challenge verification (app-level, one URL)
  POST /webhooks/instagram          → dispatches to matching connector by ig_user_id

24-hour DM window: Meta only allows free-form replies within 24 h of the user's
last message. The tool returns an explicit error when the window is closed, just like
the WhatsApp connector does.
"""

from __future__ import annotations

from typing import Any
from datetime import UTC, datetime, timedelta

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.db.models import InboundEvent
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

IG_API = "https://graph.instagram.com/v20.0"

# Meta error codes
_DM_WINDOW_CODES = {"10900", "10901", "10902", "10903", "10904", "551"}  # outside 24h window


# ── Low-level API helpers ──────────────────────────────────────────────────────

async def _get(token: str, path: str, params: dict | None = None) -> dict:
    p = {"access_token": token, **(params or {})}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{IG_API}/{path}", params=p)
    data = resp.json()
    if "error" in data:
        raise IntegrationError(data["error"].get("message", "Instagram API error"))
    return data


async def _post(token: str, path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{IG_API}/{path}",
            params={"access_token": token},
            json=payload,
        )
    data = resp.json()
    if "error" in data:
        code = str(data["error"].get("code", ""))
        msg = data["error"].get("message", "Instagram API error")
        if code in _DM_WINDOW_CODES:
            raise IntegrationError(
                "The 24-hour messaging window has closed. "
                "You can only reply after the user sends you a new message. "
                "Instagram does not allow initiating conversations via the API."
            )
        raise IntegrationError(msg)
    return data


async def _delete(token: str, path: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(
            f"{IG_API}/{path}",
            params={"access_token": token},
        )
    data = resp.json()
    if "error" in data:
        raise IntegrationError(data["error"].get("message", "Instagram API error"))
    return data


# ── OAuth helpers (called by the connectors router) ───────────────────────────

async def exchange_code(app_id: str, app_secret: str, redirect_uri: str, code: str) -> dict:
    """Exchange an auth code for a short-lived token, then immediately upgrade to long-lived."""
    async with httpx.AsyncClient(timeout=20) as client:
        # Step 1: short-lived token
        resp = await client.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
    data = resp.json()
    if "error_type" in data or "error" in data:
        msg = data.get("error_message") or data.get("error", {}).get("message", "Token exchange failed")
        raise IntegrationError(msg)

    short_token = data["access_token"]
    ig_user_id = str(data.get("user_id", ""))

    # Step 2: long-lived token (60-day)
    async with httpx.AsyncClient(timeout=20) as client:
        resp2 = await client.get(
            f"{IG_API}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "access_token": short_token,
            },
        )
    long_data = resp2.json()
    if "error" in long_data:
        raise IntegrationError(long_data["error"].get("message", "Long-lived token exchange failed"))

    long_token = long_data["access_token"]
    expires_in = long_data.get("expires_in", 5183944)  # ~60 days in seconds
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()

    # Step 3: get account info
    async with httpx.AsyncClient(timeout=10) as client:
        resp3 = await client.get(
            f"{IG_API}/me",
            params={"fields": "id,username", "access_token": long_token},
        )
    me = resp3.json()
    username = me.get("username", "")
    ig_user_id = ig_user_id or str(me.get("id", ""))

    return {
        "access_token": long_token,
        "ig_user_id": ig_user_id,
        "username": username,
        "expires_at": expires_at,
    }


async def subscribe_account(ig_user_id: str, access_token: str) -> None:
    """Subscribe this account to comment + message webhook events."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{IG_API}/{ig_user_id}/subscribed_apps",
                params={
                    "subscribed_fields": "comments,messages",
                    "access_token": access_token,
                },
            )
    except Exception:  # noqa: BLE001
        pass  # non-fatal — events still arrive if the app subscription is set up in the dashboard


# ── Tool builder ───────────────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    config = decrypt_json(ctx.connector.config)
    token = config["access_token"]
    ig_user_id = config["ig_user_id"]

    def n(base: str) -> str:
        return ctx.tool_name(base)

    # ── get_instagram_posts ───────────────────────────────────────────────────

    async def get_posts(args: dict[str, Any], dry_run: bool) -> str:
        limit = min(int(args.get("limit", 10)), 30)
        if dry_run:
            return "[simulated] Would list recent Instagram posts."
        try:
            data = await _get(
                token,
                f"{ig_user_id}/media",
                {"fields": "id,caption,media_type,timestamp,permalink", "limit": str(limit)},
            )
            items = data.get("data", [])
            if not items:
                return "No posts found."
            lines = []
            for p in items:
                cap = (p.get("caption") or "")[:80].replace("\n", " ")
                lines.append(f"[{p['timestamp'][:10]}] id={p['id']} type={p['media_type']} {cap}")
            return "\n".join(lines)
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── get_instagram_comments ────────────────────────────────────────────────

    async def get_comments(args: dict[str, Any], dry_run: bool) -> str:
        media_id = str(args.get("media_id", "")).strip()
        limit = min(int(args.get("limit", 20)), 50)
        if not media_id:
            return "Error: media_id is required."
        if dry_run:
            return f"[simulated] Would fetch up to {limit} comments on media {media_id}."
        try:
            data = await _get(
                token,
                f"{media_id}/comments",
                {"fields": "id,text,username,timestamp,like_count,hidden", "limit": str(limit)},
            )
            items = data.get("data", [])
            if not items:
                return "No comments found on that post."
            lines = []
            for c in items:
                hidden = " [hidden]" if c.get("hidden") else ""
                lines.append(
                    f"[{c['timestamp'][:16]}] @{c.get('username','?')} (id={c['id']}){hidden}: {c.get('text','')}"
                )
            return "\n".join(lines)
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── reply_to_instagram_comment ────────────────────────────────────────────

    async def reply_comment(args: dict[str, Any], dry_run: bool) -> str:
        comment_id = str(args.get("comment_id", "")).strip()
        message = str(args.get("message", "")).strip()
        if not (comment_id and message):
            return "Error: comment_id and message are required."
        if dry_run:
            return f"[simulated] Would reply to comment {comment_id}: {message[:100]}"
        try:
            await _post(token, f"{comment_id}/replies", {"message": message})
            return f"Reply posted on comment {comment_id}."
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── hide_instagram_comment ────────────────────────────────────────────────

    async def hide_comment(args: dict[str, Any], dry_run: bool) -> str:
        comment_id = str(args.get("comment_id", "")).strip()
        hide = bool(args.get("hide", True))
        if not comment_id:
            return "Error: comment_id is required."
        action = "hide" if hide else "unhide"
        if dry_run:
            return f"[simulated] Would {action} comment {comment_id}."
        try:
            await _post(token, comment_id, {"hide": hide})
            return f"Comment {comment_id} {action}d."
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── delete_instagram_comment ──────────────────────────────────────────────

    async def delete_comment(args: dict[str, Any], dry_run: bool) -> str:
        comment_id = str(args.get("comment_id", "")).strip()
        if not comment_id:
            return "Error: comment_id is required."
        if dry_run:
            return f"[simulated] Would permanently delete comment {comment_id}."
        try:
            await _delete(token, comment_id)
            await ctx.mark_processed(comment_id)
            return f"Comment {comment_id} deleted."
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── read_instagram_messages ───────────────────────────────────────────────

    async def read_messages(args: dict[str, Any], dry_run: bool) -> str:
        limit = min(int(args.get("limit", 10)), 50)
        db: AsyncSession = ctx.db
        rows = await db.exec(
            select(InboundEvent)
            .where(InboundEvent.connector_id == ctx.connector.id)
            .order_by(InboundEvent.created_at.desc())
            .limit(limit)
        )
        events = rows.all()
        if not events:
            return "No new Instagram messages."
        lines = []
        for e in reversed(events):
            ctx.note_seen(e.external_id)
            lines.append(
                f"[{e.created_at.strftime('%Y-%m-%d %H:%M')}] sender_id={e.sender}: {e.text}"
            )
        return "\n".join(lines)

    # ── reply_to_instagram_dm ─────────────────────────────────────────────────

    async def reply_dm(args: dict[str, Any], dry_run: bool) -> str:
        sender_id = str(args.get("sender_id", "")).strip()
        message = str(args.get("message", "")).strip()
        if not (sender_id and message):
            return "Error: sender_id and message are required."
        if dry_run:
            return f"[simulated] Would send DM to {sender_id}: {message[:100]}"
        try:
            await _post(
                token,
                f"{ig_user_id}/messages",
                {"recipient": {"id": sender_id}, "message": {"text": message}},
            )
            return f"Message sent to {sender_id}."
        except IntegrationError as exc:
            return f"Error: {exc}"

    # ── Assemble ──────────────────────────────────────────────────────────────

    return [
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_instagram_posts"),
                description=ctx.describe(
                    "List recent Instagram posts with their IDs. Use this first to get the "
                    "media IDs you need before fetching or moderating comments."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max posts to return (default 10, max 30)."},
                    },
                    "required": [],
                },
            ),
            handler=get_posts,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("get_instagram_comments"),
                description=ctx.describe(
                    "Get comments on a specific Instagram post. Returns comment IDs, "
                    "text, author username, timestamp, and whether the comment is hidden."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "media_id": {"type": "string", "description": "Instagram media ID (from get_instagram_posts)."},
                        "limit": {"type": "integer", "description": "Max comments to return (default 20, max 50)."},
                    },
                    "required": ["media_id"],
                },
            ),
            handler=get_comments,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("reply_to_instagram_comment"),
                description=ctx.describe(
                    "Reply to an Instagram comment. The reply appears publicly as a "
                    "threaded response under the original comment."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "comment_id": {"type": "string", "description": "ID of the comment to reply to."},
                        "message": {"type": "string", "description": "Text of your reply."},
                    },
                    "required": ["comment_id", "message"],
                },
            ),
            handler=reply_comment,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("hide_instagram_comment"),
                description=ctx.describe(
                    "Hide or unhide a comment on your Instagram post. Hidden comments "
                    "are not visible to other users but are not deleted."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "comment_id": {"type": "string", "description": "ID of the comment to hide or unhide."},
                        "hide": {"type": "boolean", "description": "true to hide, false to unhide. Default true."},
                    },
                    "required": ["comment_id"],
                },
            ),
            handler=hide_comment,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("delete_instagram_comment"),
                description=ctx.describe(
                    "Permanently delete a comment from your Instagram post. "
                    "This cannot be undone."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "comment_id": {"type": "string", "description": "ID of the comment to delete."},
                    },
                    "required": ["comment_id"],
                },
            ),
            handler=delete_comment,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("read_instagram_messages"),
                description=ctx.describe(
                    "Read recent inbound Instagram DMs. Returns messages from users who "
                    "have sent you a direct message. Use sender_id from the result to reply. "
                    "IMPORTANT: Meta only allows replies within 24 hours of the user's last message."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max messages to return (default 10, max 50)."},
                    },
                    "required": [],
                },
            ),
            handler=read_messages,
        ),
        RegisteredTool(
            spec=ToolSpec(
                name=n("reply_to_instagram_dm"),
                description=ctx.describe(
                    "Reply to an Instagram direct message. You must use the sender_id from "
                    "read_instagram_messages. IMPORTANT: Meta's 24-hour rule — you can only "
                    "reply within 24 hours of the user's last message. After that the tool "
                    "returns an error; ask the user to message again to reopen the window."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sender_id": {"type": "string", "description": "Instagram-scoped user ID from read_instagram_messages."},
                        "message": {"type": "string", "description": "Text of the reply."},
                    },
                    "required": ["sender_id", "message"],
                },
            ),
            handler=reply_dm,
        ),
    ]
