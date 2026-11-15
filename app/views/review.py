"""要確認のページ。検算で引っかかった請求書を、人が原本を見ながら直して承認する。

画面の部品だけをここに書き、処理（保存、記録、検算、承認）は src/invoice_reader/review.py に任せる。
見た目（朱の線、等幅の金額など）の CSS は app/main.py にまとめてある。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
from shared import open_db, yen

from invoice_reader import db
from invoice_reader.extract import ExtractError, pdf_to_images
from invoice_reader.review import ReviewError, approve, field_label, load, review_list, save_and_check
from invoice_reader.validate import LABELS

AMOUNTS = ("taxable_10", "tax_10", "taxable_8", "tax_8", "withholding", "total")


@st.cache_data(show_spinner=False)
def page_images(path: str, mtime: float) -> list[bytes]:
    """原本を画像にする。PDFはページごとにPNGへ、画像はそのまま。mtime はキャッシュの区別用。"""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return [img.data for img in pdf_to_images(p)]
    return [p.read_bytes()]


def main() -> None:
    with open_db() as (settings, conn):
        render(settings, conn)


def render(settings, conn) -> None:
    with st.sidebar:
        st.markdown("#### 確認者")
        reviewer = st.text_input("確認者の名前", key="reviewer", label_visibility="collapsed",
                                 placeholder="名前を入れてから直す・承認する")

    st.subheader("要確認の請求書", anchor=False)
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))

    items = review_list(conn)
    if not items:
        st.info("要確認の請求書はありません。新しい請求書は「読み取り」のページから読み取れます。")
        return

    # 一覧は見るだけの表にし、開く請求書は下の選択欄で選ぶ（表の左端のチェック欄は役割がわかりにくいため）
    table = pd.DataFrame(
        {
            "ファイル": [i["file_name"] for i in items],
            "発行元": [i["issuer"] or "" for i in items],
            "請求額": [yen(i["total"]) for i in items],
            "引っかかった理由": [" / ".join(e["message"] for e in i["errors"]) for i in items],
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")

    labels = {i["id"]: f"{i['file_name']}　{i['issuer'] or '発行元不明'}" for i in items}
    staging_id = st.selectbox(
        "確認する請求書", options=list(labels), format_func=labels.get, key="selected_invoice"
    )
    detail(settings, conn, staging_id, reviewer)


def detail(settings, conn, sid: int, reviewer: str) -> None:
    item = load(conn, sid)
    data = item["data"]
    flagged: dict[str, list[str]] = {}
    for e in item["errors"]:
        flagged.setdefault(e.get("field") or "_", []).append(e["message"])

    st.divider()
    # 左に原本、右に読み取った値。明細とボタンはその下に全幅で置く（Bill One の照合画面に近い並び）
    with st.form(key=f"form_{sid}", border=False):
        new = {}
        left, right = st.columns([1, 1], gap="large")

        with left:
            st.markdown(f"**{item['file_name']}**")
            path = settings.data_dir / item["stored_path"]
            try:
                for png in page_images(str(path), path.stat().st_mtime):
                    st.image(png, width="stretch")
            except (OSError, ExtractError) as e:
                st.error("原本の画像を表示できません。ファイルが移動・削除されていないか確認してください。")

        with right:
            if item["errors"]:
                st.markdown("**引っかかった理由**")
                for e in item["errors"]:
                    st.markdown(f'<div class="reason">{e["message"]}</div>', unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            with c1:
                new["invoice_no"] = text_field("invoice_no", data, flagged, sid)
                new["registration_no"] = text_field("registration_no", data, flagged, sid, key_prefix="regno")
                new["issue_date"] = text_field("issue_date", data, flagged, sid, help="YYYY-MM-DD")
            with c2:
                new["issuer"] = text_field("issuer", data, flagged, sid)
                new["recipient"] = text_field("recipient", data, flagged, sid, label="宛先")
                new["due_date"] = text_field("due_date", data, flagged, sid, help="YYYY-MM-DD")

            with field_box("tax_mode", flagged, sid):
                options = ["外税", "内税"]
                new["tax_mode"] = st.radio(
                    LABELS["tax_mode"], options, horizontal=True, key=f"{sid}_tax_mode",
                    index=options.index(data["tax_mode"]) if data["tax_mode"] in options else None,
                )
                reasons(flagged, "tax_mode")

            a1, a2 = st.columns(2)
            for i, f in enumerate(AMOUNTS):
                with (a1 if i % 2 == 0 else a2):
                    new[f] = amount_field(f, data, flagged, sid)

            new["is_handwritten"] = st.checkbox(
                "手書きの請求書", value=bool(data["is_handwritten"]), key=f"{sid}_is_handwritten"
            )

        with field_box("lines", flagged, sid):
            st.markdown("明細")
            st.caption("行を足すときは、表の一番下の空いた行に入力します。行を消すときは、行の左端を押して選び、Deleteキーを押します。")
            new["lines"] = lines_editor(data["lines"], sid)
            reasons(flagged, "lines")

        _, button_area = st.columns([1, 1], gap="large")
        with button_area:
            submitted = st.form_submit_button("直した値で検算する", width="stretch")
            st.caption("入力欄でEnterキーを押しても検算します。")

    _, right = st.columns([1, 1], gap="large")
    with right:
        if submitted:
            try:
                problems = save_and_check(conn, sid, new, reviewer)
            except ReviewError as e:
                st.error(str(e))
            else:
                st.session_state[f"checked_{sid}"] = not problems
                st.rerun()

        checked = st.session_state.get(f"checked_{sid}", False) and not item["errors"]
        if checked:
            st.success("検算が通りました。「承認して登録する」を押すと登録が確定します。")
        if st.button("承認して登録する", type="primary", disabled=not checked,
                     width="stretch", key=f"approve_{sid}"):
            try:
                dest = approve(conn, settings, sid, reviewer)
            except ReviewError as e:
                st.error(str(e))
            else:
                st.session_state["notice"] = f"{item['file_name']} を登録しました。"
                st.rerun()
        if not checked:
            st.caption("値を直して「直した値で検算する」を押し、検算が通ると承認できます。")

    # 履歴は画面の幅いっぱいに出す（右半分に置くと、登録番号のような長い値で他の列が1文字ずつに潰れる）
    history = db.list_corrections(conn, sid)
    if history:
        with st.expander(f"修正の履歴（{len(history)}件）"):
            # 項目名は日本語にし、長い値も切れないよう全文を出す表（st.table）にする
            st.table(
                pd.DataFrame(
                    {
                        "項目": [field_label(h["field"]) for h in history],
                        "元の値": [_shown(h["old_value"]) for h in history],
                        "直した値": [_shown(h["new_value"]) for h in history],
                        "確認者": [h["corrected_by"] for h in history],
                        "日時": [h["corrected_at"] for h in history],
                    }
                ).set_index("項目")
            )


def _shown(value) -> str:
    return "（空欄）" if value is None or value == "" else str(value)


# ---------- 入力欄 ----------


def field_box(field: str, flagged: dict, sid: int):
    """引っかかった項目なら朱の線付きの枠、そうでなければ普通の枠。"""
    kind = "flag" if field in flagged else "plain"
    return st.container(key=f"{kind}_{field}_{sid}")


def reasons(flagged: dict, field: str) -> None:
    for message in flagged.get(field, []):
        st.markdown(f'<div class="reason">{message}</div>', unsafe_allow_html=True)


def text_field(field, data, flagged, sid, label=None, help=None, key_prefix="text") -> str | None:
    with field_box(field, flagged, sid):
        value = st.text_input(label or LABELS[field], value=data[field] or "", help=help,
                              key=f"{key_prefix}_{sid}_{field}")
        reasons(flagged, field)
    value = value.strip()
    return value or None


def amount_field(field, data, flagged, sid) -> int | None:
    with field_box(field, flagged, sid):
        value = st.number_input(LABELS[field] + "（円）", value=data[field], step=1, format="%d",
                                key=f"amount_{sid}_{field}")
        reasons(flagged, field)
    return None if value is None else int(value)


def lines_editor(lines: list[dict], sid: int) -> list[dict]:
    columns = ["name", "qty", "unit", "price", "amount", "rate"]
    df = pd.DataFrame(lines, columns=columns)
    edited = st.data_editor(
        df,
        key=f"lines_{sid}",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("品名", width="medium"),
            "qty": st.column_config.NumberColumn("数量", step=1, format="%d", width="small"),
            "unit": st.column_config.TextColumn("単位", width="small"),
            "price": st.column_config.NumberColumn("単価", step=1, format="%d", width="small"),
            "amount": st.column_config.NumberColumn("金額", step=1, format="%d", width="small"),
            "rate": st.column_config.SelectboxColumn("税率", options=[10, 8], width="small"),
        },
    )
    result = []
    for row in edited.to_dict("records"):
        if all(_empty(row.get(c)) for c in columns):
            continue  # 何も入っていない行は捨てる
        result.append({c: _clean(c, row.get(c)) for c in columns})
    return result


def _empty(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or value == ""


def _clean(column: str, value):
    """表から戻ってきた値を、作業書5の形（金額は整数、空は None）に戻す。"""
    if _empty(value):
        return None
    if column in ("name", "unit"):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(value) if isinstance(value, (int,)) else value


main()
