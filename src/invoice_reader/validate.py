"""検算。AIの読み取り結果（作業書5の形の dict）を、普通のプログラムで確かめる。

- AIには計算の正しさを判定させない。足し算・税額・桁数・日付はすべてここで確かめる
- 金額は整数（円）だけで計算する。小数を使わない
- 引っかかった理由の一覧を返す。空なら通過
- 読み取れていない値があるときは、その値を使う計算は飛ばす（同じ原因で理由が何個も並ばないように）

作業書「7. 検算」の表を上から順に実装している。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

RATES = (10, 8)
TAX_MODES = ("外税", "内税")
TOLERANCE = 1  # 消費税と源泉所得税で許す端数の差（円）

# 源泉所得税率 10.21% を整数で扱うため、10000分の1021 とする
WITHHOLDING_NUM = 1021
WITHHOLDING_DEN = 10000

LABELS = {
    "invoice_no": "請求書番号",
    "issuer": "発行元",
    "registration_no": "登録番号",
    "issue_date": "発行日",
    "due_date": "支払期日",
    "tax_mode": "税方式",
    "taxable_10": "10%対象（税抜）",
    "tax_10": "10%の消費税",
    "taxable_8": "8%対象（税抜）",
    "tax_8": "8%の消費税",
    "withholding": "源泉所得税",
    "total": "請求額",
}

REQUIRED = ("invoice_no", "issuer", "issue_date", "total")
AMOUNT_FIELDS = ("taxable_10", "tax_10", "taxable_8", "tax_8", "withholding", "total")

_REGNO = re.compile(r"T[0-9]{13}")  # \d は全角数字にも一致するので使わない
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Problem:
    code: str  # 理由の種類。テストや集計で使う
    field: str | None  # 関係する項目。確認画面で入力欄を目立たせるのに使う
    message: str  # 人が読む説明。確認画面に出す


ExistsFunc = Callable[[str, str], bool]


def validate(inv: dict, exists: ExistsFunc | None = None) -> list[Problem]:
    """請求書1件を検算し、引っかかった理由の一覧を返す。

    exists は（発行元, 請求書番号）が本番テーブルに既にあるかを返す関数。
    DBに触る部分を外から渡すことで、この関数はDBなしでテストできる。
    """
    problems: list[Problem] = []

    _check_required(inv, problems)
    amounts_ok = _check_amounts(inv, problems)
    tax_mode_ok = _check_tax_mode(inv, problems)
    lines_ok = _check_lines(inv, problems)

    if amounts_ok and tax_mode_ok:
        if lines_ok:
            _check_line_sums(inv, problems)
        _check_tax(inv, problems)
    if amounts_ok:
        _check_total(inv, problems)
        _check_withholding(inv, problems)

    _check_registration_no(inv, problems)
    _check_dates(inv, problems)
    _check_duplicate(inv, exists, problems)
    _check_handwritten(inv, problems)
    return problems


def registration_check_digit(body: str) -> int:
    """登録番号（T＋13桁）の検査数字を、後ろ12桁の本体から計算する。

    本体を右端から数えて n 桁目の数字に、n が奇数なら1、偶数なら2を掛けて合計し、
    9 から「合計を9で割った余り」を引いた値が検査数字。
    """
    total = 0
    for n, ch in enumerate(reversed(body), start=1):
        total += int(ch) * (1 if n % 2 == 1 else 2)
    return 9 - total % 9


# ---------- 個々の確認 ----------


def _check_required(inv: dict, problems: list[Problem]) -> None:
    for field in REQUIRED:
        if _is_empty(inv.get(field)):
            problems.append(Problem("required", field, f"{LABELS[field]}が読み取れていません"))


def _check_amounts(inv: dict, problems: list[Problem]) -> bool:
    """金額がすべて整数で読み取れているか。計算に進んでよければ True。"""
    ok = True
    for field in AMOUNT_FIELDS:
        value = inv.get(field)
        if value is None:
            ok = False
            if field not in REQUIRED:  # 必須項目の空欄は _check_required で報告済み
                problems.append(Problem("missing", field, f"{LABELS[field]}が読み取れていません"))
        elif not _is_int(value):
            ok = False
            problems.append(
                Problem("not_integer", field, f"{LABELS[field]}が整数（円）になっていません: {value!r}")
            )
    return ok


def _check_tax_mode(inv: dict, problems: list[Problem]) -> bool:
    if inv.get("tax_mode") in TAX_MODES:
        return True
    problems.append(Problem("tax_mode", "tax_mode", "税方式（外税か内税か）が読み取れていません"))
    return False


def _check_lines(inv: dict, problems: list[Problem]) -> bool:
    """明細行の金額と税率が使える形か。合計の確認に進んでよければ True。"""
    ok = True
    for i, line in enumerate(inv.get("lines") or [], start=1):
        if not _is_int(line.get("amount")):
            ok = False
            problems.append(
                Problem("line_invalid", "lines", f"明細{i}行目の金額が整数（円）で読み取れていません")
            )
        if line.get("rate") not in RATES:
            ok = False
            problems.append(
                Problem("line_invalid", "lines", f"明細{i}行目の税率が10%でも8%でもありません")
            )
    return ok


def _check_line_sums(inv: dict, problems: list[Problem]) -> None:
    """外税：税率ごとの明細の合計＝税抜の額。内税：明細の合計（税込）＝税抜＋消費税。"""
    inclusive = inv["tax_mode"] == "内税"
    for rate in RATES:
        line_sum = sum(line["amount"] for line in inv.get("lines") or [] if line["rate"] == rate)
        taxable, tax = inv[f"taxable_{rate}"], inv[f"tax_{rate}"]
        expected = taxable + tax if inclusive else taxable
        if line_sum != expected:
            what = "税込の額（税抜＋消費税）" if inclusive else "税抜の額"
            problems.append(
                Problem(
                    "line_sum",
                    f"taxable_{rate}",
                    f"{rate}%の明細の合計 {line_sum:,}円 が{what} {expected:,}円 と合いません",
                )
            )


def _check_tax(inv: dict, problems: list[Problem]) -> None:
    """外税：税抜×税率の切り捨て。内税：税込×税率÷(100＋税率)の切り捨て。1円の差は許す。"""
    inclusive = inv["tax_mode"] == "内税"
    for rate in RATES:
        taxable, tax = inv[f"taxable_{rate}"], inv[f"tax_{rate}"]
        if inclusive:
            expected = (taxable + tax) * rate // (100 + rate)
        else:
            expected = taxable * rate // 100
        if abs(tax - expected) > TOLERANCE:
            problems.append(
                Problem(
                    "tax",
                    f"tax_{rate}",
                    f"{rate}%の消費税 {tax:,}円 が計算した額 {expected:,}円 と合いません",
                )
            )


def _check_total(inv: dict, problems: list[Problem]) -> None:
    computed = (
        inv["taxable_10"] + inv["tax_10"] + inv["taxable_8"] + inv["tax_8"] - inv["withholding"]
    )
    if computed != inv["total"]:
        problems.append(
            Problem(
                "total",
                "total",
                f"記載の請求額 {inv['total']:,}円 が計算した額 {computed:,}円 と"
                f" {inv['total'] - computed:+,}円 ずれています",
            )
        )


def _check_withholding(inv: dict, problems: list[Problem]) -> None:
    withholding = inv["withholding"]
    if withholding == 0:
        return
    expected = inv["taxable_10"] * WITHHOLDING_NUM // WITHHOLDING_DEN
    if abs(withholding - expected) > TOLERANCE:
        problems.append(
            Problem(
                "withholding",
                "withholding",
                f"源泉所得税 {withholding:,}円 が 10%対象の10.21% {expected:,}円 と合いません",
            )
        )


def _check_registration_no(inv: dict, problems: list[Problem]) -> None:
    regno = inv.get("registration_no")
    if not isinstance(regno, str) or not _REGNO.fullmatch(regno):
        problems.append(
            Problem(
                "regno_format",
                "registration_no",
                f"登録番号が「T＋数字13桁」の形になっていません: {regno!r}",
            )
        )
        return
    digits = regno[1:]
    expected = registration_check_digit(digits[1:])
    if int(digits[0]) != expected:
        problems.append(
            Problem(
                "regno_check_digit",
                "registration_no",
                f"登録番号の検査数字（Tの次の1桁）が {digits[0]} ですが、計算では {expected} です",
            )
        )


def _check_dates(inv: dict, problems: list[Problem]) -> None:
    issue = _parse_date(inv, "issue_date", problems)
    due = _parse_date(inv, "due_date", problems)
    if issue and due and due <= issue:
        problems.append(
            Problem(
                "due_before_issue",
                "due_date",
                f"支払期日 {due} が発行日 {issue} より後になっていません",
            )
        )


def _parse_date(inv: dict, field: str, problems: list[Problem]) -> date | None:
    """YYYY-MM-DD の実在する日付なら date を返す。空欄は None（必須かどうかは別で確かめる）。"""
    value = inv.get(field)
    if _is_empty(value):
        return None
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    problems.append(
        Problem("date_invalid", field, f"{LABELS[field]}が実在する YYYY-MM-DD の日付ではありません: {value!r}")
    )
    return None


def _check_duplicate(inv: dict, exists: ExistsFunc | None, problems: list[Problem]) -> None:
    issuer, invoice_no = inv.get("issuer"), inv.get("invoice_no")
    if exists is None or _is_empty(issuer) or _is_empty(invoice_no):
        return
    if exists(issuer, invoice_no):
        problems.append(
            Problem("duplicate", "invoice_no", f"{issuer} の請求書 {invoice_no} は登録済みです")
        )


def _check_handwritten(inv: dict, problems: list[Problem]) -> None:
    if inv.get("is_handwritten") is True:
        problems.append(
            Problem("handwritten", "is_handwritten", "手書きの請求書なので、人が確認します")
        )


# ---------- 小さな道具 ----------


def _is_int(value: object) -> bool:
    # True/False も int の一種なので除く
    return isinstance(value, int) and not isinstance(value, bool)


def _is_empty(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
