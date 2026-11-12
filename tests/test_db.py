import sqlite3

import pytest

from invoice_reader.db import INVOICE_FIELDS, connect, init_db, invoice_exists


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_staging(conn, file_hash="h1"):
    cur = conn.execute(
        "INSERT INTO staging (file_name, file_hash) VALUES (?, ?)", ("a.pdf", file_hash)
    )
    return cur.lastrowid


def _add_invoice(conn, staging_id, invoice_no="INV-1", issuer="発行元A"):
    conn.execute(
        """INSERT INTO invoices
           (staging_id, invoice_no, issuer, issue_date, total, original_path, approved_by)
           VALUES (?, ?, ?, '2026-09-30', 1000, 'done/x.pdf', 'auto')""",
        (staging_id, invoice_no, issuer),
    )


def test_creates_four_tables(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"staging", "invoices", "invoice_lines", "corrections"} <= tables


def test_staging_and_invoices_have_all_json_fields(conn):
    for table in ("staging", "invoices"):
        assert set(INVOICE_FIELDS) <= _columns(conn, table), table


def test_init_db_can_run_twice(conn):
    init_db(conn)


def test_same_file_hash_is_rejected(conn):
    _add_staging(conn, "same")
    with pytest.raises(sqlite3.IntegrityError):
        _add_staging(conn, "same")


def test_same_issuer_and_invoice_no_is_rejected(conn):
    _add_invoice(conn, _add_staging(conn, "h1"))
    with pytest.raises(sqlite3.IntegrityError):
        _add_invoice(conn, _add_staging(conn, "h2"))


def test_same_invoice_no_from_another_issuer_is_allowed(conn):
    _add_invoice(conn, _add_staging(conn, "h1"), issuer="発行元A")
    _add_invoice(conn, _add_staging(conn, "h2"), issuer="発行元B")


@pytest.mark.parametrize("bad_total", [1000.5, "1,000"])
def test_amount_must_be_integer(conn, bad_total):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO staging (file_name, file_hash, total) VALUES ('a.pdf', 'h', ?)",
            (bad_total,),
        )


def test_unknown_status_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO staging (file_name, file_hash, status) VALUES ('a.pdf', 'h', 'done')"
        )


def test_invoice_must_point_to_existing_staging_row(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _add_invoice(conn, staging_id=999)


def test_invoice_exists(conn):
    _add_invoice(conn, _add_staging(conn), invoice_no="INV-1", issuer="発行元A")

    assert invoice_exists(conn, "発行元A", "INV-1")
    assert not invoice_exists(conn, "発行元A", "INV-2")
    assert not invoice_exists(conn, "発行元B", "INV-1")
