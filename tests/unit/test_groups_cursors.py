"""Sprint 11: per-agent read cursors and Telegram group handling.

Pure-function and mocked-DB tests; no provider or database access.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.conversations import (
    Peer,
    display_name,
    format_turn,
    resolve_peer,
)
from app.db.models import (
    Conversation,
    ConversationMessage,
    MessageAuthor,
    MessageDirection,
    ConnectorType,
)
from app.integrations.base import ToolContext
from app.integrations.gmail import _uids_after
from app.integrations.telegram import (
    PER_CHAT_LIMIT,
    ChatBatch,
    TgMessage,
    format_batches,
)


# ── display_name ───────────────────────────────────────────────────────────────

class TestDisplayName:
    def test_full(self):
        assert display_name("Reza", "K", "reza", 1) == "Reza K (@reza)"

    def test_name_only(self):
        assert display_name("Reza", None, None, 1) == "Reza"

    def test_username_only(self):
        assert display_name(None, None, "reza", 1) == "@reza"

    def test_id_fallback(self):
        assert display_name(None, None, None, 123) == "ID:123"


# ── resolve_peer — Telegram bot groups ─────────────────────────────────────────

class TestResolvePeerGroups:
    def test_private_chat_is_not_group(self):
        peer = resolve_peer(ConnectorType.telegram_bot, {
            "message": {"chat": {"id": 5, "type": "private", "first_name": "Ali"}, "text": "hi"},
        })
        assert peer.is_group is False
        assert peer.peer_id == "5"
        assert peer.speaker_id == ""

    def test_supergroup_sets_group_title_and_speaker(self):
        peer = resolve_peer(ConnectorType.telegram_bot, {
            "message": {
                "chat": {"id": -100123, "type": "supergroup", "title": "Tehran Biz"},
                "from": {"id": 42, "first_name": "Reza", "username": "reza"},
                "text": "anyone know an accountant?",
            },
        })
        assert peer.is_group is True
        assert peer.peer_id == "-100123"
        assert peer.peer_name == "Tehran Biz"
        assert peer.speaker_id == "42"
        assert peer.speaker_name == "Reza (@reza)"
        assert peer.reply_to_external_id == ""

    def test_group_reply_captures_parent(self):
        peer = resolve_peer(ConnectorType.telegram_bot, {
            "message": {
                "chat": {"id": -100123, "type": "group", "title": "Tehran Biz"},
                "from": {"id": 7, "first_name": "Ali"},
                "reply_to_message": {"message_id": 1500, "text": "x" * 400},
                "text": "I can give you a number",
            },
        })
        assert peer.reply_to_external_id == "1500"
        assert len(peer.reply_to_text) == 200  # excerpt capped

    def test_client_payload_round_trips_group_fields(self):
        peer = resolve_peer(ConnectorType.telegram_client, {
            "peer_id": "-100123", "peer_name": "Tehran Biz", "is_group": True,
            "speaker_id": "42", "speaker_name": "Reza", "reply_to_external_id": "9",
            "reply_to_text": "parent",
        })
        assert peer == Peer(
            peer_id="-100123", peer_name="Tehran Biz", is_group=True,
            speaker_id="42", speaker_name="Reza",
            reply_to_external_id="9", reply_to_text="parent",
        )


# ── format_turn ────────────────────────────────────────────────────────────────

def _conv(is_group: bool) -> Conversation:
    return Conversation(
        org_id=uuid.uuid4(), connector_id=uuid.uuid4(), channel="telegram_client",
        peer_id="-1", peer_name="Tehran Biz" if is_group else "Reza", is_group=is_group,
    )


def _msg(text: str, *, speaker="", speaker_id="", reply_id="", reply_text="",
         outbound=False, external_id="") -> ConversationMessage:
    return ConversationMessage(
        conversation_id=uuid.uuid4(), org_id=uuid.uuid4(),
        direction=MessageDirection.outbound if outbound else MessageDirection.inbound,
        author=MessageAuthor.agent if outbound else MessageAuthor.peer,
        text=text, speaker_name=speaker, speaker_id=speaker_id,
        reply_to_external_id=reply_id, reply_to_text=reply_text,
        external_id=external_id,
        created_at=datetime(2026, 9, 26, 14, 2, tzinfo=UTC),
    )


class TestFormatTurn:
    def test_one_to_one_uses_peer_label(self):
        line = format_turn(_msg("hello"), _conv(False))
        assert line == "Reza [2026-09-26 14:02]: hello"

    def test_one_to_one_outbound_is_you(self):
        assert format_turn(_msg("hi", outbound=True), _conv(False)).startswith("You [")

    def test_group_names_speaker(self):
        line = format_turn(_msg("anyone?", speaker="Reza (@reza)"), _conv(True))
        assert line == "[09-26 14:02] Reza (@reza): anyone?"

    def test_group_reply_uses_stored_excerpt_when_parent_missing(self):
        line = format_turn(
            _msg("I can", speaker="Ali", reply_id="1500", reply_text="anyone know an accountant?"),
            _conv(True),
        )
        assert line == '[09-26 14:02] Ali: ↳ re earlier message: "anyone know an accountant?" — I can'

    def test_group_reply_prefers_parent_row(self):
        parent = _msg("anyone know an accountant?", speaker="Reza", external_id="1500")
        line = format_turn(
            _msg("I can", speaker="Ali", reply_id="1500", reply_text="stale excerpt"),
            _conv(True),
            parents={"1500": parent},
        )
        assert '↳ re Reza: "anyone know an accountant?"' in line

    def test_group_without_time(self):
        assert format_turn(_msg("x", speaker="Ali"), _conv(True), with_time=False) == "Ali: x"


# ── Telegram format_batches ────────────────────────────────────────────────────

def _tg(msg_id: int, text: str, *, name="Reza (@reza)", sid="42", reply=None) -> TgMessage:
    m = TgMessage(
        msg_id=msg_id, date=datetime(2026, 9, 26, 14, msg_id % 60, tzinfo=UTC),
        text=text, speaker_id=sid, speaker_name=name,
    )
    if reply:
        m.reply_to_id, m.reply_to_speaker, m.reply_to_text = reply
    return m


class TestFormatBatches:
    def test_empty(self):
        assert format_batches([]) == ""
        assert format_batches([ChatBatch("1", "x", True, 9)]) == ""

    def test_group_header_and_lines(self):
        out = format_batches([ChatBatch(
            "-100", "Tehran Biz", True, 12,
            messages=[_tg(10, "anyone know an accountant?"),
                      _tg(12, "I can", name="Ali", sid="7",
                          reply=(10, "Reza (@reza)", "anyone know an accountant?"))],
        )])
        assert out.startswith('Group "Tehran Biz" (chat_id=-100) — 2 new messages')
        assert "[09-26 14:10] Reza (@reza) #42 [msg 10]: anyone know an accountant?" in out
        assert '[09-26 14:12] Ali #7 [msg 12]: ↳ re Reza (@reza): "anyone know an accountant?" — I can' in out

    def test_older_skipped_note(self):
        out = format_batches([ChatBatch("-1", "G", True, 99, [_tg(1, "x")], older_skipped=True)])
        assert f"older messages beyond the newest {PER_CHAT_LIMIT} skipped" in out

    def test_id_only_speaker_not_doubled(self):
        out = format_batches([ChatBatch("-1", "G", True, 1, [_tg(1, "x", name="ID:42", sid="42")])])
        assert "#42 [msg 1]" in out
        assert "ID:42" not in out

    def test_total_limit_cuts_oldest_and_reports(self):
        msgs = [_tg(i, f"m{i}") for i in range(1, 8)]
        out = format_batches([ChatBatch("-1", "G", True, 7, msgs)], total_limit=3)
        assert "4 oldest omitted for length" in out
        assert "m7" in out and "m1" not in out

    def test_second_chat_omitted_when_limit_reached(self):
        a = ChatBatch("-1", "A", True, 2, [_tg(1, "a"), _tg(2, "b")])
        b = ChatBatch("-2", "B", False, 1, [_tg(1, "c")])
        out = format_batches([a, b], total_limit=2)
        assert "further chats omitted" in out
        assert 'Chat "B"' not in out

    def test_private_chat_label(self):
        out = format_batches([ChatBatch("5", "Ali", False, 1, [_tg(1, "hi", name="Ali", sid="5")])])
        assert out.startswith('Chat "Ali" (chat_id=5) — 1 new message\n')


# ── Gmail UID cursor helper ────────────────────────────────────────────────────

class TestUidsAfter:
    def test_no_cursor_passthrough(self):
        assert _uids_after(["3", "2", "1"], None) == ["3", "2", "1"]

    def test_drops_at_or_below_cursor(self):
        # `UID 6:*` returns UID 5 when 5 is the highest; it must be filtered out.
        assert _uids_after(["5", "4"], "5") == []
        assert _uids_after(["7", "6", "5"], "5") == ["7", "6"]


# ── ToolContext cursors ────────────────────────────────────────────────────────

def _ctx() -> ToolContext:
    connector = MagicMock()
    connector.id = uuid.uuid4()
    db = MagicMock()
    db.exec = AsyncMock()
    return ToolContext(db=db, agent_id=uuid.uuid4(), connector=connector)


class TestToolContextCursors:
    @pytest.mark.asyncio
    async def test_get_reads_db_when_unset(self):
        ctx = _ctx()
        result = MagicMock()
        result.first.return_value = "1500"
        ctx.db.exec.return_value = result
        assert await ctx.get_cursor("-100") == "1500"
        ctx.db.exec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_returns_none_when_no_row(self):
        ctx = _ctx()
        result = MagicMock()
        result.first.return_value = None
        ctx.db.exec.return_value = result
        assert await ctx.get_cursor("INBOX") is None

    @pytest.mark.asyncio
    async def test_set_shadows_db_within_run(self):
        ctx = _ctx()
        ctx.set_cursor("-100", "1600")
        assert await ctx.get_cursor("-100") == "1600"
        ctx.db.exec.assert_not_awaited()
        assert ctx.cursors == {"-100": "1600"}
