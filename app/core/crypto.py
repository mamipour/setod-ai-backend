"""
Fernet symmetric encryption for storing connector credentials.
Credentials are encrypted before being written to the DB and decrypted
only inside service/router code — they are never returned to the client.
"""

import json
from cryptography.fernet import Fernet

from app.config import settings


def _fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def encrypt_json(data: dict) -> str:
    """Serialize dict → JSON → encrypt → return base64 ciphertext string."""
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(ciphertext: str) -> dict:
    """Decrypt base64 ciphertext → JSON → dict."""
    return json.loads(_fernet().decrypt(ciphertext.encode()))
