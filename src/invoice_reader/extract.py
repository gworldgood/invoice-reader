"""請求書ファイルから、AIに渡す材料（文字か画像）を取り出す。

- PDF：まず文字を取り出す（pdfplumber）。取り出せなければページを画像にする（pymupdf）
- JPG / PNG：そのまま画像として渡す
- 壊れたファイルや対応していない形式は ExtractError にする（呼び出し側で error/ に移す）
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pdfplumber
import pymupdf

# 文字がこれより少なければ「取り出せなかった」とみなして画像にする（スキャンしたPDFなど）
MIN_TEXT_CHARS = 30
# PDFを画像にするときの解像度。小さい文字（登録番号など）が潰れない程度
PDF_IMAGE_DPI = 200
# 1ファイルで読むページ数の上限（請求書は通常1〜2ページ）
MAX_PAGES = 5

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
# 読み取れるファイルの種類（画面で受け付けるときにも使う）
SUPPORTED_SUFFIXES = (".pdf", *IMAGE_TYPES)

# pdfminer がフォント情報の不足を大量に警告するが、文字の取り出しには影響しないので抑える
logging.getLogger("pdfminer").setLevel(logging.ERROR)


class ExtractError(Exception):
    """ファイルが読めない、壊れている、対応していない形式。"""


@dataclass(frozen=True)
class Image:
    media_type: str  # image/png など
    data: bytes


@dataclass(frozen=True)
class Extracted:
    source: Literal["text", "image"]  # staging.source に入れる値
    text: str | None = None
    images: list[Image] = field(default_factory=list)


def extract(path: Path, mode: Literal["auto", "image"] = "auto") -> Extracted:
    """ファイルから文字か画像を取り出す。

    mode="image" にすると、PDFでも文字を使わず画像にする。
    精度の測定（フェーズ5）で、文字の経路と画像の経路を分けて測るため。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if not path.is_file():
        raise ExtractError(f"ファイルがありません: {path.name}")

    if suffix in IMAGE_TYPES:
        data = path.read_bytes()
        if not data:
            raise ExtractError(f"空のファイルです: {path.name}")
        return Extracted(source="image", images=[Image(IMAGE_TYPES[suffix], data)])

    if suffix != ".pdf":
        raise ExtractError(f"対応していない形式です: {path.name}")

    if mode == "auto":
        text = pdf_text(path)
        if len("".join(text.split())) >= MIN_TEXT_CHARS:
            return Extracted(source="text", text=text)
    return Extracted(source="image", images=pdf_to_images(path))


def pdf_text(path: Path) -> str:
    """PDFの文字を取り出す。ページが複数あれば区切りを入れてつなぐ。"""
    try:
        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages[:MAX_PAGES]]
    except Exception as e:  # 壊れたPDFは pdfminer の色々な例外になるので、まとめて扱う
        raise ExtractError(f"PDFを開けません: {path.name}（{e.__class__.__name__}）") from e

    if len(pages) == 1:
        text = pages[0]
    else:
        text = "\n".join(f"--- {i}ページ目 ---\n{p}" for i, p in enumerate(pages, start=1))
    return fix_radicals(text)


def pdf_to_images(path: Path, dpi: int = PDF_IMAGE_DPI) -> list[Image]:
    """PDFの各ページをPNG画像にする。"""
    try:
        with pymupdf.open(path) as doc:
            images = [
                Image("image/png", page.get_pixmap(dpi=dpi).tobytes("png"))
                for page in list(doc)[:MAX_PAGES]
            ]
    except Exception as e:
        raise ExtractError(f"PDFを画像にできません: {path.name}（{e.__class__.__name__}）") from e
    if not images:
        raise ExtractError(f"ページがありません: {path.name}")
    return images


# ---------- 文字の直し ----------

# 日本語のPDFでは、「行」「日」などが見た目の同じ「康熙部首」（U+2F00〜U+2FD5）という
# 別の文字で入っていることがある。そのままだと「行」と一致しないので、普通の漢字に直す。
# Unicodeの正規化（NFKC）で直せるが、一部は旧字体になる（例：「⼾」が「戶」になり「戸」にならない）。
# そこで旧字体になるものだけ、日本でふだん使う字に置き換える。
_OLD_TO_COMMON = {
    "戶": "戸",
    "靑": "青",
    "黃": "黄",
    "黑": "黒",
    "齊": "斉",
    "齒": "歯",
    "龜": "亀",
}
_RADICALS = {}
for _code in range(0x2F00, 0x2FD6):
    _normalized = unicodedata.normalize("NFKC", chr(_code))
    _RADICALS[_code] = _OLD_TO_COMMON.get(_normalized, _normalized)


def fix_radicals(text: str) -> str:
    """康熙部首を普通の漢字に直す。それ以外の文字（全角数字など）は変えない。"""
    return text.translate(_RADICALS)
