"""AI（Anthropic API）に請求書の文字か画像を渡し、作業書5の形のJSONを受け取る。

- 返ってくる形は、APIの構造化出力（JSONスキーマを指定する機能）で固定する
- それでも形が崩れていたら（途中で切れた、断られた等）LLMError にする
- 計算の正しさはここでは見ない。検算は validate.py の仕事
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Literal

import anthropic
import pydantic
from pydantic import BaseModel, ConfigDict

from invoice_reader.config import Settings
from invoice_reader.extract import Extracted

# 思考の深さ。low / medium / high / xhigh / max。精度と費用の兼ね合いはフェーズ5で測る
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16000

# 安全上の理由で断られたとき、サーバー側で別のモデルに切り替えて続ける機能（ベータ）。
# 使えるモデルでだけ有効にする
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}


# ---------- 受け取るJSONの形（作業書5） ----------
# strict=True で、"1,000" や 1000.0 を整数に読み替えずにエラーにする


class LineItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: str | None
    qty: int | None
    unit: str | None
    price: int | None
    amount: int | None  # 値引きはマイナス。内税なら税込のまま
    rate: Literal[10, 8] | None


class InvoiceData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    invoice_no: str | None
    issuer: str | None
    registration_no: str | None
    recipient: str | None
    issue_date: str | None  # YYYY-MM-DD
    due_date: str | None
    tax_mode: Literal["外税", "内税"] | None
    taxable_10: int | None  # 税抜
    tax_10: int | None
    taxable_8: int | None
    tax_8: int | None
    withholding: int | None
    total: int | None  # 請求書に書かれた請求額
    is_handwritten: bool
    lines: list[LineItem]


SYSTEM_PROMPT = """\
あなたは経理担当者の補助として、日本の請求書から項目を読み取ります。
読み取った値は、このあとプログラムが検算し、合わないものは人が確認します。
そのため、計算で辻褄を合わせようとせず、請求書に書かれているとおりに読み取ってください。

決まりごと
- 金額はすべて整数（円）。カンマ、円記号、末尾の「-」や「也」は付けない
- 日付は YYYY-MM-DD。和暦は西暦に直す（令和元年が2019年、令和N年は2018+N年）。年が書かれていない日付は、発行日の年で補う
- 読み取れない項目や書かれていない項目は null。推測で埋めない

項目
- invoice_no: 請求書番号や伝票番号など、この請求書を識別する番号。「第123号」のように番号の前後に付いた「第」「号」も含めて、書かれたとおりに。「請求書番号:」のような見出しは含めない
- issuer: 請求書を出した会社または個人の名前（宛先ではない）。個人事業主なら屋号と氏名を書かれたとおりに。社名に重なっている角印（四角いはんこ）の中の文字は含めないが、名前そのものに含まれる語は削らない
- registration_no: 適格請求書発行事業者の登録番号。「T」と数字を、空白やハイフンを除いて半角で。桁が足りなくても補わない
- recipient: 宛先の名前。「御中」「様」は除く
- issue_date: 発行日
- due_date: 支払期日（お支払期限）
- tax_mode: 単価や金額が税込で書かれていれば「内税」、税抜で書かれて消費税を別に足していれば「外税」
- taxable_10, taxable_8: その税率の対象額を税抜で。内税の請求書では、書かれている税込額から消費税額を引いた額
- tax_10, tax_8: 書かれている消費税額。その税率の対象がなければ、対象額も消費税も 0
- withholding: 源泉所得税を差し引いていればその額。なければ 0
- total: 請求書に書かれている請求額（「ご請求金額」「合計」「金〇〇円也」など）をそのまま。自分で計算した額にしない
- is_handwritten: 金額や品名が手書きなら true
- lines: 明細を書かれた順に。amount は書かれている金額（内税なら税込のまま）。値引きの行はマイナス。rate は 10 か 8（「※」などで軽減税率と示された行は 8）
"""

TEXT_INSTRUCTION = """\
次の文字は、請求書のPDFから取り出したものです。表の罫線は失われており、印影の文字が混ざることがあります。

<請求書>
{text}
</請求書>"""

IMAGE_INSTRUCTION = "この請求書の画像から項目を読み取ってください。"


# 費用の目安を出すための料金（ドル／100万トークン：入力, 出力）。2026年11月時点の公開価格
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """料金表にあるモデルなら費用の目安（ドル）を返す。ないモデルは None。"""
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


class LLMError(Exception):
    """AIの返事が使えない（断られた、途中で切れた、JSONの形が崩れている）。"""


@dataclass(frozen=True)
class ReadResult:
    data: dict  # 作業書5の形
    source: str  # text / image
    model: str  # 実際に答えたモデル（切り替えが起きたら切り替え先）
    input_tokens: int
    output_tokens: int  # 思考の分も含む（料金がかかる量）
    seconds: float
    request_id: str | None


def make_client(settings: Settings) -> anthropic.Anthropic:
    missing = settings.missing_for_api()
    if missing:
        raise LLMError(f".env に {', '.join(missing)} を設定してください")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def describe_api_error(e: Exception) -> str:
    """APIそのものの失敗を、画面で使う人向けの文にする。何をすればよいかまで書く。

    ファイルが悪いわけではないので、ファイルは読み取り待ちに残っている（pipeline.process_file）。
    """
    again = "少し時間をおいてから、もう一度「読み取る」を押してください。"
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "AIの読み取りサービスのAPIキーが使えません。管理者に連絡してください。"
    if isinstance(e, anthropic.NotFoundError):
        return "設定されているAIのモデル名が見つかりません。管理者に連絡してください。"
    if isinstance(e, anthropic.RateLimitError):
        return "AIの読み取りサービスが混み合っています。" + again
    if isinstance(e, anthropic.APIConnectionError):  # 時間切れ（APITimeoutError）も含む
        return "AIの読み取りサービスにつながりませんでした。インターネットの接続を確かめて、" + again
    if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
        return "AIの読み取りサービスが一時的に使えません。" + again
    return "読み取りの途中で問題が起きました。管理者に連絡してください。"


def read_invoice(
    extracted: Extracted,
    settings: Settings,
    client: anthropic.Anthropic | None = None,
    effort: str = DEFAULT_EFFORT,
) -> ReadResult:
    """取り出した文字か画像をAIに渡し、項目を読み取る。

    APIそのものの失敗（通信エラー、レート制限など）は anthropic の例外のまま上に投げる。
    返事の中身が使えないときだけ LLMError にする。
    """
    client = client or make_client(settings)
    model = settings.model
    if not model:
        raise LLMError(".env に MODEL を設定してください")

    request = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_content(extracted)}],
        "output_config": {"effort": effort},
        "output_format": InvoiceData,
    }
    if model in FALLBACK_MODELS:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"

    started = time.perf_counter()
    try:
        response = client.beta.messages.parse(**request)
    except (pydantic.ValidationError, ValueError) as e:
        raise LLMError(f"AIの返事がJSONの形になっていません: {e}") from e
    seconds = time.perf_counter() - started

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        raise LLMError(f"AIが読み取りを断りました（{category}）")
    if response.stop_reason == "max_tokens":
        raise LLMError("AIの返事が途中で切れました（max_tokens に達した）")
    parsed = response.parsed_output
    if parsed is None:
        raise LLMError("AIの返事からJSONを取り出せませんでした")

    return ReadResult(
        data=normalize_names(parsed.model_dump()),
        source=extracted.source,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        seconds=seconds,
        request_id=getattr(response, "_request_id", None),
    )


_SPACES = re.compile(r"\s+")  # 全角の空白（U+3000）も含む


def normalize_name(value: str | None) -> str | None:
    """名前の中の空白を、全角・半角や個数にかかわらず半角1つにそろえる。

    「屋号　氏名」の全角空白は、PDFの文字でも画像でも半角になったりならなかったりする。
    そろえないと、同じ発行元なのに二重登録の確認（発行元＋請求書番号）をすり抜ける。
    """
    if value is None:
        return None
    return _SPACES.sub(" ", value).strip()


def normalize_names(data: dict) -> dict:
    return {**data, "issuer": normalize_name(data["issuer"]), "recipient": normalize_name(data["recipient"])}


def build_content(extracted: Extracted) -> list[dict]:
    """APIに渡す user メッセージの中身。画像は説明文より前に置く。"""
    if extracted.source == "text":
        return [{"type": "text", "text": TEXT_INSTRUCTION.format(text=extracted.text)}]

    blocks: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.standard_b64encode(image.data).decode("ascii"),
            },
        }
        for image in extracted.images
    ]
    blocks.append({"type": "text", "text": IMAGE_INSTRUCTION})
    return blocks
