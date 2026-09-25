"""Tabular ingest and the `query_data` guard — unit tests (no database).

The guard is the security boundary for SQL the model writes, so every escape route that
`enable_external_access=false` + `lock_configuration` is meant to close has a test here.
"""

import io
import os
import tempfile

import pytest
from openpyxl import Workbook

from app.core import tabular
from app.core.tabular import (
    MAX_RESULT_ROWS,
    TabularError,
    ingest,
    run_query,
    sanitise_identifier,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _parquet_path(draft, tmpdir) -> str:
    path = os.path.join(tmpdir, f"{draft.name}.parquet")
    with open(path, "wb") as fh:
        fh.write(draft.parquet)
    return path


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


TENDERS_CSV = (
    b"Ref No,Title (long),Deadline,Budget\n"
    b"T-1,Network upgrade,2026-10-03,240000\n"
    b"T-2,Roof repair,2026-11-03,12000\n"
    b"T-3,Fleet leasing,2026-12-01,98000\n"
)


# ── identifiers ────────────────────────────────────────────────────────────────

def test_sanitise_identifier_snake_cases_and_avoids_reserved():
    assert sanitise_identifier("Ref No", fallback="c") == "ref_no"
    assert sanitise_identifier("Title (long)", fallback="c") == "title_long"
    assert sanitise_identifier("2024 budget", fallback="c") == "c_2024_budget"
    assert sanitise_identifier("order", fallback="c") == "order_t"  # reserved word
    assert sanitise_identifier("???", fallback="col_3") == "col_3"


# ── ingest: CSV ────────────────────────────────────────────────────────────────

def test_csv_ingest_infers_types_and_normalises_names():
    (draft,) = ingest("tenders.csv", TENDERS_CSV, taken=set())
    assert draft.name == "tenders"
    assert draft.sheet is None
    assert draft.row_count == 3
    names = [c["name"] for c in draft.columns]
    assert names == ["ref_no", "title_long", "deadline", "budget"]
    assert draft.columns[0]["original"] == "Ref No"
    types = {c["name"]: c["type"] for c in draft.columns}
    assert types["deadline"] == "DATE"
    assert types["budget"] in ("BIGINT", "INTEGER", "HUGEINT")
    assert draft.sample[0][0] == "T-1"
    assert draft.text.startswith("ref_no,title_long,deadline,budget")


def test_csv_ingest_falls_back_to_text_on_mixed_dates():
    data = b"id,closing\n1,2026-10-03\n2,03/11/2026\n"
    (draft,) = ingest("mixed.csv", data, taken=set())
    types = {c["name"]: c["type"] for c in draft.columns}
    assert types["closing"] == "VARCHAR"
    assert draft.row_count == 2


def test_csv_ingest_dedupes_table_name_against_existing():
    (draft,) = ingest("tenders.csv", TENDERS_CSV, taken={"tenders"})
    assert draft.name == "tenders_2"


def test_csv_with_only_header_is_rejected():
    with pytest.raises(TabularError):
        ingest("empty.csv", b"a,b\n", taken=set())


# ── ingest: XLSX ───────────────────────────────────────────────────────────────

def test_xlsx_ingest_one_table_per_sheet_and_skips_title_rows():
    data = _xlsx({
        "Suppliers": [
            ["Approved supplier list 2026"],       # title row
            [],                                     # blank
            ["Name", "Country", "Rating"],          # header
            ["Acme", "CA", 4],
            ["Globex", "US", 5],
        ],
        "Rates": [
            ["Item", "Price"],
            ["Bolt", 1.5],
        ],
    })
    drafts = ingest("suppliers.xlsx", data, taken=set())
    by_name = {d.name: d for d in drafts}
    assert set(by_name) == {"suppliers__suppliers", "suppliers__rates"}
    sup = by_name["suppliers__suppliers"]
    assert [c["name"] for c in sup.columns] == ["name", "country", "rating"]
    assert sup.row_count == 2
    assert sup.sheet == "Suppliers"
    assert by_name["suppliers__rates"].row_count == 1


def test_xlsx_single_sheet_uses_plain_filename():
    data = _xlsx({"Sheet1": [["a", "b"], [1, 2]]})
    (draft,) = ingest("prices.xlsx", data, taken=set())
    assert draft.name == "prices"


def test_xlsx_without_data_is_rejected():
    with pytest.raises(TabularError):
        ingest("blank.xlsx", _xlsx({"S": [["only", "header"]]}), taken=set())


# ── query guard ────────────────────────────────────────────────────────────────

@pytest.fixture
def tenders_table():
    (draft,) = ingest("tenders.csv", TENDERS_CSV, taken=set())
    with tempfile.TemporaryDirectory() as tmp:
        yield [(draft.name, _parquet_path(draft, tmp))]


def test_select_returns_markdown_table(tenders_table):
    out = run_query(tenders_table, "SELECT ref_no, budget FROM tenders WHERE budget > 50000 ORDER BY budget")
    assert "| ref_no | budget |" in out
    assert "T-3" in out and "T-1" in out and "T-2" not in out
    assert out.rstrip().endswith("(2 rows)")


def test_filesystem_read_is_blocked(tenders_table):
    out = run_query(tenders_table, "SELECT * FROM read_csv('/etc/passwd')")
    assert out.startswith("SQL error")
    assert "disabled" in out or "Permission" in out


def test_filesystem_write_is_blocked(tenders_table):
    out = run_query(tenders_table, "COPY tenders TO '/tmp/should-not-exist.csv'")
    assert out.startswith("SQL error")
    assert not os.path.exists("/tmp/should-not-exist.csv")


def test_configuration_cannot_be_unlocked(tenders_table):
    out = run_query(tenders_table, "SET enable_external_access = true")
    assert out.startswith("SQL error")


def test_extension_install_is_blocked(tenders_table):
    out = run_query(tenders_table, "INSTALL httpfs")
    assert out.startswith("SQL error")


def test_multiple_statements_are_rejected(tenders_table):
    out = run_query(tenders_table, "SELECT 1; SELECT 2")
    assert "one SQL statement" in out


def test_trailing_semicolon_is_fine(tenders_table):
    out = run_query(tenders_table, "SELECT count(*) AS n FROM tenders;")
    assert "| 3 |" in out


def test_sql_error_is_returned_verbatim_not_raised(tenders_table):
    out = run_query(tenders_table, "SELECT nope FROM tenders")
    assert out.startswith("SQL error")
    assert "nope" in out


def test_summarize_and_describe_work(tenders_table):
    assert "column_name" in run_query(tenders_table, "SUMMARIZE tenders")
    assert "ref_no" in run_query(tenders_table, "DESCRIBE tenders")


def test_long_result_is_truncated_with_note():
    rows = "\n".join(f"{i},x" for i in range(MAX_RESULT_ROWS + 50))
    (draft,) = ingest("big.csv", f"id,v\n{rows}\n".encode(), taken=set())
    with tempfile.TemporaryDirectory() as tmp:
        out = run_query([(draft.name, _parquet_path(draft, tmp))], "SELECT * FROM big")
    assert f"first {MAX_RESULT_ROWS} rows" in out
    assert f"({MAX_RESULT_ROWS} rows)" in out


def test_empty_sql_prompts_for_statement(tenders_table):
    assert run_query(tenders_table, "   ") == "Provide a SQL statement."


# ── description ────────────────────────────────────────────────────────────────

def test_render_schema_lists_columns_and_sample():
    (draft,) = ingest("tenders.csv", TENDERS_CSV, taken=set())

    class _T:  # stand-in for the ORM row
        def __init__(self, d):
            self.file_id = "f1"
            self.name, self.sheet, self.row_count = d.name, d.sheet, d.row_count
            self.columns, self.sample = d.columns, d.sample

    text = tabular.render_schema([_T(draft)], {"f1": "tenders.csv"})
    assert "- tenders  (tenders.csv, 3 rows)" in text
    assert "ref_no VARCHAR [was: Ref No]" in text
    assert "deadline DATE" in text
    assert "e.g. T-1 | Network upgrade" in text
