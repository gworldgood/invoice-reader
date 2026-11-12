"""sample_data の30枚をAIに読ませ、正解データと項目ごとに照合して docs/精度.md に表で残す。

    python scripts/evaluate.py                       # APIを呼んで測る（文字24回＋画像30回）
    python scripts/evaluate.py --rescore 結果.json    # 保存した結果から表だけ作り直す（APIを呼ばない）

経路は2つに分けて測る（作業書9、11）。
- 文字：PDF 24枚から文字を取り出して渡す
- 画像：PDF 24枚は画像に変換して渡す。スキャン風の6枚（JPG）はそのまま渡す
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invoice_reader.config import PROJECT_ROOT, load_settings  # noqa: E402
from invoice_reader.extract import ExtractError, extract  # noqa: E402
from invoice_reader.ground_truth import EXPECTED_STOP, Sample, load_samples  # noqa: E402
from invoice_reader.llm import (  # noqa: E402
    DEFAULT_EFFORT,
    LLMError,
    estimate_usd,
    make_client,
    normalize_name,
    read_invoice,
)
from invoice_reader.validate import validate  # noqa: E402

# 作業書11の項目。「税額」は10%と8%の消費税が両方合っていれば一致とする
FIELDS = [
    ("total", "請求額"),
    ("registration_no", "登録番号"),
    ("issue_date", "発行日"),
    ("due_date", "支払期日"),
    ("invoice_no", "請求書番号"),
    ("issuer", "発行元"),
    ("tax", "税額"),
]

REPORT = PROJECT_ROOT / "docs" / "精度.md"
RESULTS_DIR = PROJECT_ROOT / "data" / "eval"


# ---------- 読ませる ----------


def run(effort: str, workers: int) -> dict:
    settings = load_settings()
    client = make_client(settings)
    samples = load_samples()

    jobs = []
    for s in samples:
        if not s.scan:
            jobs.append((s, "text"))
        jobs.append((s, "image"))

    def one(job: tuple[Sample, str]) -> dict:
        sample, route = job
        record = {"file": sample.file, "route": route, "data": None, "error": None}
        started = time.perf_counter()
        try:
            extracted = extract(sample.path, mode="auto" if route == "text" else "image")
            r = read_invoice(extracted, settings, client=client, effort=effort)
        except (ExtractError, LLMError) as e:
            record["error"] = str(e)
            record["seconds"] = time.perf_counter() - started
            return record
        record.update(
            data=r.data,
            seconds=r.seconds,
            model=r.model,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            usd=estimate_usd(r.model, r.input_tokens, r.output_tokens),
        )
        print(f"  {sample.file} {route:<5} {r.seconds:5.1f}秒", flush=True)
        return record

    print(f"{len(jobs)}回読ませます（並列{workers}）")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(one, jobs))

    return {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "model": settings.model,
        "effort": effort,
        "records": records,
    }


# ---------- 照合する ----------


def score(results: dict) -> list[dict]:
    truth = {s.file: s for s in load_samples()}
    rows = []
    for rec in results["records"]:
        s = truth[rec["file"]]
        data = rec["data"]
        row = {
            **rec,
            "layout": s.layout,
            "scan": s.scan,
            "has_error": s.file in EXPECTED_STOP,
        }
        if data is None:
            row["match"] = {key: False for key, _ in FIELDS}
            row["codes"] = []
        else:
            row["match"] = {key: _same(key, data, s.invoice) for key, _ in FIELDS}
            row["codes"] = sorted({p.code for p in validate(data)})
        row["all_match"] = all(row["match"].values())
        row["stopped"] = data is None or bool(row["codes"])
        row["right_reason"] = s.file in EXPECTED_STOP and EXPECTED_STOP[s.file] <= set(row["codes"])
        rows.append(row)
    return rows


def _same(key: str, data: dict, truth: dict) -> bool:
    if key == "tax":
        return data["tax_10"] == truth["tax_10"] and data["tax_8"] == truth["tax_8"]
    if key == "issuer":
        # 読み取った名前は空白を半角1つにそろえてからDBに入れるので、正解も同じようにそろえて比べる
        return normalize_name(data[key]) == normalize_name(truth[key])
    return data[key] == truth[key]


# ---------- 表にする ----------


def build_report(results: dict, rows: list[dict]) -> str:
    groups = {
        "文字（PDF 24枚）": [r for r in rows if r["route"] == "text"],
        "画像（PDF 24枚を変換）": [r for r in rows if r["route"] == "image" and not r["scan"]],
        "画像（スキャン風 6枚）": [r for r in rows if r["route"] == "image" and r["scan"]],
        "画像（30枚すべて）": [r for r in rows if r["route"] == "image"],
    }
    names = list(groups)
    out = []
    usd_total = sum(r.get("usd") or 0 for r in rows)
    out += [
        "# 精度",
        "",
        f"測定日時 {results['run_at']}　モデル {results['model']}　思考の深さ {results['effort']}",
        f"読ませた回数 {len(rows)}回　費用の目安の合計 約{usd_total:.2f}ドル",
        "",
        "`python scripts/evaluate.py` で測り直せる。正解は `sample_data/ground_truth.json`。",
        "発行元は、空白の全角・半角の違いを区別せずに比べる（DBに入れる前に半角1つにそろえるため）。",
        "",
        "## 項目ごとの一致率",
        "",
        "| 項目 | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for key, label in FIELDS:
        cells = [_rate(sum(r["match"][key] for r in g), len(g)) for g in groups.values()]
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    cells = [_rate(sum(r["all_match"] for r in g), len(g)) for g in groups.values()]
    out.append("| **7項目すべて一致** | " + " | ".join(cells) + " |")

    out += ["", "## 検算の結果", "", "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    cells = []
    for g in groups.values():
        bad = [r for r in g if r["has_error"]]
        cells.append(
            f"{sum(r['stopped'] for r in bad)} / {len(bad)}（正しい理由 {sum(r['right_reason'] for r in bad)}）"
        )
    out.append("| 誤りを入れた請求書を止めた | " + " | ".join(cells) + " |")
    cells = []
    for g in groups.values():
        good = [r for r in g if not r["has_error"]]
        cells.append(f"{sum(r['stopped'] for r in good)} / {len(good)}")
    out.append("| 正しい請求書を誤って止めた | " + " | ".join(cells) + " |")

    out += ["", "## 様式ごとの「7項目すべて一致」", ""]
    route_groups = {"文字": [r for r in rows if r["route"] == "text"], "画像": [r for r in rows if r["route"] == "image"]}
    out += ["| 様式 | 特徴 | 文字 | 画像 |", "|---|---|---|---|"]
    features = {
        "A": "罫線の表、角印が社名に重なる",
        "B": "10%と8%が混在、値引き行",
        "C": "和暦、「金〇〇円也」",
        "D": "内税、軽減税率は※印",
        "E": "個人事業主、源泉所得税、期日に年なし",
    }
    for layout, feature in features.items():
        cells = []
        for g in route_groups.values():
            sub = [r for r in g if r["layout"] == layout]
            cells.append(_rate(sum(r["all_match"] for r in sub), len(sub)) if sub else "-")
        out.append(f"| {layout} | {feature} | " + " | ".join(cells) + " |")

    out += [
        "",
        "## 1枚あたりの処理時間と費用",
        "",
        "| 経路 | 平均の秒数 | 平均の入力トークン | 平均の出力トークン | 平均の費用の目安 |",
        "|---|---|---|---|---|",
    ]
    for name, g in groups.items():
        ok = [r for r in g if r["data"] is not None]
        if not ok:
            continue
        n = len(ok)
        out.append(
            f"| {name} | {sum(r['seconds'] for r in ok) / n:.1f} | "
            f"{sum(r['input_tokens'] for r in ok) / n:,.0f} | {sum(r['output_tokens'] for r in ok) / n:,.0f} | "
            f"約{sum(r.get('usd') or 0 for r in ok) / n:.3f}ドル |"
        )
    out += ["", "費用は料金表（`llm.py` の `PRICES`）から計算した目安。出力トークンには思考の分も含む。"]

    out += ["", "## 正解と違った項目", ""]
    truth = {s.file: s.invoice for s in load_samples()}
    diffs = []
    for r in rows:
        if r["data"] is None:
            diffs.append(f"| {r['file']} | {_route(r)} | （読み取りエラー） | {r['error']} | |")
            continue
        for key, label in FIELDS:
            if not r["match"][key]:
                if key == "tax":
                    got = f"{r['data']['tax_10']} / {r['data']['tax_8']}"
                    want = f"{truth[r['file']]['tax_10']} / {truth[r['file']]['tax_8']}"
                else:
                    got, want = r["data"][key], truth[r["file"]][key]
                diffs.append(f"| {r['file']} | {_route(r)} | {label} | {got} | {want} |")
    if diffs:
        out += ["| ファイル | 経路 | 項目 | 読み取り | 正解 |", "|---|---|---|---|---|", *diffs]
    else:
        out.append("なし")

    out += ["", "## 誤って止めた請求書と理由", ""]
    false_stops = [r for r in rows if not r["has_error"] and r["stopped"]]
    if false_stops:
        out += ["| ファイル | 経路 | 理由 |", "|---|---|---|"]
        for r in false_stops:
            reasons = ", ".join(r["codes"]) or r["error"]
            out.append(f"| {r['file']} | {_route(r)} | {reasons} |")
    else:
        out.append("なし")
    out.append("")
    return "\n".join(out)


def _rate(n: int, total: int) -> str:
    return f"{n / total:.1%}（{n}/{total}）" if total else "-"


def _route(r: dict) -> str:
    if r["route"] == "text":
        return "文字"
    return "画像（スキャン風）" if r["scan"] else "画像"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rescore", type=Path, help="保存した結果のJSONから表だけ作り直す")
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    if args.rescore:
        results = json.loads(args.rescore.read_text(encoding="utf-8"))
    else:
        try:
            results = run(args.effort, args.workers)
        except LLMError as e:
            print(e)
            return 1
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        saved = RESULTS_DIR / f"eval_{datetime.now():%Y%m%d_%H%M%S}.json"
        saved.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"結果を保存しました: {saved.relative_to(PROJECT_ROOT).as_posix()}")

    rows = score(results)
    REPORT.write_text(build_report(results, rows), encoding="utf-8")
    print(f"表を書きました: {REPORT.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
