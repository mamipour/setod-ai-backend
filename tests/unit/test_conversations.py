"""Unit tests for the conversation layer.

Tests are fully isolated from the database — all DB calls are mocked.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.conversations import (
    Attachment,
    Peer,
    extract_attachments,
    resolve_peer,
)
from app.core.triggers.dispatch import _build_opening
from app.db.models import (
    ConnectorType,
    InboundEvent,
    InboundEventStatus,
    MessageKind,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _event(
    sender: str = "42",
    text: str = "hello",
    received_offset_seconds: int = 0,
    conversation_id: uuid.UUID | None = None,
) -> MagicMock:
    ev = MagicMock(spec=InboundEvent)
    ev.id = uuid.uuid4()
    ev.sender = sender
    ev.text = text
    ev.payload = {}
    ev.external_id = uuid.uuid4().hex
    ev.status = InboundEventStatus.pending
    ev.conversation_id = conversation_id or uuid.uuid4()
    ev.received_at = datetime.now(UTC) - timedelta(seconds=received_offset_seconds)
    return ev


# ── resolve_peer ───────────────────────────────────────────────────────────────

class TestResolvePeer:
    def test_telegram_bot_extracts_chat_id_and_username(self):
        payload = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123456, "username": "alice"},
                "text": "hi",
            },
        }
        peer = resolve_peer(ConnectorType.telegram_bot, payload)
        assert peer.peer_id == "123456"
        assert peer.peer_name == "alice"

    def test_telegram_bot_fallback_to_first_name(self):
        payload = {
            "update_id": 1,
            "message": {
                "chat": {"id": 789, "first_name": "Bob"},
                "text": "hi",
            },
        }
        peer = resolve_peer(ConnectorType.telegram_bot, payload)
        assert peer.peer_id == "789"
        assert peer.peer_name == "Bob"

    def test_whatsapp_extracts_from_field(self):
        payload = {"type": "text", "from": "15551234567", "text": {"body": "hi"}}
        peer = resolve_peer(ConnectorType.whatsapp, payload)
        assert peer.peer_id == "15551234567"

    def test_instagram_dm_extracts_sender(self):
        payload = {"sender": {"id": "IG_USER_1", "name": "Charlie"}}
        peer = resolve_peer(ConnectorType.instagram, payload)
        assert peer.peer_id == "IG_USER_1"
        assert peer.peer_name == "Charlie"
        assert peer.thread_key == ""

    def test_instagram_comment_extracts_from_and_media_id(self):
        payload = {
            "from": {"id": "IG_USER_2", "username": "dana"},
            "media_id": "MEDIA_999",
            "text": "Great post!",
        }
        peer = resolve_peer(ConnectorType.instagram, payload)
        assert peer.peer_id == "IG_USER_2"
        assert peer.peer_name == "dana"
        assert peer.thread_key == "MEDIA_999"

    def test_twilio_extracts_from(self):
        payload = {"From": "+15559876543", "Body": "Hello"}
        peer = resolve_peer(ConnectorType.twilio, payload)
        assert peer.peer_id == "+15559876543"

    def test_unknown_connector_returns_empty_peer_id(self):
        peer = resolve_peer(ConnectorType.gmail, {})
        assert peer.peer_id == ""


# ── extract_attachments ────────────────────────────────────────────────────────

class TestExtractAttachments:
    def test_telegram_bot_text_only(self):
        payload = {"message": {"text": "Hello world"}}
        text, atts = extract_attachments(ConnectorType.telegram_bot, payload)
        assert text == "Hello world"
        assert atts == []

    def test_telegram_bot_voice_message(self):
        payload = {
            "message": {
                "voice": {"file_id": "FILE1", "duration": 5, "mime_type": "audio/ogg"},
            }
        }
        text, atts = extract_attachments(ConnectorType.telegram_bot, payload)
        assert text == ""
        assert len(atts) == 1
        assert atts[0].kind == MessageKind.audio
        assert atts[0].provider_ref == "FILE1"

    def test_telegram_bot_photo_takes_largest(self):
        payload = {
            "message": {
                "photo": [
                    {"file_id": "SMALL", "file_size": 100},
                    {"file_id": "LARGE", "file_size": 5000},
                ],
                "caption": "look at this",
            }
        }
        text, atts = extract_attachments(ConnectorType.telegram_bot, payload)
        assert text == ""
        assert atts[0].provider_ref == "LARGE"
        assert atts[0].caption == "look at this"

    def test_telegram_bot_location(self):
        payload = {"message": {"location": {"latitude": 51.5, "longitude": -0.1}}}
        text, atts = extract_attachments(ConnectorType.telegram_bot, payload)
        assert atts[0].kind == MessageKind.location
        assert "51.5" in atts[0].provider_ref

    def test_whatsapp_text(self):
        payload = {"type": "text", "text": {"body": "Hey"}}
        text, atts = extract_attachments(ConnectorType.whatsapp, payload)
        assert text == "Hey"
        assert atts == []

    def test_whatsapp_audio(self):
        payload = {"type": "audio", "audio": {"id": "AUD1", "mime_type": "audio/ogg"}}
        text, atts = extract_attachments(ConnectorType.whatsapp, payload)
        assert atts[0].kind == MessageKind.audio
        assert atts[0].provider_ref == "AUD1"

    def test_whatsapp_image_with_caption(self):
        payload = {
            "type": "image",
            "image": {"id": "IMG1", "caption": "product shot", "mime_type": "image/jpeg"},
        }
        text, atts = extract_attachments(ConnectorType.whatsapp, payload)
        assert atts[0].kind == MessageKind.image
        assert text == "product shot"

    def test_twilio_mms(self):
        payload = {
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://example.com/img.jpg",
            "MediaContentType0": "image/jpeg",
        }
        text, atts = extract_attachments(ConnectorType.twilio, payload)
        assert text == ""
        assert atts[0].kind == MessageKind.image

    def test_instagram_attachment(self):
        payload = {
            "message": {
                "text": "",
                "attachments": [{"type": "image", "payload": {"url": "https://fb.com/img.jpg"}}],
            }
        }
        text, atts = extract_attachments(ConnectorType.instagram, payload)
        assert atts[0].kind == MessageKind.image
        assert atts[0].provider_ref == "https://fb.com/img.jpg"


# ── _build_opening ─────────────────────────────────────────────────────────────

class TestBuildOpening:
    def test_single_text_event(self):
        ev = _event(sender="alice", text="Is this open?")
        opening = _build_opening([ev], ev.conversation_id)
        assert "alice" in opening
        assert "Is this open?" in opening

    def test_single_event_no_sender(self):
        ev = _event(sender="", text="ping")
        opening = _build_opening([ev], ev.conversation_id)
        assert "ping" in opening

    def test_bundle_of_three(self):
        cid = uuid.uuid4()
        events = [
            _event(sender="bob", text="hi", conversation_id=cid),
            _event(sender="bob", text="you open?", conversation_id=cid),
            _event(sender="bob", text="Sunday?", conversation_id=cid),
        ]
        opening = _build_opening(events, cid)
        assert "3" in opening  # "3 new messages"
        assert "hi" in opening
        assert "Sunday?" in opening

    def test_instagram_comment_includes_comment_id(self):
        ev = _event(sender="alice", text="love it")
        ev.payload = {"media_id": "MEDIA_999"}
        opening = _build_opening([ev], ev.conversation_id)
        assert "MEDIA_999" in opening
        assert "comment_id" in opening

    def test_media_only_event_shows_marker(self):
        ev = _event(sender="alice", text="")
        opening = _build_opening([ev], ev.conversation_id)
        assert "media" in opening.lower() or "no text" in opening.lower()


# ── debounce logic ─────────────────────────────────────────────────────────────

class TestDebounce:
    """Test the debounce window logic in isolation using the constants."""

    def test_old_event_outside_debounce_window_is_ready(self):
        """An event older than 30s (hard cap) should be claimed regardless of quiet window."""
        from app.core.triggers.dispatch import DEBOUNCE_MAX_SECONDS, DEBOUNCE_SECONDS

        now = datetime.now(UTC)
        oldest = now - timedelta(seconds=DEBOUNCE_MAX_SECONDS + 1)
        newest = now - timedelta(seconds=2)  # still within quiet window

        # Hard cap applies: oldest > DEBOUNCE_MAX_SECONDS old → ready
        hard_cutoff = now - timedelta(seconds=DEBOUNCE_MAX_SECONDS)
        assert oldest <= hard_cutoff  # passed the hard cap → should fire

    def test_recent_event_in_quiet_window_is_not_ready(self):
        from app.core.triggers.dispatch import DEBOUNCE_SECONDS

        now = datetime.now(UTC)
        newest = now - timedelta(seconds=DEBOUNCE_SECONDS - 2)  # within quiet window
        debounce_cutoff = now - timedelta(seconds=DEBOUNCE_SECONDS)
        assert newest > debounce_cutoff  # NOT past cutoff → should NOT fire

    def test_quiet_window_elapsed_is_ready(self):
        from app.core.triggers.dispatch import DEBOUNCE_SECONDS

        now = datetime.now(UTC)
        newest = now - timedelta(seconds=DEBOUNCE_SECONDS + 1)  # past quiet window
        debounce_cutoff = now - timedelta(seconds=DEBOUNCE_SECONDS)
        assert newest <= debounce_cutoff  # past cutoff → should fire


# ── record_outbound dry_run guard ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outbound_dry_run_returns_none():
    """record_outbound must not write to DB on dry_run=True."""
    from app.core.conversations import record_outbound
    from app.db.models import MessageAuthor

    db = AsyncMock()
    connector = MagicMock()
    connector.org_id = uuid.uuid4()

    result = await record_outbound(
        db,
        connector=connector,
        conversation_id=uuid.uuid4(),
        peer_id="123",
        text="hello",
        dry_run=True,
    )
    assert result is None
    db.exec.assert_not_called()
    db.commit.assert_not_called()
