"""DBへの接続とテーブル定義。

SQLはこのファイルにだけ書く。後でOracleに差し替えるとき、直す場所をここだけにするため。

テーブルの役割（作業書「6. DB」）:
- staging        AIの読み取り結果をそのまま入れる仮テーブル。検算の前はここにしか入れない
- invoices       検算を通った（または人が承認した）請求書だけを入れる本番テーブル
- invoice_lines  明細行。読み取った時点で staging_id で持ち、本番登録時に invoice_id を埋める
- corrections    確認画面で人が直した項目の履歴

SQLiteの STRICT テーブルを使い、金額の列に小数や文字列が入ったらDB側でも拒否する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# 作業書「5. AIから受け取るJSON」の項目（明細 lines を除く）。staging と invoices で同じ列名にする。
INVOICE_FIELDS = (
    "invoice_no",
    "issuer",
    "registration_no",
    "recipient",
    "issue_date",
    "due_date",
    "tax_mode",
    "taxable_10",
    "tax_10",
    "taxable_8",
    "tax_8",
    "withholding",
    "total",
    "is_handwritten",
)

LINE_FIELDS = ("name", "qty", "unit", "price", "amount", "rate")

STAGING_STATUSES = ("pending", "passed", "review", "approved", "error")

# STRICT テーブルに必要なSQLiteの版
_MIN_SQLITE = (3, 37, 0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging (
    id               INTEGER PRIMARY KEY,
    file_name        TEXT    NOT NULL,
    file_hash        TEXT    NOT NULL UNIQUE,   -- ファイル内容のSHA-256。同じファイルは二度処理しない
    invoice_no       TEXT,
    issuer           TEXT,
    registration_no  TEXT,
    recipient        TEXT,
    issue_date       TEXT,                      -- YYYY-MM-DD
    due_date         TEXT,
    tax_mode         TEXT    CHECK (tax_mode IN ('外税', '内税')),
    taxable_10       INTEGER,
    tax_10           INTEGER,
    taxable_8        INTEGER,
    tax_8            INTEGER,
    withholding      INTEGER,
    total            INTEGER,
    is_handwritten   INTEGER CHECK (is_handwritten IN (0, 1)),
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'passed', 'review', 'approved', 'error')),
    errors           TEXT,                      -- 検算で引っかかった理由の一覧（JSON）
    source           TEXT    CHECK (source IN ('text', 'image')),
    stored_path      TEXT,                      -- 原本の今の置き場所（data/ からの相対パス）
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_staging_status ON staging (status);

CREATE TABLE IF NOT EXISTS invoices (
    id               INTEGER PRIMARY KEY,
    staging_id       INTEGER NOT NULL UNIQUE REFERENCES staging (id),
    invoice_no       TEXT    NOT NULL,
    issuer           TEXT    NOT NULL,
    registration_no  TEXT,
    recipient        TEXT,
    issue_date       TEXT    NOT NULL,
    due_date         TEXT,
    tax_mode         TEXT    CHECK (tax_mode IN ('外税', '内税')),
    taxable_10       INTEGER,
    tax_10           INTEGER,
    taxable_8        INTEGER,
    tax_8            INTEGER,
    withholding      INTEGER,
    total            INTEGER NOT NULL,
    is_handwritten   INTEGER CHECK (is_handwritten IN (0, 1)),
    original_path    TEXT    NOT NULL,          -- done/ に移した原本のパス
    approved_by      TEXT    NOT NULL,          -- 人の承認なら担当者名、検算を通った自動登録なら 'auto'
    approved_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (issuer, invoice_no)                 -- 同じ請求書の二重登録を防ぐ
) STRICT;

CREATE TABLE IF NOT EXISTS invoice_lines (
    id          INTEGER PRIMARY KEY,
    staging_id  INTEGER NOT NULL REFERENCES staging (id),
    invoice_id  INTEGER REFERENCES invoices (id),   -- 本番登録までは NULL
    line_no     INTEGER NOT NULL,                   -- 請求書に書かれた順番（1から）
    name        TEXT,
    qty         INTEGER,
    unit        TEXT,
    price       INTEGER,
    amount      INTEGER,                            -- 値引きはマイナス
    rate        INTEGER,                            -- 税率（10 か 8）
    UNIQUE (staging_id, line_no)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON invoice_lines (invoice_id);

CREATE TABLE IF NOT EXISTS corrections (
    id            INTEGER PRIMARY KEY,
    staging_id    INTEGER NOT NULL REFERENCES staging (id),
    field         TEXT    NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    corrected_by  TEXT    NOT NULL,
    corrected_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_corrections_staging ON corrections (staging_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """DBに接続する。ファイルやフォルダがなければ作る。

    外部キーの確認はSQLiteでは接続ごとに有効にする必要があるので、ここで必ず有効にする。
    """
    if sqlite3.sqlite_version_info < _MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} では STRICT テーブルが使えません。"
            "3.37 以上が必要です（Python 3.11 以上に同梱の版なら足ります）。"
        )
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """テーブルがなければ作る。何度呼んでもよい。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def invoice_exists(conn: sqlite3.Connection, issuer: str, invoice_no: str) -> bool:
    """同じ発行元・請求書番号の請求書が本番テーブルにあるか。検算の二重登録の確認で使う。"""
    row = conn.execute(
        "SELECT 1 FROM invoices WHERE issuer = ? AND invoice_no = ?", (issuer, invoice_no)
    ).fetchone()
    return row is not None


# ---------- 書き込み ----------
# ここの関数は commit しない。呼び出し側が `with conn:` で囲み、まとめて確定か取り消しをする。


def find_staging_by_hash(conn: sqlite3.Connection, file_hash: str) -> int | None:
    """同じ内容のファイルを処理済みなら、その staging の id を返す。"""
    row = conn.execute("SELECT id FROM staging WHERE file_hash = ?", (file_hash,)).fetchone()
    return row["id"] if row else None


def insert_staging(
    conn: sqlite3.Connection,
    *,
    file_name: str,
    file_hash: str,
    stored_path: str | None,
    source: str | None = None,
    data: dict | None = None,
    status: str = "pending",
    errors: list[dict] | None = None,
) -> int:
    """AIの読み取り結果を仮テーブルに入れる。明細も staging_id で入れる。"""
    data = data or {}
    columns = ("file_name", "file_hash", "stored_path", "source", "status", "errors") + INVOICE_FIELDS
    values = (file_name, file_hash, stored_path, source, status, _to_json(errors)) + tuple(
        data.get(f) for f in INVOICE_FIELDS
    )
    cur = conn.execute(
        f"INSERT INTO staging ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        values,
    )
    staging_id = cur.lastrowid
    for line_no, line in enumerate(data.get("lines") or [], start=1):
        conn.execute(
            """INSERT INTO invoice_lines (staging_id, line_no, name, qty, unit, price, amount, rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (staging_id, line_no) + tuple(line.get(k) for k in LINE_FIELDS),
        )
    return staging_id


def update_staging(
    conn: sqlite3.Connection,
    staging_id: int,
    *,
    status: str,
    stored_path: str | None,
    errors: list[dict] | None = None,
) -> None:
    conn.execute(
        "UPDATE staging SET status = ?, stored_path = ?, errors = ? WHERE id = ?",
        (status, stored_path, _to_json(errors), staging_id),
    )


def insert_invoice(
    conn: sqlite3.Connection,
    *,
    staging_id: int,
    original_path: str,
    approved_by: str,
) -> int:
    """仮テーブルの1件を本番テーブルに移す。値は staging から写し、明細に invoice_id を書き足す。"""
    fields = ", ".join(INVOICE_FIELDS)
    cur = conn.execute(
        f"""INSERT INTO invoices (staging_id, {fields}, original_path, approved_by)
            SELECT id, {fields}, ?, ? FROM staging WHERE id = ?""",
        (original_path, approved_by, staging_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"staging id {staging_id} がありません")
    invoice_id = cur.lastrowid
    conn.execute(
        "UPDATE invoice_lines SET invoice_id = ? WHERE staging_id = ?", (invoice_id, staging_id)
    )
    return invoice_id


def update_staging_data(conn: sqlite3.Connection, staging_id: int, data: dict) -> None:
    """確認画面で直した値で、仮テーブルの項目と明細を書き換える。"""
    sets = ", ".join(f"{f} = ?" for f in INVOICE_FIELDS)
    conn.execute(
        f"UPDATE staging SET {sets} WHERE id = ?",
        tuple(data.get(f) for f in INVOICE_FIELDS) + (staging_id,),
    )
    conn.execute("DELETE FROM invoice_lines WHERE staging_id = ? AND invoice_id IS NULL", (staging_id,))
    for line_no, line in enumerate(data.get("lines") or [], start=1):
        conn.execute(
            """INSERT INTO invoice_lines (staging_id, line_no, name, qty, unit, price, amount, rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (staging_id, line_no) + tuple(line.get(k) for k in LINE_FIELDS),
        )


def insert_correction(
    conn: sqlite3.Connection,
    *,
    staging_id: int,
    field: str,
    old_value: object,
    new_value: object,
    corrected_by: str,
) -> None:
    conn.execute(
        """INSERT INTO corrections (staging_id, field, old_value, new_value, corrected_by)
           VALUES (?, ?, ?, ?, ?)""",
        (staging_id, field, _to_text(old_value), _to_text(new_value), corrected_by),
    )


# ---------- 読み出し ----------


def list_staging(conn: sqlite3.Connection, status: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, file_name, issuer, invoice_no, total, errors, stored_path, created_at
           FROM staging WHERE status = ? ORDER BY id""",
        (status,),
    ).fetchall()
    return [{**dict(r), "errors": _from_json(r["errors"])} for r in rows]


def count_staging(conn: sqlite3.Connection) -> dict[str, int]:
    """仮テーブルの状態ごとの件数（review: 3 など）。画面の見出しに出す。"""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM staging GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def get_staging(conn: sqlite3.Connection, staging_id: int) -> dict | None:
    """仮テーブルの1件を、作業書5の形の data と、状態などの情報に分けて返す。"""
    row = conn.execute("SELECT * FROM staging WHERE id = ?", (staging_id,)).fetchone()
    if row is None:
        return None
    lines = conn.execute(
        f"SELECT {', '.join(LINE_FIELDS)} FROM invoice_lines WHERE staging_id = ? ORDER BY line_no",
        (staging_id,),
    ).fetchall()
    data = {f: row[f] for f in INVOICE_FIELDS}
    if data["is_handwritten"] is not None:
        data["is_handwritten"] = bool(data["is_handwritten"])
    data["lines"] = [dict(line) for line in lines]
    return {
        "id": row["id"],
        "file_name": row["file_name"],
        "status": row["status"],
        "stored_path": row["stored_path"],
        "source": row["source"],
        "errors": _from_json(row["errors"]),
        "data": data,
    }


def list_corrections(conn: sqlite3.Connection, staging_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM corrections WHERE staging_id = ? ORDER BY id", (staging_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_invoices(conn: sqlite3.Connection) -> list[dict]:
    """本番テーブルの請求書を、明細なしで発行日順に返す。仕訳CSVで使う。"""
    rows = conn.execute("SELECT * FROM invoices ORDER BY issue_date, id").fetchall()
    return [dict(r) for r in rows]


def _to_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _from_json(value: str | None) -> list[dict]:
    return json.loads(value) if value else []


def _to_json(value: list[dict] | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)
