"""画面の各ページで共通に使うもの（設定とDBの用意、読み取りのロック、金額の表示）。"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from invoice_reader import db  # noqa: E402
from invoice_reader.config import Settings, ensure_data_dirs, load_settings  # noqa: E402

# 読み取りは同時に1つだけにする。2人が同時に「読み取る」を押しても、同じファイルを取り合わないように。
# このモジュールは画面を開いている全員で共有されるので、ここに置いたロックは全員に効く
READ_LOCK = threading.Lock()


@contextmanager
def open_db() -> Iterator[tuple[Settings, sqlite3.Connection]]:
    """設定を読み、実行時のフォルダとDBを用意して渡す。終わったら接続を閉じる。"""
    settings = load_settings()
    ensure_data_dirs(settings)
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    try:
        yield settings, conn
    finally:
        conn.close()


def yen(value: int | None) -> str:
    return "" if value is None else f"{value:,}円"
