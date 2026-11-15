import csv

import pytest

from invoice_reader import db
from invoice_reader.export import HEADER, JournalError, export_csv, journal_csv, journal_rows
from invoice_reader.ground_truth import EXPECTED_STOP, load_samples

GOOD_SAMPLES = [s for s in load_samples() if s.file not in EXPECTED_STOP]


@pytest.mark.parametrize("sample", GOOD_SAMPLES, ids=lambda s: s.file)
def test_debit_equals_credit_for_every_good_sample(sample):
    rows = journal_rows(sample.invoice, slip_no=1)
    debit = sum(r[4] for r in rows if r[4] != "")
    credit = sum(r[6] for r in rows if r[6] != "")
    assert debit == credit


def test_simple_invoice():
    inv = next(s.invoice for s in GOOD_SAMPLES if s.file == "invoice_01")
    rows = journal_rows(inv, slip_no=7)

    assert [(r[2], r[3], r[4], r[5], r[6]) for r in rows] == [
        ("仕入高", "課税仕入10%", 108680, "", ""),
        ("仮払消費税", "課税仕入10%", 10868, "", ""),
        ("", "", "", "買掛金", 119548),
    ]
    assert all(r[0] == 7 and r[1] == "2026-09-30" for r in rows)


def test_withholding_goes_to_azukarikin():
    # 様式E：個人事業主、源泉所得税あり
    inv = next(s.invoice for s in GOOD_SAMPLES if s.layout == "E")
    rows = journal_rows(inv, slip_no=1)

    assert rows[0][2] == "支払報酬料"
    assert ("預り金", inv["withholding"]) in [(r[5], r[6]) for r in rows]
    assert ("買掛金", inv["total"]) in [(r[5], r[6]) for r in rows]


def test_mixed_rates_are_split():
    inv = next(s.invoice for s in GOOD_SAMPLES if s.invoice["taxable_10"] and s.invoice["taxable_8"])
    classes = [r[3] for r in journal_rows(inv, slip_no=1) if r[3]]

    assert "課税仕入10%" in classes and "課税仕入8%（軽）" in classes


def test_unbalanced_invoice_is_rejected():
    inv = next(s for s in load_samples() if s.file == "invoice_08").invoice  # 請求額が9,000円ずれている
    with pytest.raises(JournalError):
        journal_rows(inv, slip_no=1)


def test_export_csv(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    inv = next(s.invoice for s in GOOD_SAMPLES if s.file == "invoice_01")
    with conn:
        sid = db.insert_staging(conn, file_name="a.pdf", file_hash="h", stored_path=None, data=inv)
        db.insert_invoice(conn, staging_id=sid, original_path="done/a.pdf", approved_by="auto")

    out = tmp_path / "out" / "journal.csv"
    assert export_csv(conn, out) == 1

    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel 用の BOM
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert rows[0] == HEADER
    assert len(rows) == 4
    conn.close()


def test_journal_csv_for_download():
    invoices = [{**s.invoice, "id": n} for n, s in enumerate(GOOD_SAMPLES[:2], start=1)]

    raw = journal_csv(invoices)

    assert raw.startswith(b"\xef\xbb\xbf")  # Excel 用の BOM
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    assert rows[0] == HEADER
    assert {r[0] for r in rows[1:]} == {"1", "2"}  # 伝票番号は請求書ごと


def test_unbalanced_invoice_makes_no_file(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    inv = next(s for s in load_samples() if s.file == "invoice_08").invoice  # 請求額が9,000円ずれている
    with conn:
        sid = db.insert_staging(conn, file_name="a.pdf", file_hash="h", stored_path=None, data=inv)
        db.insert_invoice(conn, staging_id=sid, original_path="done/a.pdf", approved_by="auto")

    out = tmp_path / "journal.csv"
    with pytest.raises(JournalError):
        export_csv(conn, out)
    assert not out.exists()
    conn.close()
