"""本番テーブルの請求書から仕訳CSVを書き出す。

    python scripts/export_journal.py                 # data/journal.csv に書き出す
    python scripts/export_journal.py 出力先.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice_reader import db  # noqa: E402
from invoice_reader.config import load_settings  # noqa: E402
from invoice_reader.export import JournalError, export_csv  # noqa: E402


def main() -> int:
    settings = load_settings()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.data_dir / "journal.csv"
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    try:
        n = export_csv(conn, out)
    except JournalError as e:
        print(e)
        return 1
    finally:
        conn.close()
    print(f"{n}枚の請求書の仕訳を書き出しました: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
