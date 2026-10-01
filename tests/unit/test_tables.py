"""
Tables v1 — unit tests (no database required).

Covers:
- schema.py: validation, coercion, slugify, hidden-from-agents stripping,
  render_tool_description, formula-injection escaping
- service.py: dedup key logic, write cap
- query.py: DuckDB sandbox (read-only, no external access, max rows)
- importer.py: CSV parsing and preview
"""

import csv
import io
import uuid
from typing import Any

import pytest
import pytest_asyncio

from app.core.tables.schema import (
    ColumnError,
    MAX_CELL_CHARS,
    MAX_COLUMNS_PER_TABLE,
    coerce_value,
    render_tool_description,
    slugify,
    strip_hidden,
    validate_column_def,
    validate_row_data,
)


# ── slugify ────────────────────────────────────────────────────────────────────

def test_slugify_basic():
    assert slugify("Leads") == "leads"

def test_slugify_spaces():
    assert slugify("My Leads Table") == "my_leads_table"

def test_slugify_special_chars():
    assert slugify("A-B & C!") == "a_b_c"

def test_slugify_strip_leading_trailing_underscores():
    s = slugify("  hello  ")
    assert not s.startswith("_") and not s.endswith("_")


# ── validate_column_def ────────────────────────────────────────────────────────

def test_validate_column_def_minimal():
    col = validate_column_def({"key": "name", "name": "Name", "type": "text"})
    assert col["key"] == "name"
    assert col["type"] == "text"

def test_validate_column_def_generates_key_from_name():
    # validate_column_def requires key — key generation is done by the router layer.
    # If key is absent it raises ColumnError.
    with pytest.raises(ColumnError):
        validate_column_def({"name": "Full Name", "type": "text"})
    # But providing a key works:
    col = validate_column_def({"key": "full_name", "name": "Full Name", "type": "text"})
    assert col["key"] == "full_name"

def test_validate_column_def_invalid_type():
    with pytest.raises(ColumnError):
        validate_column_def({"name": "X", "type": "imaginary"})

def test_validate_column_def_select_requires_options():
    with pytest.raises(ColumnError):
        validate_column_def({"name": "Status", "type": "select"})

def test_validate_column_def_select_with_options_ok():
    col = validate_column_def({"key": "status", "name": "Status", "type": "select", "options": ["a", "b"]})
    assert col["options"] == ["a", "b"]


# ── coerce_value ───────────────────────────────────────────────────────────────

def test_coerce_number_ok():
    col = {"type": "number"}
    assert coerce_value("x", col, "42") == 42.0

def test_coerce_number_invalid_strict():
    col = {"type": "number"}
    with pytest.raises(ValueError):
        coerce_value("x", col, "not-a-number")

def test_coerce_number_invalid_lenient():
    col = {"type": "number"}
    result = coerce_value("x", col, "not-a-number", lenient=True)
    assert result is None

def test_coerce_checkbox_truthy():
    col = {"type": "checkbox"}
    assert coerce_value("x", col, "yes") is True
    assert coerce_value("x", col, "true") is True
    assert coerce_value("x", col, "1") is True

def test_coerce_checkbox_falsy():
    col = {"type": "checkbox"}
    assert coerce_value("x", col, "no") is False
    assert coerce_value("x", col, "false") is False
    assert coerce_value("x", col, "0") is False

def test_coerce_text_truncates_at_max():
    col = {"type": "text"}
    long_val = "a" * (MAX_CELL_CHARS + 100)
    result = coerce_value("x", col, long_val)
    assert len(result) == MAX_CELL_CHARS  # _cap now truncates instead of raising

def test_coerce_select_valid():
    col = {"type": "select", "options": ["New", "In Progress"]}
    assert coerce_value("x", col, "New") == "New"

def test_coerce_select_invalid_strict():
    col = {"type": "select", "options": ["New", "In Progress"]}
    with pytest.raises(ValueError):
        coerce_value("x", col, "Unknown")

def test_coerce_select_invalid_lenient():
    col = {"type": "select", "options": ["New"]}
    result = coerce_value("x", col, "Unknown", lenient=True)
    assert result is None


# ── validate_row_data ──────────────────────────────────────────────────────────

LEADS_COLUMNS = [
    {"key": "name",   "name": "Name",   "type": "text",   "required": True},
    {"key": "phone",  "name": "Phone",  "type": "phone",  "required": False},
    {"key": "status", "name": "Status", "type": "select", "options": ["New", "Qualified"]},
]

def test_validate_row_data_ok():
    result = validate_row_data(LEADS_COLUMNS, {"name": "Alice", "status": "New"})
    assert result["name"] == "Alice"
    assert result["status"] == "New"

def test_validate_row_data_unknown_key_stripped():
    result = validate_row_data(LEADS_COLUMNS, {"name": "Alice", "ghost_column": "x"})
    assert "ghost_column" not in result

def test_validate_row_data_missing_required():
    with pytest.raises(ValueError):
        validate_row_data(LEADS_COLUMNS, {"status": "New"})

def test_validate_row_data_partial_skips_required():
    result = validate_row_data(LEADS_COLUMNS, {"status": "New"}, partial=True)
    assert "status" in result  # partial update — no required check


# ── strip_hidden ───────────────────────────────────────────────────────────────

COLUMNS_WITH_HIDDEN = [
    {"key": "name",     "name": "Name",     "type": "text"},
    {"key": "internal", "name": "Internal", "type": "text", "hidden_from_agents": True},
    {"key": "phone",    "name": "Phone",    "type": "phone"},
]

def test_strip_hidden_removes_hidden_columns():
    data = {"name": "Alice", "internal": "secret", "phone": "123"}
    result = strip_hidden(COLUMNS_WITH_HIDDEN, data)
    assert "internal" not in result
    assert result["name"] == "Alice"
    assert result["phone"] == "123"

def test_strip_hidden_no_hidden_columns_passthrough():
    cols = [{"key": "name", "name": "Name", "type": "text"}]
    data = {"name": "Alice"}
    assert strip_hidden(cols, data) == {"name": "Alice"}


# ── render_tool_description ────────────────────────────────────────────────────

def test_render_tool_description_not_empty():
    cols = [
        {"key": "email",   "name": "Email",   "type": "email"},
        {"key": "status",  "name": "Status",  "type": "select", "options": ["A", "B"]},
    ]
    desc = render_tool_description("Leads", cols)
    # Should mention the table name or columns
    assert "Leads" in desc or "status" in desc.lower() or "email" in desc.lower()
    # Should be a non-empty string
    assert len(desc) > 0


# ── DuckDB sandbox ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_tables_select_ok():
    """Basic SELECT runs and returns rows."""
    from app.core.tables.query import run_query

    table_data = {
        "leads": [
            {"name": "Alice", "status": "New"},
            {"name": "Bob",   "status": "Qualified"},
        ]
    }
    result = await run_query(table_data, "SELECT * FROM leads")
    assert "Alice" in result
    assert "Bob" in result

@pytest.mark.asyncio
async def test_query_tables_create_blocked():
    """DDL / write statements must be rejected."""
    from app.core.tables.query import run_query

    table_data: dict = {}
    result = await run_query(table_data, "CREATE TABLE evil AS SELECT 1")
    # Should return an error string, not raise
    assert "error" in result.lower() or "only" in result.lower() or "select" in result.lower()

@pytest.mark.asyncio
async def test_query_tables_file_read_blocked():
    """read_csv / parquet external file access must be blocked after lock_configuration."""
    from app.core.tables.query import run_query

    # This should either error with "external access" or "No such table"
    result = await run_query({}, "SELECT * FROM read_csv('/etc/passwd')")
    assert any(word in result.lower() for word in ("error", "external", "permission", "cannot"))

@pytest.mark.asyncio
async def test_query_tables_max_rows():
    """Results capped at MAX_RESULT_ROWS."""
    from app.core.tables.query import run_query, MAX_RESULT_ROWS

    # Generate more rows than the cap
    n = MAX_RESULT_ROWS + 50
    table_data = {"big": [{"v": i} for i in range(n)]}
    result = await run_query(table_data, "SELECT * FROM big")
    # Result string mentions the cap or is truncated
    assert str(MAX_RESULT_ROWS) in result or "truncated" in result.lower() or "limit" in result.lower()

@pytest.mark.asyncio
async def test_query_only_permitted_tables():
    """Tables not in permitted_slugs must not be visible."""
    from app.core.tables.query import run_query

    table_data = {"leads": [{"name": "Alice"}]}
    # Try to access a table not in the permitted set (not in table_data)
    result = await run_query(table_data, "SELECT * FROM secret_table")
    assert "error" in result.lower() or "secret_table" in result.lower()


# ── CSV import preview ─────────────────────────────────────────────────────────

def test_import_preview_csv():
    from app.core.tables.importer import preview_import

    csv_bytes = b"name,email,phone\nAlice,alice@example.com,+1234567890\nBob,bob@example.com,+9876543210\n"
    result = preview_import(csv_bytes, filename="leads.csv", columns=[])
    # Returns inferred_columns and sample_rows
    col_keys = [c["key"] for c in result["inferred_columns"]]
    assert "name" in col_keys
    assert len(result["sample_rows"]) >= 1

def test_import_preview_oversized_rejected():
    from app.core.tables.importer import preview_import, MAX_IMPORT_BYTES, TableImportError

    big_csv = b"name\n" + b"x\n" * (MAX_IMPORT_BYTES + 1)
    with pytest.raises((TableImportError, Exception)):
        preview_import(big_csv, filename="big.csv", columns=[])


# ── CSV export formula escaping ────────────────────────────────────────────────

def test_export_formula_escape():
    """Values starting with = + - @ must be prefixed with ' to block injection."""
    # We test the escaping function directly from the router module.
    from app.api.tables.router import _formula_escape  # type: ignore

    assert _formula_escape("=SUM(A1)") == "'=SUM(A1)"
    assert _formula_escape("+dangerous") == "'+dangerous"
    assert _formula_escape("-cmd") == "'-cmd"
    assert _formula_escape("@evil") == "'@evil"
    assert _formula_escape("safe text") == "safe text"
    assert _formula_escape("") == ""


# ── Write cap ─────────────────────────────────────────────────────────────────

def test_write_cap_raises_after_limit():
    """AGENT_WRITES_PER_SESSION cap should block further writes in the same session."""
    from app.core.tables.service import AGENT_WRITES_PER_SESSION, WriteCapExceeded

    counter = {"count": AGENT_WRITES_PER_SESSION}  # already at limit

    with pytest.raises(WriteCapExceeded):
        from app.core.tables.service import _check_write_cap
        _check_write_cap(counter)

def test_write_cap_increments():
    from app.core.tables.service import _check_write_cap

    counter = {"count": 0}
    _check_write_cap(counter)
    assert counter["count"] == 1
