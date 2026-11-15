# 請求書読み取りツール

請求書（PDF・画像）をAIで読み取り、検算を通ったものだけDBに登録し、怪しいものは人が確認画面で直して承認するツール。業務システムにAIを組み込む例として作る。

詳しい仕様と作業の順番は `docs/作業書.md` にある。作業を始める前に必ず読むこと。

## 技術

- Python 3.11以上
- AIの読み取り：Anthropic API（`anthropic`）。モデル名は `.env` の `MODEL` から読む
- PDF：`pdfplumber`（文字の取り出し）、`pymupdf`（画像への変換）
- DB：SQLite（標準の `sqlite3`）。接続部分は後でOracleに差し替えられるよう `db.py` にまとめる
- 確認画面：Streamlit
- テスト：pytest

## フォルダ

```
src/invoice_reader/   本体（extract, llm, validate, db, pipeline, export）
app/main.py           画面の入口（Streamlit）。ページは app/views/（読み取り、要確認、登録済み）
scripts/              open_app.py（画面を開く）、run_batch.py（一括処理）、evaluate.py（精度測定）
請求書ツールを開く.bat  使う人がダブルクリックで画面を開く
tests/                テスト
sample_data/          架空の請求書30枚と正解データ（変更しない）
data/                 実行時のフォルダとDB（Gitに上げない）
docs/作業書.md        仕様と作業の順番
```

## コマンド

```
pip install -r requirements.txt
pytest
python scripts/run_batch.py
python scripts/evaluate.py
python scripts/export_journal.py
streamlit run app/main.py
```

## 守ること

- APIキーをコードに書かない。`.env` から読み、`.env` はコミットしない
- 請求書の実物をリポジトリに入れない。使うのは `sample_data/` の架空データだけ
- `sample_data/` の中身は書き換えない。精度測定の基準なので
- 検算（足し算、桁数、日付の確認）は必ず普通のプログラムで行う。AIに計算の正しさを判定させない
- AIの読み取り結果は、仮テーブルに入れてから検算する。本番テーブルに直接入れない
- 金額は整数（円）で扱う。小数や浮動小数点を使わない
- Jevを組み込むときは、APIの仕様を https://docs.typesafe.ai で確認する。推測で書かない

## 作業の進め方

- `docs/作業書.md` のフェーズを上から順に進める。1つのフェーズが終わったら止まって報告する
- 各フェーズの終わりに、何を作ったか、なぜその作りにしたかを短く説明する。あとから本人が設計の理由を説明できるようにするため
- 検算のルールを変えるときは、先にテストを書く
- APIを呼ぶ処理を試すときは、まず1枚で動かす。30枚を回すのは動作を確かめてから
- 絵文字は使わない（コード、コメント、画面、README、返答のすべて）
- 確認画面を作る前に、配色・書体・レイアウトの方向を短く提示し、確認を取ってから実装する。参考にする実在のサービスを具体的に挙げること。生成りの背景にテラコッタ、黒背景に蛍光色1色、紫のグラデーション、Interやシステムフォントといった「いかにもAI」な見た目は避ける
