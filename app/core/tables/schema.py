"""
Column type registry for org tables
=====================================
Validates, coerces, and describes table columns for both the API and the agent tool
descriptions.  The tool description uses types and select options but never real row
data, so no PII appears in prompts.
"""
from __future__ import annotations

import re
from typing import Any

# ── Supported column types ─────────────────────────────────────────────────────

COLUMN_TYPES = {
    "text",
    "long_text",
    "number",
    "checkbox",
    "date",       # YYYY-MM-DD
    "datetime",   # ISO-8601
    "email",
    "phone",
    "url",
    "select",     # requires options list
    "link",       # foreign key to another org table; requires link_table_id
}

# Synthetic sample values injected into tool descriptions so the model understands the format.
# These are never real rows — purely illustrative.
_SYNTHETIC_SAMPLES: dict[str, Any] = {
    "text":      "Example text",
    "long_text": "A longer description or note.",
    "number":    42,
    "checkbox":  False,
    "date":      "2026-01-15",
    "datetime":  "2026-01-15T09:00:00Z",
    "email":     "person@example.com",
    "phone":     "+15551234567",
    "url":       "https://example.com",
    "select":    None,   # replaced with first option when available
    "link":      None,   # replaced with "<row-id>"
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s\-().]{7,20}$")
_DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_RE   = re.compile(r"^https?://\S+$")

MAX_CELL_CHARS = 10_000
MAX_ROWS_PER_TABLE = 50_000
MAX_COLUMNS_PER_TABLE = 50
MAX_TABLE_SLUG_LEN = 40


class ColumnError(ValueError):
    """Raised when a column definition or a cell value is invalid."""


def slugify(name: str) -> str:
    """Turn a human column/table name into a safe slug for use in tool names and JSONB keys."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower().strip())
    cleaned = cleaned.strip("_")[:MAX_TABLE_SLUG_LEN]
    if not cleaned:
        raise ColumnError("Name produces an empty slug")
    # Must not start with a digit (tool names are used as identifiers)
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned


def validate_column_def(col: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a column descriptor dict.

    Returns the normalised dict.  Raises ColumnError on invalid input.
    """
    key  = str(col.get("key", "")).strip()
    name = str(col.get("name", "")).strip()
    typ  = str(col.get("type", "")).strip()

    if not key:
        raise ColumnError("Column key is required")
    if not re.match(r"^[a-z][a-z0-9_]{0,39}$", key):
        raise ColumnError(f"Column key '{key}' must be lowercase alphanumeric/underscore, start with a letter")
    if not name:
        raise ColumnError("Column name is required")
    if typ not in COLUMN_TYPES:
        raise ColumnError(f"Unknown column type '{typ}'. Allowed: {sorted(COLUMN_TYPES)}")

    out: dict[str, Any] = {
        "key":               key,
        "name":              name,
        "type":              typ,
        "required":          bool(col.get("required", False)),
        "hidden_from_agents": bool(col.get("hidden_from_agents", False)),
    }

    if typ == "select":
        options = col.get("options") or []
        if not isinstance(options, list) or not options:
            raise ColumnError(f"Column '{key}' of type 'select' requires a non-empty options list")
        out["options"] = [str(o).strip() for o in options if str(o).strip()]
        if not out["options"]:
            raise ColumnError(f"Column '{key}' has no valid options")

    if typ == "link":
        link_id = col.get("link_table_id")
        if not link_id:
            raise ColumnError(f"Column '{key}' of type 'link' requires link_table_id")
        out["link_table_id"] = str(link_id)

    return out


def coerce_value(key: str, col_def: dict[str, Any], raw: Any, *, lenient: bool = False) -> Any:
    """Coerce and validate a single cell value against its column definition.

    `lenient=True` falls back to a string on type mismatch (used during import).
    Raises ColumnError on invalid value when not lenient.
    Returns None unchanged.
    """
    if raw is None or raw == "":
        return None

    typ = col_def.get("type", "text")

    try:
        if typ == "text":
            return _cap(str(raw))
        if typ == "long_text":
            return _cap(str(raw))
        if typ == "number":
            try:
                return float(raw) if "." in str(raw) else int(raw)
            except (ValueError, TypeError):
                if lenient:
                    return None
                raise ValueError(f"'{key}' is not a valid number: {raw!r}")
        if typ == "checkbox":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("true", "1", "yes")
        if typ == "date":
            s = str(raw).strip()
            if not _DATE_RE.match(s):
                if lenient:
                    return None
                raise ColumnError(f"'{key}' must be YYYY-MM-DD, got: {s!r}")
            return s
        if typ == "datetime":
            return str(raw).strip()
        if typ == "email":
            s = str(raw).strip().lower()
            if not _EMAIL_RE.match(s):
                if lenient:
                    return None
                raise ColumnError(f"'{key}' is not a valid email: {s!r}")
            return s
        if typ == "phone":
            s = str(raw).strip()
            if not _PHONE_RE.match(s):
                if lenient:
                    return None
                raise ColumnError(f"'{key}' is not a valid phone: {s!r}")
            return s
        if typ == "url":
            s = str(raw).strip()
            if not _URL_RE.match(s):
                if lenient:
                    return None
                raise ColumnError(f"'{key}' is not a valid URL: {s!r}")
            return s
        if typ == "select":
            s = str(raw).strip()
            options = col_def.get("options", [])
            if s not in options:
                if lenient:
                    return None
                raise ColumnError(f"'{key}' value {s!r} not in options {options}")
            return s
        if typ == "link":
            return str(raw).strip()  # UUID string
        return _cap(str(raw))
    except (ColumnError, ValueError):
        if lenient:
            return None
        raise


def _cap(s: str) -> str:
    """Truncate to MAX_CELL_CHARS. Raises ColumnError if strict validation is needed."""
    if len(s) > MAX_CELL_CHARS:
        return s[:MAX_CELL_CHARS]
    return s


def validate_row_data(
    columns: list[dict[str, Any]],
    data: dict[str, Any],
    *,
    lenient: bool = False,
    partial: bool = False,
) -> dict[str, Any]:
    """Validate and coerce an entire row's data dict.

    Unknown keys are silently dropped.
    Required columns must be present unless `partial=True` (used for updates).
    Hidden columns are accepted from the API (owners can edit them) and stripped
    from agent-facing outputs separately.
    """
    col_map = {c["key"]: c for c in columns}
    out: dict[str, Any] = {}

    for key, raw in data.items():
        if key not in col_map:
            continue  # silently drop unknown columns
        col_def = col_map[key]
        out[key] = coerce_value(key, col_def, raw, lenient=lenient)

    if not partial:
        for col in columns:
            if col.get("required") and col["key"] not in out:
                if not lenient:
                    raise ColumnError(f"Required column '{col['key']}' is missing")

    return out


def strip_hidden(columns: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    """Return data with hidden_from_agents columns removed (for tool outputs)."""
    hidden = {c["key"] for c in columns if c.get("hidden_from_agents")}
    return {k: v for k, v in data.items() if k not in hidden}


def render_tool_description(table_name: str, columns: list[dict[str, Any]]) -> str:
    """Generate the tool description shown to the model for a specific table.

    Uses synthetic (non-PII) sample values.  Only agent-visible columns are described.
    """
    visible = [c for c in columns if not c.get("hidden_from_agents")]
    if not visible:
        return f"Table '{table_name}' (no agent-visible columns)"

    lines = [f"Table '{table_name}' columns:"]
    sample: dict[str, Any] = {}

    for col in visible:
        typ  = col["type"]
        key  = col["key"]
        name = col["name"]
        req  = " (required)" if col.get("required") else ""

        if typ == "select":
            opts = col.get("options", [])
            lines.append(f"  {key} ({name}): select — options: {', '.join(opts)}{req}")
            sample[key] = opts[0] if opts else "option"
        elif typ == "link":
            lines.append(f"  {key} ({name}): link to another table row id{req}")
            sample[key] = "<row-id>"
        else:
            lines.append(f"  {key} ({name}): {typ}{req}")
            sample[key] = _SYNTHETIC_SAMPLES.get(typ, "value")

    import json as _json
    lines.append(f"Sample: {_json.dumps(sample)}")
    return "\n".join(lines)
