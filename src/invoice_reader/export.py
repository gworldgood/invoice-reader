"""本番テーブルの請求書から、仕訳のCSVを作る。作業書「10. フェーズ7」。

1枚の請求書を1つの伝票（複合仕訳）にする。
    借方：費用の勘定科目（税率ごと、税抜）と仮払消費税（税率ごと）
    貸方：買掛金（請求額）と、源泉所得税があれば預り金
借方の合計＝税抜＋消費税、貸方の合計＝請求額＋源泉所得税 で、検算を通った請求書なら必ず一致する。

費用の勘定科目は、いまは決め打ち（源泉所得税ありなら「支払報酬料」、なしなら「仕入高」）。
請求書の中身から科目を選ぶのは、作業書8の任意項目（Jev）で置き換える想定。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from invoice_reader import db

HEADER = ["伝票番号", "日付", "借方勘定科目", "借方税区分", "借方金額", "貸方勘定科目", "貸方金額", "摘要"]

TAX_CLASS = {10: "課税仕入10%", 8: "課税仕入8%（軽）"}


class JournalError(Exception):
    """借方と貸方が合わない（検算を通っていないデータが混ざっている）。"""


def expense_account(invoice: dict) -> str:
    return "支払報酬料" if invoice["withholding"] else "仕入高"


def journal_rows(invoice: dict, slip_no: int) -> list[list]:
    """1枚の請求書の仕訳行。金額が0の行は出さない。"""
    memo = f"{invoice['issuer']} {invoice['invoice_no']}"
    date = invoice["issue_date"]
    rows = []
    for rate in (10, 8):
        if invoice[f"taxable_{rate}"]:
            rows.append([slip_no, date, expense_account(invoice), TAX_CLASS[rate],
                         invoice[f"taxable_{rate}"], "", "", memo])
        if invoice[f"tax_{rate}"]:
            rows.append([slip_no, date, "仮払消費税", TAX_CLASS[rate],
                         invoice[f"tax_{rate}"], "", "", memo])
    rows.append([slip_no, date, "", "", "", "買掛金", invoice["total"], memo])
    if invoice["withholding"]:
        rows.append([slip_no, date, "", "", "", "預り金", invoice["withholding"], memo])

    debit = sum(r[4] for r in rows if r[4] != "")
    credit = sum(r[6] for r in rows if r[6] != "")
    if debit != credit:
        raise JournalError(f"{memo}: 借方 {debit:,}円 と貸方 {credit:,}円 が合いません")
    return rows


def export_csv(conn: sqlite3.Connection, path: Path) -> int:
    """本番テーブルの請求書をすべてCSVに書き出す。書き出した請求書の枚数を返す。

    Excel で文字化けしないよう、BOM付きの UTF-8 で書く。
    """
    invoices = db.list_invoices(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for inv in invoices:
            writer.writerows(journal_rows(inv, slip_no=inv["id"]))
    return len(invoices)
