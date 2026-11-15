"""pipeline.py のテスト。AIの代わりに、正解データを返す偽物の読み取りを使う。"""

import json
import shutil

import pytest

from invoice_reader import db
from invoice_reader.config import ensure_data_dirs, load_settings
from invoice_reader.ground_truth import load_samples
from invoice_reader.llm import LLMError, ReadResult
from invoice_reader.pipeline import (
    IntakeError,
    inbox_files,
    process_file,
    recover_processing,
    run_inbox,
    save_to_inbox,
)

SAMPLES = {s.file: s for s in load_samples()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "MODEL", "JEV_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(env_file=tmp_path / "none.env", data_dir=tmp_path / "data")
    ensure_data_dirs(settings)
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    yield settings, conn
    conn.close()


def reader_for(data):
    """決まった読み取り結果を返す偽物の読み取り。"""

    def reader(extracted):
        return ReadResult(
            data=data,
            source=extracted.source,
            model="fake",
            input_tokens=0,
            output_tokens=0,
            seconds=0.0,
            request_id=None,
        )

    return reader


def put_in_inbox(settings, sample_file, name=None, extra=b""):
    """sample_data からコピーして inbox に置く（sample_data は書き換えない）。"""
    src = SAMPLES[sample_file].path
    dest = settings.folder("inbox") / (name or src.name)
    shutil.copyfile(src, dest)
    if extra:
        with dest.open("ab") as f:
            f.write(extra)
    return dest


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_passing_invoice_is_registered_and_renamed(env):
    settings, conn = env
    path = put_in_inbox(settings, "invoice_01")

    o = process_file(path, settings, conn, reader_for(SAMPLES["invoice_01"].invoice))

    assert o.status == "passed"
    assert o.moved_to == settings.folder("done") / "20260930_瀬戸内サンプル物流株式会社_119548.pdf"
    assert o.moved_to.exists() and not path.exists()
    inv = conn.execute("SELECT * FROM invoices").fetchone()
    assert (inv["invoice_no"], inv["total"], inv["approved_by"]) == ("INV-2026-7771", 119548, "auto")
    assert inv["original_path"] == "done/20260930_瀬戸内サンプル物流株式会社_119548.pdf"
    lines = conn.execute("SELECT invoice_id FROM invoice_lines").fetchall()
    assert len(lines) == 3 and all(r["invoice_id"] == inv["id"] for r in lines)
    assert conn.execute("SELECT status FROM staging").fetchone()["status"] == "passed"


def test_same_file_twice_is_not_registered_twice(env):
    settings, conn = env
    reader = reader_for(SAMPLES["invoice_01"].invoice)
    process_file(put_in_inbox(settings, "invoice_01"), settings, conn, reader)

    o = process_file(put_in_inbox(settings, "invoice_01", name="again.pdf"), settings, conn, reader)

    assert o.status == "skipped"
    assert o.read is None  # AIを呼ぶ前に止まる
    assert o.problems[0]["message"] == "同じ内容のファイル（invoice_01.pdf）をすでに読み取っています"
    assert (settings.folder("error") / "again.pdf").exists()
    assert count(conn, "invoices") == 1
    assert count(conn, "staging") == 1


def test_same_invoice_in_another_file_goes_to_review(env):
    settings, conn = env
    reader = reader_for(SAMPLES["invoice_01"].invoice)
    process_file(put_in_inbox(settings, "invoice_01"), settings, conn, reader)

    # 中身が1バイト違う別ファイル（再スキャンなど）。ハッシュは違うが、同じ請求書
    other = put_in_inbox(settings, "invoice_01", name="rescan.pdf", extra=b"\n")
    o = process_file(other, settings, conn, reader)

    assert o.status == "review"
    assert [p["code"] for p in o.problems] == ["duplicate"]
    assert count(conn, "invoices") == 1


def test_failing_invoice_goes_to_review_with_reasons(env):
    settings, conn = env
    path = put_in_inbox(settings, "invoice_08")

    o = process_file(path, settings, conn, reader_for(SAMPLES["invoice_08"].invoice))

    assert o.status == "review"
    assert o.moved_to == settings.folder("review") / "invoice_08.pdf"
    row = conn.execute("SELECT * FROM staging").fetchone()
    assert row["status"] == "review"
    assert row["stored_path"] == "review/invoice_08.pdf"
    assert [e["code"] for e in json.loads(row["errors"])] == ["total"]
    assert count(conn, "invoices") == 0


def test_broken_file_goes_to_error(env):
    settings, conn = env
    path = settings.folder("inbox") / "broken.pdf"
    path.write_bytes(b"not a pdf")

    o = process_file(path, settings, conn, reader_for({}))

    assert o.status == "error"
    assert (settings.folder("error") / "broken.pdf").exists()
    row = conn.execute("SELECT status, errors FROM staging").fetchone()
    assert row["status"] == "error"
    assert json.loads(row["errors"])[0]["code"] == "unreadable"


def test_unusable_ai_answer_goes_to_error(env):
    settings, conn = env
    path = put_in_inbox(settings, "invoice_01")

    def reader(extracted):
        raise LLMError("AIの返事が途中で切れました")

    o = process_file(path, settings, conn, reader)

    assert o.status == "error"
    assert "途中で切れました" in o.problems[0]["message"]


def test_api_failure_puts_file_back_in_inbox(env):
    settings, conn = env
    path = put_in_inbox(settings, "invoice_01")

    def reader(extracted):
        raise ConnectionError("network down")

    with pytest.raises(ConnectionError):
        process_file(path, settings, conn, reader)

    assert path.exists()  # inbox に戻っている
    assert count(conn, "staging") == 0


def test_run_inbox_processes_every_file(env):
    settings, conn = env
    put_in_inbox(settings, "invoice_01")

    outcomes = run_inbox(settings, conn, reader_for(SAMPLES["invoice_01"].invoice))

    assert [(o.file_name, o.status) for o in outcomes] == [("invoice_01.pdf", "passed")]
    assert list(settings.folder("inbox").iterdir()) == []


# ---------- 画面からファイルを受け取る ----------


def test_save_to_inbox_keeps_only_the_file_name(env):
    settings, _ = env
    data = SAMPLES["invoice_01"].path.read_bytes()

    dest = save_to_inbox(settings, r"C:\Users\someone\請求書.PDF", data)

    assert dest == settings.folder("inbox") / "請求書.PDF"
    assert dest.read_bytes() == data
    assert inbox_files(settings) == [dest]  # 書き込み途中の仮のファイルは残らない


def test_save_to_inbox_does_not_overwrite(env):
    settings, _ = env
    first = save_to_inbox(settings, "a.pdf", b"%PDF-1")
    second = save_to_inbox(settings, "a.pdf", b"%PDF-2")

    assert (first.name, second.name) == ("a.pdf", "a_2.pdf")
    assert first.read_bytes() == b"%PDF-1"


@pytest.mark.parametrize(
    "name, data, message",
    [
        ("memo.txt", b"x", "読み取れない種類のファイルです"),
        ("scan.gif", b"x", "読み取れない種類のファイルです"),
        ("empty.pdf", b"", "中身が空のファイルです"),
    ],
)
def test_save_to_inbox_rejects(env, name, data, message):
    settings, _ = env
    with pytest.raises(IntakeError, match=message):
        save_to_inbox(settings, name, data)
    assert list(settings.folder("inbox").iterdir()) == []


def test_inbox_files_skips_hidden_files(env):
    settings, _ = env
    (settings.folder("inbox") / ".a.pdf.part").write_bytes(b"x")
    (settings.folder("inbox") / "b.pdf").write_bytes(b"x")

    assert [p.name for p in inbox_files(settings)] == ["b.pdf"]


def test_recover_processing_puts_files_back(env):
    settings, conn = env
    (settings.folder("processing") / "stuck.pdf").write_bytes(b"x")
    (settings.folder("inbox") / "stuck.pdf").write_bytes(b"y")  # 同じ名前があっても上書きしない

    recovered = recover_processing(settings)

    assert [p.name for p in recovered] == ["stuck_2.pdf"]
    assert list(settings.folder("processing").iterdir()) == []
    assert (settings.folder("inbox") / "stuck.pdf").read_bytes() == b"y"
