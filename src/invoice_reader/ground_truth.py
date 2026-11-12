"""sample_data の正解データを、作業書「5. AIから受け取るJSON」と同じ形に直す。

検算のテスト（フェーズ2）と精度の測定（フェーズ5）の両方で使う。
sample_data/ の中身は読むだけで、書き換えない。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from invoice_reader.config import PROJECT_ROOT

SAMPLE_DIR = PROJECT_ROOT / "sample_data"

# わざと誤りを入れた3枚と、検算で止まるべき理由の種類（validate.Problem.code）。作業書9
EXPECTED_STOP = {
    "invoice_08": {"total"},  # 記載の請求額が明細の合計と9,000円ずれている
    "invoice_15": {"regno_format"},  # 登録番号が1桁足りない
    "invoice_23": {"due_before_issue"},  # 支払期日が発行日より前
}


@dataclass(frozen=True)
class Sample:
    file: str  # 拡張子なしのファイル名（invoice_01）
    path: Path  # 請求書ファイルのパス（scan なら .jpg、それ以外は .pdf）
    layout: str  # 様式 A〜E
    scan: bool  # スキャン風の画像か
    expected_error: str  # 空でなければ、検算で止まるべき（わざと入れた誤りの内容）
    invoice: dict  # 作業書5の形


def load_samples(sample_dir: Path = SAMPLE_DIR) -> list[Sample]:
    data = json.loads((sample_dir / "ground_truth.json").read_text(encoding="utf-8"))
    tax_modes = _load_tax_modes(sample_dir / "ground_truth.csv")

    samples = []
    for g in data:
        ext = ".jpg" if g["scan"] else ".pdf"
        samples.append(
            Sample(
                file=g["file"],
                path=sample_dir / "invoices" / f"{g['file']}{ext}",
                layout=g["layout"],
                scan=g["scan"],
                expected_error=g["err"],
                invoice=_to_invoice(g, tax_modes[g["file"]]),
            )
        )
    return samples


def _to_invoice(g: dict, tax_mode: str) -> dict:
    """ground_truth.json の1件を作業書5の形にする。対応は作業書9の表のとおり。"""
    return {
        "invoice_no": g["invoice_no"],
        "issuer": g["issuer"],
        "registration_no": g["regno"],
        "recipient": g["recipient"],
        "issue_date": g["issue"],
        "due_date": g["due"],
        "tax_mode": tax_mode,
        "taxable_10": g["x10"],
        "tax_10": g["t10"],
        "taxable_8": g["x8"],
        "tax_8": g["t8"],
        "withholding": g["wh"],
        "total": g["printed_total"],  # 紙に書かれている額。計算上の正しい額（total）ではない
        "is_handwritten": False,  # 正解データの30枚はすべて印字
        "lines": [
            {
                "name": line["name"],
                "qty": line["qty"],
                "unit": line["unit"],
                "price": line["price"],
                "amount": line["amount"],
                "rate": line["rate"],
            }
            for line in g["lines"]
        ],
    }


def _load_tax_modes(csv_path: Path) -> dict[str, str]:
    """税方式は ground_truth.json にないので、ground_truth.csv の「税方式」列から取る。"""
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        return {Path(row["ファイル"]).stem: row["税方式"] for row in csv.DictReader(f)}
