"""確認画面の裏側の処理。画面の要確認のページ（app/views/review.py）から呼ぶ。作業書「8. 確認画面」。

- 要確認の一覧を出す
- 人が直した値を仮テーブルに書き戻し、直した項目を corrections に記録する
- 直した値で検算をやり直す
- 検算が通ったものだけ承認でき、本番テーブルへ移して原本を done/ に移す

画面の部品（Streamlit）とは分けてあるので、ここは普通のテストで確かめられる。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

from invoice_reader import db
from invoice_reader.config import Settings
from invoice_reader.db import INVOICE_FIELDS, LINE_FIELDS
from invoice_reader.pipeline import done_name, unique_path
from invoice_reader.validate import Problem, validate


class ReviewError(Exception):
    """承認できない（検算が通っていない、もう承認済み、原本がない）。"""


def review_list(conn: sqlite3.Connection) -> list[dict]:
    return db.list_staging(conn, "review")


def load(conn: sqlite3.Connection, staging_id: int) -> dict:
    item = db.get_staging(conn, staging_id)
    if item is None:
        raise ReviewError("この請求書は見つかりませんでした。画面を読み込み直してください。")
    return item


def check(conn: sqlite3.Connection, data: dict) -> list[Problem]:
    """検算（作業書7）をやり直す。二重登録の確認も含む。"""
    return validate(data, exists=lambda issuer, no: db.invoice_exists(conn, issuer, no))


def changes(old: dict, new: dict) -> list[tuple[str, object, object]]:
    """直した項目の一覧（項目名, 元の値, 新しい値）。明細は lines[2].amount のように行番号付きで。"""
    result = [(f, old.get(f), new.get(f)) for f in INVOICE_FIELDS if old.get(f) != new.get(f)]
    old_lines, new_lines = old.get("lines") or [], new.get("lines") or []
    for i in range(max(len(old_lines), len(new_lines))):
        before = old_lines[i] if i < len(old_lines) else None
        after = new_lines[i] if i < len(new_lines) else None
        if before is None or after is None:
            result.append((f"lines[{i + 1}]", _line_text(before), _line_text(after)))
            continue
        for k in LINE_FIELDS:
            if before.get(k) != after.get(k):
                result.append((f"lines[{i + 1}].{k}", before.get(k), after.get(k)))
    return result


def save_and_check(
    conn: sqlite3.Connection, staging_id: int, new_data: dict, corrected_by: str
) -> list[Problem]:
    """直した値を保存し、直した項目を記録してから検算をやり直す。理由の一覧を返す。"""
    item = load(conn, staging_id)
    if item["status"] != "review":
        raise ReviewError("この請求書はすでに確認が終わっているため、直せません")
    if not corrected_by.strip():
        raise ReviewError("確認者の名前を入れてください")

    problems = check(conn, new_data)
    with conn:
        for field, old_value, new_value in changes(item["data"], new_data):
            db.insert_correction(
                conn,
                staging_id=staging_id,
                field=field,
                old_value=old_value,
                new_value=new_value,
                corrected_by=corrected_by,
            )
        db.update_staging_data(conn, staging_id, new_data)
        db.update_staging(
            conn,
            staging_id,
            status="review",
            stored_path=item["stored_path"],
            errors=[asdict(p) for p in problems],
        )
    return problems


def approve(conn: sqlite3.Connection, settings: Settings, staging_id: int, approved_by: str) -> Path:
    """検算が通っていれば本番テーブルへ移し、原本を done/ に移す。移した先のパスを返す。"""
    item = load(conn, staging_id)
    if item["status"] != "review":
        raise ReviewError("この請求書はすでに確認が終わっているため、承認できません")
    if not approved_by.strip():
        raise ReviewError("確認者の名前を入れてください")
    problems = check(conn, item["data"])
    if problems:
        raise ReviewError("検算が通っていないので承認できません: " + " / ".join(p.message for p in problems))

    src = settings.data_dir / item["stored_path"]
    if not src.exists():
        # 画面に出る文なので、フォルダの場所は出さずにファイル名だけを出す
        raise ReviewError(f"原本のファイル（{item['file_name']}）が見つかりません")
    dest = unique_path(settings.folder("done") / done_name(item["data"], src.suffix))
    rel = dest.relative_to(settings.data_dir).as_posix()
    with conn:  # 本番登録と原本の移動を一緒に行う（pipeline と同じ考え方）
        db.insert_invoice(conn, staging_id=staging_id, original_path=rel, approved_by=approved_by)
        db.update_staging(conn, staging_id, status="approved", stored_path=rel)
        src.rename(dest)
    return dest


def _line_text(line: dict | None) -> str | None:
    if line is None:
        return None
    return " ".join("" if line.get(k) is None else str(line.get(k)) for k in LINE_FIELDS)
