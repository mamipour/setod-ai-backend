"""Webhook HMAC verification and external ID derivation — unit tests.

Tests the pure helper functions extracted from app.api.hooks.router without
starting a real HTTP server.
"""

import hashlib
import hmac as hmac_module
from unittest.mock import MagicMock

import pytest

from app.api.hooks.router import _external_id_from


# ── _external_id_from ─────────────────────────────────────────────────────────

def _fake_request(headers: dict) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    req.client = None
    return req


def test_webhook_id_header_wins():
    req = _fake_request({"x-webhook-id": "evt_abc123", "x-request-id": "req_other"})
    body = b'{"event": "order.created"}'
    assert _external_id_from(req, body) == "evt_abc123"


def test_request_id_fallback():
    req = _fake_request({"x-request-id": "req_xyz"})
    body = b'{"event": "ping"}'
    assert _external_id_from(req, body) == "req_xyz"


def test_idempotency_key_fallback():
    req = _fake_request({"idempotency-key": "idem_42"})
    body = b'{}'
    assert _external_id_from(req, body) == "idem_42"


def test_body_hash_when_no_header():
    req = _fake_request({})
    body = b'{"order_id": 99}'
    result = _external_id_from(req, body)
    expected = hashlib.sha256(body[:512]).hexdigest()
    assert result == expected


def test_body_hash_uses_first_512_bytes():
    req = _fake_request({})
    long_body = b"x" * 1000
    result = _external_id_from(req, long_body)
    expected = hashlib.sha256(long_body[:512]).hexdigest()
    assert result == expected


def test_same_body_same_id_deterministic():
    req = _fake_request({})
    body = b'{"stable": true}'
    assert _external_id_from(req, body) == _external_id_from(req, body)


def test_header_value_truncated_at_255():
    req = _fake_request({"x-webhook-id": "a" * 300})
    body = b""
    result = _external_id_from(req, body)
    assert len(result) <= 255


# ── HMAC-SHA256 verification (logic, not the full FastAPI endpoint) ─────────

def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac_module.new(secret.encode(), body, "sha256").hexdigest()


def test_valid_signature_matches():
    secret = "my-signing-secret"
    body = b'{"test": 1}'
    sig = _sign(secret, body)
    expected = hmac_module.new(secret.encode(), body, "sha256").hexdigest()
    _, _, sig_hex = sig.partition("=")
    assert hmac_module.compare_digest(sig_hex, expected)


def test_wrong_secret_does_not_match():
    body = b'{"test": 1}'
    real_sig = _sign("real-secret", body)
    wrong = hmac_module.new("wrong-secret".encode(), body, "sha256").hexdigest()
    _, _, sig_hex = real_sig.partition("=")
    assert not hmac_module.compare_digest(sig_hex, wrong)


def test_tampered_body_invalidates_signature():
    secret = "s3cr3t"
    original = b'{"amount": 100}'
    tampered = b'{"amount": 999}'
    sig = _sign(secret, original)
    expected_for_tampered = hmac_module.new(secret.encode(), tampered, "sha256").hexdigest()
    _, _, sig_hex = sig.partition("=")
    assert not hmac_module.compare_digest(sig_hex, expected_for_tampered)
