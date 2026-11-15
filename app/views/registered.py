"""登録済みのページ。登録した請求書の一覧と、会計ソフトに取り込む仕訳CSVのダウンロード。"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from shared import open_db, yen

from invoice_reader import db
from invoice_reader.export import JournalError, journal_csv
from invoice_reader.pipeline import AUTO_APPROVER

ALL = "すべて"


def main() -> None:
    with open_db() as (settings, conn):
        render(conn)


def render(conn) -> None:
    st.subheader("登録済みの請求書", anchor=False)
    invoices = db.list_invoices(conn)
    if not invoices:
        st.info("登録済みの請求書はまだありません。「読み取り」のページから請求書を読み取ると、ここに並びます。")
        return

    months = sorted({inv["issue_date"][:7] for inv in invoices}, reverse=True)
    left, _ = st.columns([1, 2])
    with left:
        month = st.selectbox("発行月", [ALL, *months], format_func=_month_label)
    selected = [inv for inv in invoices if month == ALL or inv["issue_date"].startswith(month)]

    a, b, _ = st.columns([1, 2, 3])  # 合計の金額は桁が多いので広めにする（狭いと「304,79…」と切れる）
    a.metric("件数", f"{len(selected)}件")
    b.metric("請求額の合計", yen(sum(inv["total"] for inv in selected)))

    st.dataframe(
        pd.DataFrame(
            {
                "発行日": [inv["issue_date"] for inv in reversed(selected)],
                "発行元": [inv["issuer"] for inv in reversed(selected)],
                "請求書番号": [inv["invoice_no"] for inv in reversed(selected)],
                "請求額": [yen(inv["total"]) for inv in reversed(selected)],
                "支払期日": [inv["due_date"] or "" for inv in reversed(selected)],
                "登録": [_how(inv["approved_by"]) for inv in reversed(selected)],
            }
        ),
        hide_index=True,
        width="stretch",
    )

    try:
        csv = journal_csv(selected)
    except JournalError as e:
        st.error(f"仕訳を作れませんでした。管理者に連絡してください。（{e}）")
        return
    name = "仕訳_すべて.csv" if month == ALL else f"仕訳_{month.replace('-', '')}.csv"
    st.download_button(
        f"仕訳CSVをダウンロード（{_month_label(month)}・{len(selected)}件）",
        data=csv,
        file_name=name,
        mime="text/csv",
        type="primary",
        on_click="ignore",
    )
    st.caption("会計ソフトに取り込める形の仕訳です（借方：費用・仮払消費税、貸方：買掛金・預り金）。Excelでも開けます。")


def _month_label(month: str) -> str:
    return month if month == ALL else f"{int(month[:4])}年{int(month[5:])}月"


def _how(approved_by: str) -> str:
    return "自動（検算が通過）" if approved_by == AUTO_APPROVER else f"{approved_by} さんが承認"


main()
