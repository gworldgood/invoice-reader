"""請求書読み取りツールの画面の入口。左のメニューで3つのページを切り替える。

    読み取り   請求書のファイルを選んで読み取る（views/read.py）
    要確認     検算で引っかかった請求書を直して承認する（views/review.py）
    登録済み   登録した請求書の一覧と、仕訳CSVのダウンロード（views/registered.py）

ふだんは「請求書ツールを開く.bat」をダブルクリックして開く。直接動かすときは
    streamlit run app/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import open_db  # noqa: E402  (shared が src を読み込めるようにする)

from invoice_reader import db  # noqa: E402

st.set_page_config(page_title="請求書読み取りツール", layout="wide")

st.html(
    """
<style>
/* ---------- 確認画面：要確認の項目に朱の線を引く、金額と登録番号を等幅・右寄せにする ---------- */
[class*="st-key-flag_"] {
  border-left: 3px solid #C63D2F;
  background: #FBEDEA;
  padding: 0.35rem 0.6rem 0.2rem;
  border-radius: 0.25rem;
}
[class*="st-key-amount_"] input,
[class*="st-key-regno_"] input {
  font-family: "BIZ UDGothic", monospace;
  font-variant-numeric: tabular-nums;
}
[class*="st-key-amount_"] input { text-align: right; }
.reason { color: #C63D2F; font-size: 0.85rem; margin: 0.1rem 0 0.4rem; overflow-wrap: anywhere; }
/* Streamlit は文章の部品に下 -16px の余白を付ける。理由の文ではその分が枠からはみ出すので打ち消す */
[data-testid="stMarkdownContainer"]:has(> .reason) { margin-bottom: 0; }

/* ---------- 英語の表示を出さない・日本語にする ---------- */
/* 入力欄の中に出る案内（Press Enter to submit form など）。
   日本語に置き換えても、狭い欄では入力した値に重なって読みにくいので、ボタンの下に1か所だけ説明を出す */
[data-testid="InputInstructions"] { display: none; }

/* 表や画像にマウスを乗せると出るボタン（Download, Search, Fullscreen など） */
[data-testid="stElementToolbar"] { display: none; }

/* 処理中に右上に出る「Running...」と「Stop」。読み取り中は画面に進み具合を出している */
[data-testid="stStatusWidget"] { display: none; }

/* Tabキーで操作したときに出る「Skip to main content」 */
[data-testid="stSkipToContent"] { font-size: 0; }
[data-testid="stSkipToContent"]::after { content: "本文へ移動"; font-size: 0.875rem; }

/* ファイルを置く枠のボタン「Upload」と、横の「200MB per file」 */
[data-testid="stFileUploaderDropzone"] [data-testid="stBaseButton-secondary"] p { font-size: 0; }
[data-testid="stFileUploaderDropzone"] [data-testid="stBaseButton-secondary"] p::after {
  content: "ファイルを選ぶ"; font-size: 0.875rem;
}
[data-testid="stFileUploaderDropzoneInstructions"] span { font-size: 0; }
[data-testid="stFileUploaderDropzoneInstructions"] span::after {
  content: "またはここにドラッグして置く（PDF・JPG・PNG、何枚でもまとめて置けます）"; font-size: 0.875rem;
}
</style>
"""
)

with open_db() as (settings, conn):
    counts = db.count_staging(conn)
waiting = counts.get("review", 0)
registered = counts.get("passed", 0) + counts.get("approved", 0)

pages = [
    st.Page("views/read.py", title="読み取り", icon=":material/upload_file:", default=True),
    st.Page("views/review.py", title=f"要確認（{waiting}件）", icon=":material/fact_check:",
            url_path="review"),
    st.Page("views/registered.py", title=f"登録済み（{registered}件）", icon=":material/inventory_2:",
            url_path="registered"),
]
st.navigation(pages).run()
