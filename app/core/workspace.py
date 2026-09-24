"""Workspace-level integration settings stored encrypted on the Organization row.

Lives in core rather than the API layer because agent runs need to read these
settings too — core importing from app.api would invert the layering.
"""

from app.core.crypto import decrypt_json, encrypt_json
from app.db.models import Organization


def load_web_settings(org: Organization) -> dict:
    """Decrypt the org's web settings. Empty dict when unset or undecryptable."""
    if not org.web_settings:
        return {}
    try:
        return decrypt_json(org.web_settings)
    except Exception:
        return {}


def save_web_settings(org: Organization, settings: dict) -> None:
    """Encrypt settings back onto the org row (caller commits)."""
    org.web_settings = encrypt_json(settings) if settings else None


def load_notify_settings(org: Organization) -> dict:
    """Decrypt the org's notification preferences. Empty dict when unset."""
    if not org.notify_settings:
        return {}
    try:
        return decrypt_json(org.notify_settings)
    except Exception:
        return {}


def save_notify_settings(org: Organization, settings: dict) -> None:
    """Encrypt notification preferences back onto the org row (caller commits)."""
    org.notify_settings = encrypt_json(settings) if settings else None


def get_tavily_key(org: Organization | None) -> str | None:
    """The org's Tavily API key, or None when unset — callers fall back to DuckDuckGo."""
    if org is None:
        return None
    return load_web_settings(org).get("tavily_api_key") or None
