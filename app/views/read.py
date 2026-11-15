"""読み取りのページ。請求書のファイルを受け取り、AIで読み取って検算し、登録か要確認に振り分ける。

ファイルはいったん inbox/ に置き、pipeline.process_file で1枚ずつ処理する（scripts/run_batch.py と同じ流れ）。
ここに書くのは画面の部品と、進み具合・結果の見せ方だけ。
"""

from __future__ import annotations

import logging

import pandas as pd
import streamlit as st
from shared import READ_LOCK, open_db, yen

from invoice_reader import db
from invoice_reader.llm import LLMError, describe_api_error, make_client, read_invoice
from invoice_reader.pipeline import (
    IntakeError,
    inbox_files,
    process_file,
    recover_processing,
    save_to_inbox,
)

log = logging.getLogger(__name__)

RESULT = {"passed": "登録しました", "review": "要確認", "error": "読めませんでした", "skipped": "読み取り済み"}


def main() -> None:
    with open_db() as (settings, conn):
        render(settings, conn)


def render(settings, conn) -> None:
    st.subheader("請求書の読み取り", anchor=False)
    st.caption(
        "請求書のファイルを置いて「読み取る」を押すと、AIが読み取り、金額や登録番号を検算します。"
        "検算が通ったものはそのまま登録され、合わないものは「要確認」に回ります。1枚あたり5秒ほどかかります。"
    )
    show_last_result()

    if settings.missing_for_api():
        st.error("AIの読み取りの設定（APIキー）がまだされていません。管理者に連絡してください。")

    reading = st.session_state.get("reading", False)
    uploads = st.file_uploader(
        "請求書のファイル",
        accept_multiple_files=True,
        key=f"uploads_{st.session_state.get('upload_round', 0)}",
        disabled=reading,
    )

    waiting = inbox_files(settings)
    if waiting and not reading:
        names = "、".join(p.name for p in waiting[:3]) + (" ほか" if len(waiting) > 3 else "")
        st.info(f"読み取り待ちのファイルが {len(waiting)} 件あります（{names}）。「読み取る」を押すと一緒に読み取ります。")

    st.button(
        "読み取る",
        type="primary",
        on_click=_start,
        disabled=reading or bool(settings.missing_for_api()) or not (uploads or waiting),
    )

    if reading:
        try:
            st.session_state["last_result"] = run_reading(settings, conn, uploads or [])
        finally:
            # 途中で別のページに移ったときも、ボタンが押せないままにならないように戻す
            st.session_state["reading"] = False
        st.session_state["upload_round"] = st.session_state.get("upload_round", 0) + 1  # 置いたファイルを空にする
        st.rerun()

    unreadable_section(conn)


def _start() -> None:
    st.session_state["reading"] = True
    st.session_state.pop("last_result", None)


def run_reading(settings, conn, uploads) -> dict:
    """置かれたファイルを inbox/ に入れ、読み取り待ちのファイルを1枚ずつ処理する。結果をまとめて返す。"""
    if not READ_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        rejected = []
        # 一度 inbox に入れたファイルは、途中で止まって押し直したときに二重に入れない
        saved = st.session_state.setdefault("saved_uploads", set())
        for f in uploads:
            if f.file_id in saved:
                continue
            try:
                save_to_inbox(settings, f.name, f.getvalue())
            except IntakeError as e:
                rejected.append(str(e))
            saved.add(f.file_id)

        recover_processing(settings)  # 前に途中で止まって processing/ に残ったものを読み取り待ちに戻す
        targets = inbox_files(settings)
        if not targets:
            return {"rejected": rejected, "outcomes": []}

        try:
            client = make_client(settings)
        except LLMError:
            return {"rejected": rejected, "outcomes": [],
                    "stopped": "AIの読み取りの設定（APIキー）がまだされていません。管理者に連絡してください。"}

        def reader(extracted):
            return read_invoice(extracted, settings, client=client)

        outcomes, stopped = [], None
        progress = st.progress(0.0)
        for i, path in enumerate(targets):
            progress.progress(i / len(targets), text=f"{path.name} を読み取っています（{i + 1} / {len(targets)} 枚目）")
            if not path.exists():
                continue
            try:
                outcomes.append(_summary(process_file(path, settings, conn, reader)))
            except Exception as e:  # APIの失敗など。ファイルは読み取り待ちに戻っている
                log.exception("読み取りの途中で止まりました: %s", path.name)
                stopped = describe_api_error(e)
                break
        progress.empty()
        return {"rejected": rejected, "outcomes": outcomes, "stopped": stopped,
                "left": len(inbox_files(settings)) if stopped else 0}
    finally:
        READ_LOCK.release()


def _summary(o) -> dict:
    data = o.read.data if o.read else {}
    return {
        "status": o.status,
        "ファイル": o.file_name,
        "結果": RESULT[o.status],
        "発行元": data.get("issuer") or "",
        "請求額": yen(data.get("total")),
        "内容": " / ".join(p["message"] for p in o.problems),
    }


def show_last_result() -> None:
    result = st.session_state.get("last_result")
    if not result:
        return
    if result.get("busy"):
        st.warning("ほかの人が読み取り中です。終わってから、もう一度「読み取る」を押してください。")
        return
    for message in result.get("rejected", []):
        st.error(message)
    if result.get("stopped"):
        st.error(f"{result['stopped']}（読み取っていない {result['left']} 件は、読み取り待ちに残っています）")

    outcomes = result.get("outcomes", [])
    if not outcomes:
        return
    counts = {key: sum(o["status"] == key for o in outcomes) for key in RESULT}
    st.success(f"{len(outcomes)} 枚を読み取りました。")
    cols = st.columns(4)
    for col, key in zip(cols, RESULT):
        col.metric(RESULT[key], f"{counts[key]}件")
    # 理由の文が長くても途中で切れないよう、折り返して全文を出す表（st.table）にする
    st.table(pd.DataFrame(outcomes).drop(columns="status").set_index("ファイル"))
    if counts["review"]:
        st.page_link("views/review.py", label="要確認の請求書を直す", icon=":material/arrow_forward:")


def unreadable_section(conn) -> None:
    items = db.list_staging(conn, "error")
    if not items:
        return
    with st.expander(f"読めなかったファイル（{len(items)}件）"):
        st.caption(
            "ファイルが壊れている、請求書ではないなどの理由で読めなかったものです。"
            "スキャンし直したファイルや、発行元から送り直してもらったファイルで、もう一度読み取ってください。"
        )
        st.table(
            pd.DataFrame(
                {
                    "ファイル": [i["file_name"] for i in items],
                    "理由": [" / ".join(e["message"] for e in i["errors"]) for i in items],
                    "日時": [i["created_at"] for i in items],
                }
            ).set_index("ファイル")
        )


main()
