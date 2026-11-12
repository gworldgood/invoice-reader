"""data/inbox/ にある請求書をまとめて処理する。

    python scripts/run_batch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice_reader import db  # noqa: E402
from invoice_reader.config import ensure_data_dirs, load_settings  # noqa: E402
from invoice_reader.llm import LLMError, estimate_usd, make_client, read_invoice  # noqa: E402
from invoice_reader.pipeline import run_inbox  # noqa: E402

LABELS = {"passed": "登録", "review": "要確認", "error": "エラー", "skipped": "処理済み"}


def main() -> int:
    settings = load_settings()
    ensure_data_dirs(settings)
    try:
        client = make_client(settings)  # 設定が足りなければ、ファイルを動かす前に止める
    except LLMError as e:
        print(e)
        return 1

    conn = db.connect(settings.db_path)
    db.init_db(conn)

    def reader(extracted):
        return read_invoice(extracted, settings, client=client)

    try:
        outcomes = run_inbox(settings, conn, reader)
    finally:
        conn.close()

    if not outcomes:
        print(f"inbox にファイルがありません: {settings.folder('inbox')}")
        return 0

    tokens_in = tokens_out = 0
    usd = 0.0
    for o in outcomes:
        where = o.moved_to.relative_to(settings.data_dir).as_posix() if o.moved_to else "-"
        print(f"{LABELS[o.status]:<5} {o.file_name} -> {where}")
        for p in o.problems:
            print(f"        {p['message']}")
        if o.read:
            tokens_in += o.read.input_tokens
            tokens_out += o.read.output_tokens
            usd += estimate_usd(o.read.model, o.read.input_tokens, o.read.output_tokens) or 0.0

    counts = {label: sum(o.status == key for o in outcomes) for key, label in LABELS.items()}
    print()
    print("  ".join(f"{label} {n}件" for label, n in counts.items()))
    print(f"トークン 入力 {tokens_in:,} / 出力 {tokens_out:,}  費用の目安 約{usd:.3f}ドル")
    return 0


if __name__ == "__main__":
    sys.exit(main())
