"""llm.py のテスト。APIは呼ばず、偽物のクライアントで確かめる（料金がかからないように）。"""

from types import SimpleNamespace

import pydantic
import pytest

from invoice_reader.config import Settings
from invoice_reader.extract import Extracted, Image
from invoice_reader.llm import (
    FALLBACK_BETA,
    InvoiceData,
    LLMError,
    SYSTEM_PROMPT,
    read_invoice,
)

GOOD = {
    "invoice_no": "INV-1",
    "issuer": "架空商事株式会社",
    "registration_no": "T8999900000001",
    "recipient": "株式会社テスト商会",
    "issue_date": "2026-09-30",
    "due_date": "2026-10-31",
    "tax_mode": "外税",
    "taxable_10": 10000,
    "tax_10": 1000,
    "taxable_8": 0,
    "tax_8": 0,
    "withholding": 0,
    "total": 11000,
    "is_handwritten": False,
    "lines": [
        {"name": "作業費", "qty": 1, "unit": "式", "price": 10000, "amount": 10000, "rate": 10}
    ],
}


def settings(model="claude-opus-5-5"):
    return Settings(anthropic_api_key="test", model=model, jev_api_key=None, data_dir=None)


class FakeClient:
    """client.beta.messages.parse(...) の呼び出しを記録し、決めた返事を返す。"""

    def __init__(self, stop_reason="end_turn", parsed=GOOD, error=None):
        self.calls = []
        self._stop_reason = stop_reason
        self._parsed = parsed
        self._error = error
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return SimpleNamespace(
            stop_reason=self._stop_reason,
            stop_details=SimpleNamespace(category="cyber"),
            parsed_output=InvoiceData(**self._parsed) if self._parsed else None,
            model="claude-opus-5-5",
            usage=SimpleNamespace(input_tokens=1200, output_tokens=800),
            _request_id="req_test",
        )


TEXT = Extracted(source="text", text="請求書番号:INV-1")
IMAGE = Extracted(source="image", images=[Image("image/jpeg", b"\xff\xd8abc")])


def test_text_request_and_result():
    client = FakeClient()
    result = read_invoice(TEXT, settings(), client=client)

    assert result.data == GOOD
    assert result.source == "text"
    assert (result.input_tokens, result.output_tokens) == (1200, 800)
    assert result.request_id == "req_test"

    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_format"] is InvoiceData
    assert call["output_config"] == {"effort": "medium"}
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "請求書番号:INV-1" in content[0]["text"]


def test_image_request_puts_image_first():
    client = FakeClient()
    result = read_invoice(IMAGE, settings(), client=client)

    assert result.source == "image"
    content = client.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[0]["source"]["data"] == "/9hhYmM="  # base64 of b"\xff\xd8abc"


def test_fallback_is_on_only_for_supported_models():
    client = FakeClient()
    read_invoice(TEXT, settings("claude-opus-5-5"), client=client)
    assert client.calls[0]["betas"] == [FALLBACK_BETA]
    assert client.calls[0]["fallbacks"] == "default"

    client = FakeClient()
    read_invoice(TEXT, settings("claude-haiku-5-5"), client=client)
    assert "fallbacks" not in client.calls[0]
    assert "betas" not in client.calls[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stop_reason": "refusal"},
        {"stop_reason": "max_tokens"},
        {"parsed": None},
        {"error": ValueError("bad json")},
    ],
)
def test_unusable_answers_raise(kwargs):
    with pytest.raises(LLMError):
        read_invoice(TEXT, settings(), client=FakeClient(**kwargs))


def test_missing_settings():
    s = Settings(anthropic_api_key=None, model=None, jev_api_key=None, data_dir=None)
    with pytest.raises(LLMError):
        read_invoice(TEXT, s)


@pytest.mark.parametrize(
    "change",
    [
        {"total": 11000.0},
        {"total": "11,000"},
        {"tax_mode": "税込"},
        {"unknown_field": 1},
    ],
)
def test_schema_is_strict(change):
    with pytest.raises(pydantic.ValidationError):
        InvoiceData(**{**GOOD, **change})


def test_names_have_their_spaces_unified():
    client = FakeClient(parsed={**GOOD, "issuer": " テスト事務所　見本  太郎 ", "recipient": "株式会社\tテスト商会"})
    result = read_invoice(TEXT, settings(), client=client)

    assert result.data["issuer"] == "テスト事務所 見本 太郎"
    assert result.data["recipient"] == "株式会社 テスト商会"


def test_schema_has_every_field_of_section_5():
    from invoice_reader.db import INVOICE_FIELDS

    assert set(InvoiceData.model_fields) == set(INVOICE_FIELDS) | {"lines"}
