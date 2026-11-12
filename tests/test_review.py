import copy
import shutil

import pytest

from invoice_reader import db
from invoice_reader.config import ensure_data_dirs, load_settings
from invoice_reader.ground_truth import load_samples
from invoice_reader.review import ReviewError, approve, changes, load, review_list, save_and_check

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


def add_review_item(settings, conn, sample_file):
    """検算で引っかかって review/ にある状態を作る。"""
    sample = SAMPLES[sample_file]
    dest = settings.folder("review") / sample.path.name
    shutil.copyfile(sample.path, dest)
    with conn:
        sid = db.insert_staging(
            conn,
            file_name=sample.path.name,
            file_hash=sample_file,
            stored_path=f"review/{sample.path.name}",
            source="text",
            data=sample.invoice,
            status="review",
            errors=[{"code": "total", "field": "total", "message": "請求額がずれています"}],
        )
    return sid


def test_review_list(env):
    settings, conn = env
    sid = add_review_item(settings, conn, "invoice_08")

    items = review_list(conn)

    assert [(i["id"], i["file_name"]) for i in items] == [(sid, "invoice_08.pdf")]
    assert items[0]["errors"][0]["message"] == "請求額がずれています"


def test_load_returns_the_same_shape_as_the_ai_json(env):
    settings, conn = env
    sid = add_review_item(settings, conn, "invoice_08")

    assert load(conn, sid)["data"] == SAMPLES["invoice_08"].invoice


def test_cannot_approve_before_fixing(env):
    settings, conn = env
    sid = add_review_item(settings, conn, "invoice_08")

    with pytest.raises(ReviewError, match="検算が通っていない"):
        approve(conn, settings, sid, "山田")
    assert (settings.folder("review") / "invoice_08.pdf").exists()


def test_fix_record_and_approve(env):
    settings, conn = env
    sid = add_review_item(settings, conn, "invoice_08")
    fixed = copy.deepcopy(load(conn, sid)["data"])
    fixed["total"] = 77640  # 記載の請求額を、計算した額に直す

    problems = save_and_check(conn, sid, fixed, "山田")

    assert problems == []
    rec = db.list_corrections(conn, sid)
    assert [(r["field"], r["old_value"], r["new_value"], r["corrected_by"]) for r in rec] == [
        ("total", "86640", "77640", "山田")
    ]
    assert load(conn, sid)["errors"] == []

    dest = approve(conn, settings, sid, "山田")

    assert dest.parent == settings.folder("done") and dest.exists()
    assert not (settings.folder("review") / "invoice_08.pdf").exists()
    inv = conn.execute("SELECT * FROM invoices").fetchone()
    assert (inv["total"], inv["approved_by"]) == (77640, "山田")
    assert load(conn, sid)["status"] == "approved"
    assert review_list(conn) == []


def test_save_keeps_the_reasons_when_still_wrong(env):
    settings, conn = env
    sid = add_review_item(settings, conn, "invoice_08")
    still_wrong = copy.deepcopy(load(conn, sid)["data"])
    still_wrong["total"] = 70000

    problems = save_and_check(conn, sid, still_wrong, "山田")

    assert [p.code for p in problems] == ["total"]
    assert load(conn, sid)["errors"][0]["code"] == "total"


def test_line_changes_are_recorded_by_line_number():
    old = {"lines": [{"name": "A", "qty": 1, "unit": "式", "price": 100, "amount": 100, "rate": 10}]}
    new = copy.deepcopy(old)
    new["lines"][0]["amount"] = 90
    new["lines"].append({"name": "値引き", "qty": 1, "unit": "式", "price": -10, "amount": -10, "rate": 10})

    assert changes(old, new) == [
        ("lines[1].amount", 100, 90),
        ("lines[2]", None, "値引き 1 式 -10 -10 10"),
    ]


def test_name_is_required(env):
    settings, conn = env
    sid = add_review_item(settings, conn, "invoice_08")

    with pytest.raises(ReviewError, match="確認者"):
        save_and_check(conn, sid, load(conn, sid)["data"], " ")
