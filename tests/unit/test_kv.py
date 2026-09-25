"""Key-value memory — pure-logic unit tests (no database).

Scope resolution and the size caps are the contract between the model, the REST API and
the table; both callers go through the same functions, so they are tested once here.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core import kv
from app.core.kv import KVError, Scope, encode, preview, render_keys, resolve
from app.db.models import AgentKV

ORG = uuid4()
AGENT = uuid4()


# ── resolve ────────────────────────────────────────────────────────────────────

def test_plain_key_is_private_to_the_agent():
    s = resolve(ORG, AGENT, "last_id")
    assert s == Scope(org_id=ORG, agent_id=AGENT, key="last_id")
    assert s.display_key == "last_id"


def test_shared_prefix_moves_key_to_workspace_scope_and_is_stripped():
    s = resolve(ORG, AGENT, "shared:tenders_seen")
    assert s.agent_id is None
    assert s.key == "tenders_seen"
    assert s.display_key == "shared:tenders_seen"


def test_key_is_trimmed():
    assert resolve(ORG, AGENT, "  x  ").key == "x"
    assert resolve(ORG, AGENT, "shared:  y ").key == "y"


@pytest.mark.parametrize("bad", ["", "   ", None, "shared:", "shared:   "])
def test_empty_keys_are_refused(bad):
    with pytest.raises(KVError):
        resolve(ORG, AGENT, bad)


def test_key_length_cap():
    resolve(ORG, AGENT, "k" * kv.MAX_KEY_LEN)  # exactly at the cap is fine
    with pytest.raises(KVError, match="too long"):
        resolve(ORG, AGENT, "k" * (kv.MAX_KEY_LEN + 1))
    # The prefix does not count against the cap — it is not stored.
    resolve(ORG, AGENT, "shared:" + "k" * kv.MAX_KEY_LEN)


def test_control_characters_are_refused():
    with pytest.raises(KVError, match="control"):
        resolve(ORG, AGENT, "a\nb")
    with pytest.raises(KVError, match="control"):
        resolve(ORG, AGENT, "a\x00b")


def test_non_string_keys_are_coerced():
    assert resolve(ORG, AGENT, 42).key == "42"


# ── encode / preview ───────────────────────────────────────────────────────────

def test_encode_accepts_json_types():
    assert encode(4412) == "4412"
    assert encode({"a": [1, 2]}) == '{"a":[1,2]}'
    assert encode(None) == "null"


def test_encode_refuses_oversize_with_actual_size():
    big = "x" * (kv.MAX_VALUE_BYTES + 1)
    with pytest.raises(KVError, match="too large"):
        encode(big)
    # Multi-byte characters count as bytes, not chars.
    with pytest.raises(KVError):
        encode("é" * (kv.MAX_VALUE_BYTES // 2 + 1))


def test_encode_at_the_cap_passes():
    # json.dumps adds two quote characters.
    encode("x" * (kv.MAX_VALUE_BYTES - 2))


def test_preview_truncates_and_flattens_newlines():
    assert preview(123) == "123"
    long = "a" * 200
    p = preview(long)
    assert len(p) == kv.PREVIEW_CHARS and p.endswith("…")
    assert "\n" not in preview("line1\nline2")


# ── render_keys ────────────────────────────────────────────────────────────────

def _row(key: str, value, shared: bool = False) -> AgentKV:
    return AgentKV(
        org_id=ORG,
        agent_id=None if shared else AGENT,
        key=key,
        value=value,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_render_keys_empty():
    assert render_keys([]) == "Nothing is stored yet."


def test_render_keys_shows_prefix_for_shared_and_preview_for_values():
    text = render_keys([_row("last_id", 4412), _row("seen", ["A", "B"], shared=True)])
    assert "- last_id = 4412" in text
    assert '- shared:seen = ["A","B"]' in text


def test_render_keys_caps_the_inventory():
    rows = [_row(f"k{i:03}", i) for i in range(kv.DESCRIPTION_MAX_KEYS + 7)]
    text = render_keys(rows)
    assert text.count("\n- ") == kv.DESCRIPTION_MAX_KEYS + 1  # +1 for the "…and N more" line
    assert "…and 7 more" in text
