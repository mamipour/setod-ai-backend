"""
Gmail connector — IMAP read + SMTP send via App Password
=========================================================
No OAuth, no Google Cloud Console review required.
Users generate a 16-char App Password from their Google Account
(myaccount.google.com → Security → 2-Step Verification → App passwords).

Config stored encrypted: {"email": "user@gmail.com", "app_password": "xxxx xxxx xxxx xxxx"}
"""

from __future__ import annotations

import asyncio
import email as _email_lib
import email.header
import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from app.core.crypto import decrypt_json
from app.core.llm.client import ToolSpec
from app.db.models import Connector
from app.integrations.base import IntegrationError, RegisteredTool, ToolContext

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Fetch at most this many bytes per message body when listing.
# Keeps listing fast; full body is available when the agent needs it.
_BODY_PREVIEW_BYTES = 30_000


# ── Config helpers ─────────────────────────────────────────────────────────────

def _cfg(connector: Connector) -> tuple[str, str]:
    """Return (email, app_password) from the encrypted connector config."""
    data = decrypt_json(connector.config)
    return data["email"], data["app_password"]


# ── IMAP primitives (sync — run in thread) ────────────────────────────────────

def _imap_connect(email_addr: str, app_password: str) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    try:
        imap.login(email_addr, app_password)
    except imaplib.IMAP4.error as exc:
        raise IntegrationError(
            f"Gmail login failed — check the App Password is correct. ({exc})"
        ) from exc
    return imap


def _decode_header_str(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            out.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(raw)
    return "".join(out)


def _extract_body(msg: _email_lib.message.Message) -> str:
    """Pull plain text from a parsed email, stripping HTML tags as fallback."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            if ct == "text/plain":
                raw = part.get_payload(decode=True)
                body = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
                break
            if ct == "text/html" and not body:
                raw = part.get_payload(decode=True)
                html = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
                body = _strip_html(html)
    else:
        raw = msg.get_payload(decode=True)
        text = raw.decode(msg.get_content_charset() or "utf-8", errors="replace") if raw else ""
        body = _strip_html(text) if msg.get_content_type() == "text/html" else text
    return body.strip()


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s{2,}", " ", text).strip()


def _parse_message(uid: str, raw_bytes: bytes) -> dict[str, Any]:
    msg = _email_lib.message_from_bytes(raw_bytes)
    return {
        "id": uid,
        "from": _decode_header_str(msg.get("From")),
        "to": _decode_header_str(msg.get("To")),
        "subject": _decode_header_str(msg.get("Subject")) or "(no subject)",
        "date": msg.get("Date", ""),
        "message_id_header": msg.get("Message-ID", ""),
        "body": _extract_body(msg),
    }


def _search_sync(
    email_addr: str, app_password: str, criteria: str, limit: int
) -> list[dict[str, Any]]:
    """IMAP search → fetch message data. Returns newest-first up to limit."""
    imap = _imap_connect(email_addr, app_password)
    try:
        imap.select("INBOX")
        status, data = imap.uid("search", None, criteria)
        if status != "OK":
            return []
        uids = data[0].split() if data[0] else []
        uids = uids[-limit:]  # newest UIDs are highest numbers

        results = []
        for uid_bytes in reversed(uids):
            uid = uid_bytes.decode()
            status, msg_data = imap.uid(
                "fetch", uid, f"(BODY.PEEK[]<0.{_BODY_PREVIEW_BYTES}>)"
            )
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            results.append(_parse_message(uid, raw))
        return results
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _get_message_sync(email_addr: str, app_password: str, uid: str) -> dict[str, Any]:
    """Fetch a single message by IMAP UID (full body)."""
    imap = _imap_connect(email_addr, app_password)
    try:
        imap.select("INBOX")
        status, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise IntegrationError(f"Message {uid} not found in INBOX.")
        return _parse_message(uid, msg_data[0][1])
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _send_sync(
    email_addr: str,
    app_password: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    msg = EmailMessage()
    msg["From"] = email_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as smtp:
        try:
            smtp.login(email_addr, app_password)
        except smtplib.SMTPAuthenticationError as exc:
            raise IntegrationError(
                f"Gmail SMTP login failed — check the App Password. ({exc})"
            ) from exc
        smtp.send_message(msg)


def _archive_sync(email_addr: str, app_password: str, uid: str) -> None:
    """Move message from INBOX to [Gmail]/All Mail (archive, not delete)."""
    imap = _imap_connect(email_addr, app_password)
    try:
        imap.select("INBOX")
        # Gmail supports the MOVE extension; fall back to copy+delete if not.
        try:
            imap.uid("move", uid, "[Gmail]/All Mail")
        except Exception:
            imap.uid("copy", uid, "[Gmail]/All Mail")
            imap.uid("store", uid, "+FLAGS", "\\Deleted")
            imap.expunge()
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _test_connection_sync(email_addr: str, app_password: str) -> None:
    """Attempt IMAP login and return; raises IntegrationError on failure."""
    imap = _imap_connect(email_addr, app_password)
    imap.logout()


# ── Async wrappers ─────────────────────────────────────────────────────────────

async def search_messages(
    connector: Connector, criteria: str, limit: int
) -> list[dict[str, Any]]:
    email_addr, pwd = _cfg(connector)
    return await asyncio.to_thread(_search_sync, email_addr, pwd, criteria, limit)


async def get_message(connector: Connector, uid: str) -> dict[str, Any]:
    email_addr, pwd = _cfg(connector)
    return await asyncio.to_thread(_get_message_sync, email_addr, pwd, uid)


async def send_message(
    connector: Connector,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    email_addr, pwd = _cfg(connector)
    await asyncio.to_thread(_send_sync, email_addr, pwd, to, subject, body, in_reply_to, references)


async def archive_message(connector: Connector, uid: str) -> None:
    email_addr, pwd = _cfg(connector)
    await asyncio.to_thread(_archive_sync, email_addr, pwd, uid)


async def test_connection(connector: Connector) -> str:
    """Returns a success detail string, raises IntegrationError on failure."""
    email_addr, pwd = _cfg(connector)
    await asyncio.to_thread(_test_connection_sync, email_addr, pwd)
    return email_addr


# ── Tools ──────────────────────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> list[RegisteredTool]:
    connector = ctx.connector

    async def read_unread(args: dict[str, Any], dry_run: bool) -> str:
        limit = min(int(args.get("limit", 10)), 25)
        msgs = await search_messages(connector, "UNSEEN", limit)
        ids = [m["id"] for m in msgs]

        fresh_set = await ctx.unprocessed(ids)
        fresh = [m for m in msgs if m["id"] in fresh_set]
        skipped = len(msgs) - len(fresh)

        if not fresh:
            return f"No new unread email. ({skipped} already handled in an earlier run.)"

        for m in fresh:
            ctx.note_seen(m["id"])

        lines = [
            f"{i + 1}. id={m['id']} from={m['from']} subject={m['subject']!r}\n"
            f"   {m['body'][:300]}"
            for i, m in enumerate(fresh)
        ]
        suffix = f"\n({skipped} already handled, omitted.)" if skipped else ""
        return f"{len(fresh)} unread email(s):\n" + "\n".join(lines) + suffix

    async def search(args: dict[str, Any], dry_run: bool) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: query is required."
        # Convert Gmail-style search to IMAP where possible; pass through otherwise.
        imap_criteria = _gmail_query_to_imap(query)
        limit = min(int(args.get("limit", 10)), 25)
        msgs = await search_messages(connector, imap_criteria, limit)
        if not msgs:
            return f"No email matched {query!r}."
        lines = [
            f"{i + 1}. id={m['id']} from={m['from']} subject={m['subject']!r}\n"
            f"   {m['body'][:300]}"
            for i, m in enumerate(msgs)
        ]
        return f"{len(msgs)} result(s):\n" + "\n".join(lines)

    async def send(args: dict[str, Any], dry_run: bool) -> str:
        to = str(args.get("to", "")).strip()
        subject = str(args.get("subject", "")).strip()
        body = str(args.get("body", "")).strip()
        if not (to and body):
            return "Error: 'to' and 'body' are required."
        if dry_run:
            return f"Would have emailed {to} — subject {subject!r}, body: {body[:300]}"
        email_addr, _ = _cfg(connector)
        await send_message(connector, to, subject, body)
        return f"Email sent to {to}."

    async def reply(args: dict[str, Any], dry_run: bool) -> str:
        uid = str(args.get("message_id", "")).strip()
        body = str(args.get("body", "")).strip()
        if not (uid and body):
            return "Error: 'message_id' and 'body' are required."

        original = await get_message(connector, uid)
        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        if dry_run:
            return (
                f"Would have replied to {original['from']} about {original['subject']!r} "
                f"with: {body[:300]}"
            )
        await send_message(
            connector,
            original["from"],
            subject,
            body,
            in_reply_to=original["message_id_header"],
            references=original["message_id_header"],
        )
        await ctx.mark_processed(uid)
        return f"Replied to {original['from']} about {original['subject']!r}."

    async def archive(args: dict[str, Any], dry_run: bool) -> str:
        uid = str(args.get("message_id", "")).strip()
        if not uid:
            return "Error: 'message_id' is required."
        if dry_run:
            original = await get_message(connector, uid)
            return f"Would have archived {original['subject']!r} from {original['from']}."
        await archive_message(connector, uid)
        await ctx.mark_processed(uid)
        return f"Archived message {uid}."

    limit_prop = {"limit": {"type": "integer", "description": "Max messages, default 10, cap 25."}}

    tools = [
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("read_unread_emails"),
                ctx.describe(
                    "List unread inbox emails with body preview. Excludes messages this "
                    "agent already handled on a previous run."
                ),
                {"type": "object", "properties": limit_prop},
            ),
            read_unread,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("search_emails"),
                ctx.describe(
                    "Search email. Supports Gmail-style syntax: from:, subject:, after:, before:, "
                    "is:unread, is:read, etc."
                ),
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, **limit_prop},
                    "required": ["query"],
                },
            ),
            search,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("send_email"),
                ctx.describe("Send a new email."),
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient address."},
                        "subject": {"type": "string"},
                        "body": {"type": "string", "description": "Plain text body."},
                    },
                    "required": ["to", "body"],
                },
            ),
            send,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("reply_to_email"),
                ctx.describe("Reply to an email, keeping it in the same thread."),
                {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Id from read_unread_emails."},
                        "body": {"type": "string", "description": "Plain text reply."},
                    },
                    "required": ["message_id", "body"],
                },
            ),
            reply,
        ),
        RegisteredTool(
            ToolSpec(
                ctx.tool_name("archive_email"),
                ctx.describe("Archive an email (moves out of inbox, nothing is deleted)."),
                {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            ),
            archive,
        ),
    ]

    from app.integrations import calendar as google_calendar

    return tools + google_calendar.build_tools(ctx)


# ── Query translation helpers ──────────────────────────────────────────────────

def _gmail_query_to_imap(query: str) -> str:
    """Best-effort conversion of Gmail search syntax to IMAP search criteria."""
    parts = []
    remaining = query

    # from:address → FROM "address"
    for m in re.finditer(r'from:(\S+)', remaining):
        parts.append(f'FROM "{m.group(1)}"')
        remaining = remaining.replace(m.group(0), "")

    # subject:text → SUBJECT "text"
    for m in re.finditer(r'subject:(\S+)', remaining):
        parts.append(f'SUBJECT "{m.group(1)}"')
        remaining = remaining.replace(m.group(0), "")

    # is:unread → UNSEEN, is:read → SEEN
    if "is:unread" in remaining:
        parts.append("UNSEEN")
        remaining = remaining.replace("is:unread", "")
    if "is:read" in remaining:
        parts.append("SEEN")
        remaining = remaining.replace("is:read", "")

    # after:YYYY/MM/DD → SINCE DD-Mon-YYYY
    for m in re.finditer(r'after:(\d{4}/\d{1,2}/\d{1,2})', remaining):
        parts.append(f'SINCE "{_ymd_to_imap(m.group(1))}"')
        remaining = remaining.replace(m.group(0), "")

    # before:YYYY/MM/DD → BEFORE DD-Mon-YYYY
    for m in re.finditer(r'before:(\d{4}/\d{1,2}/\d{1,2})', remaining):
        parts.append(f'BEFORE "{_ymd_to_imap(m.group(1))}"')
        remaining = remaining.replace(m.group(0), "")

    # Remaining free text → TEXT search
    remaining = remaining.strip()
    if remaining:
        parts.append(f'TEXT "{remaining}"')

    return " ".join(parts) if parts else "ALL"


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _ymd_to_imap(ymd: str) -> str:
    """2026/08/01 → 01-Aug-2026"""
    y, m, d = ymd.split("/")
    return f"{int(d):02d}-{_MONTHS[int(m) - 1]}-{y}"
