import copy

import pytest

from invoice_reader.ground_truth import EXPECTED_STOP, load_samples
from invoice_reader.validate import registration_check_digit, validate

SAMPLES = load_samples()


def codes(problems):
    return {p.code for p in problems}


# ---------- 正解データ30件 ----------


def test_samples_are_30_and_3_have_errors():
    assert len(SAMPLES) == 30
    assert {s.file for s in SAMPLES if s.expected_error} == set(EXPECTED_STOP)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s.file)
def test_ground_truth(sample):
    problems = validate(sample.invoice)
    if sample.file in EXPECTED_STOP:
        assert codes(problems) == EXPECTED_STOP[sample.file]
    else:
        assert problems == []


# ---------- 1つずつのルール ----------


def base(**changes):
    """検算を通る外税の請求書。10%と8%が1行ずつ。"""
    inv = {
        "invoice_no": "INV-1",
        "issuer": "架空商事株式会社",
        "registration_no": "T8999900000001",
        "recipient": "株式会社テスト商会",
        "issue_date": "2026-09-30",
        "due_date": "2026-10-31",
        "tax_mode": "外税",
        "taxable_10": 10000,
        "tax_10": 1000,
        "taxable_8": 5000,
        "tax_8": 400,
        "withholding": 0,
        "total": 16400,
        "is_handwritten": False,
        "lines": [
            {"name": "作業費", "qty": 1, "unit": "式", "price": 10000, "amount": 10000, "rate": 10},
            {"name": "食品", "qty": 5, "unit": "個", "price": 1000, "amount": 5000, "rate": 8},
        ],
    }
    inv.update(changes)
    return inv


def test_base_passes():
    assert validate(base()) == []


# 必須項目


@pytest.mark.parametrize("field", ["invoice_no", "issuer", "issue_date"])
@pytest.mark.parametrize("empty", [None, "", "  "])
def test_required_text_fields(field, empty):
    problems = validate(base(**{field: empty}))
    assert codes(problems) == {"required"}
    assert problems[0].field == field


def test_required_total():
    assert codes(validate(base(total=None))) == {"required"}


# 金額の型


@pytest.mark.parametrize("bad", [16400.0, "16,400", True])
def test_amount_must_be_integer(bad):
    assert codes(validate(base(total=bad))) == {"not_integer"}


def test_unreadable_amount_is_flagged():
    problems = validate(base(tax_8=None))
    assert codes(problems) == {"missing"}
    assert problems[0].field == "tax_8"


def test_unknown_tax_mode():
    assert codes(validate(base(tax_mode=None))) == {"tax_mode"}


# 明細の合計


def test_line_sum_mismatch():
    inv = base()
    inv["lines"][0]["amount"] = 9000
    assert codes(validate(inv)) == {"line_sum"}


def test_discount_line_is_subtracted():
    inv = base()
    inv["lines"][0]["amount"] = 11000
    inv["lines"].append({"name": "値引き", "qty": 1, "unit": "式", "price": -1000, "amount": -1000, "rate": 10})
    assert validate(inv) == []


@pytest.mark.parametrize("bad_line", [{"amount": 10000.0}, {"amount": None}, {"rate": 5}])
def test_broken_line(bad_line):
    inv = base()
    inv["lines"][0].update(bad_line)
    assert codes(validate(inv)) == {"line_invalid"}


# 消費税


def test_tax_off_by_one_yen_is_allowed():
    assert validate(base(tax_10=1001, total=16401)) == []


def test_tax_off_by_two_yen_is_flagged():
    problems = validate(base(tax_10=1002, total=16402))
    assert codes(problems) == {"tax"}
    assert problems[0].field == "tax_10"


def test_inclusive_tax():
    # 内税：明細は税込。2,800円（10%）の消費税は 2800*10/110 = 254.5 の切り捨てで254円
    inv = base(
        tax_mode="内税",
        taxable_10=2546,
        tax_10=254,
        taxable_8=0,
        tax_8=0,
        total=2800,
        lines=[{"name": "ごみ袋", "qty": 4, "unit": "袋", "price": 700, "amount": 2800, "rate": 10}],
    )
    assert validate(inv) == []

    inv["tax_10"], inv["taxable_10"] = 280, 2520  # 外税の計算（2800の10%）をしてしまった誤り
    assert codes(validate(inv)) == {"tax"}


# 請求額


def test_total_mismatch():
    assert codes(validate(base(total=17400))) == {"total"}


# 源泉所得税


def test_withholding():
    # 107,500円 × 10.21% = 10,975.75 の切り捨てで 10,975円
    inv = base(
        taxable_10=107500,
        tax_10=10750,
        taxable_8=0,
        tax_8=0,
        withholding=10975,
        total=107275,
        lines=[{"name": "デザイン料", "qty": 1, "unit": "式", "price": 107500, "amount": 107500, "rate": 10}],
    )
    assert validate(inv) == []

    inv["withholding"], inv["total"] = 10750, 107500  # 10% で計算してしまった誤り
    assert codes(validate(inv)) == {"withholding"}


# 登録番号


def test_check_digit_example():
    # T8999900000001：本体 999900000001 から検査数字 8
    assert registration_check_digit("999900000001") == 8


@pytest.mark.parametrize(
    "regno",
    [None, "8999900000001", "T899990000001", "T89999000000012", "T８９９９９０００００００１", "T8999-900000001"],
)
def test_registration_format(regno):
    assert codes(validate(base(registration_no=regno))) == {"regno_format"}


def test_registration_check_digit_mismatch():
    assert codes(validate(base(registration_no="T1999900000001"))) == {"regno_check_digit"}


# 日付


@pytest.mark.parametrize("bad", ["2026-02-30", "2026/09/30", "令和8年9月30日", "20260930"])
def test_invalid_issue_date(bad):
    problems = validate(base(issue_date=bad))
    assert codes(problems) == {"date_invalid"}
    assert problems[0].field == "issue_date"


def test_due_date_may_be_missing():
    assert validate(base(due_date=None)) == []


@pytest.mark.parametrize("due", ["2026-09-30", "2026-09-29"])
def test_due_date_must_be_after_issue_date(due):
    assert codes(validate(base(due_date=due))) == {"due_before_issue"}


# 二重登録


def test_duplicate():
    calls = []

    def exists(issuer, invoice_no):
        calls.append((issuer, invoice_no))
        return True

    assert codes(validate(base(), exists=exists)) == {"duplicate"}
    assert calls == [("架空商事株式会社", "INV-1")]


def test_not_duplicate():
    assert validate(base(), exists=lambda issuer, invoice_no: False) == []


# 手書き


def test_handwritten_goes_to_review_even_if_everything_else_passes():
    assert codes(validate(base(is_handwritten=True))) == {"handwritten"}


# 入力を書き換えない


def test_validate_does_not_modify_input():
    inv = base()
    before = copy.deepcopy(inv)
    validate(inv)
    assert inv == before
