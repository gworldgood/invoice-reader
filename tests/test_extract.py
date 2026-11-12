import pytest

from invoice_reader.extract import ExtractError, extract, fix_radicals
from invoice_reader.ground_truth import SAMPLE_DIR, load_samples

INVOICES = SAMPLE_DIR / "invoices"


def test_text_pdf_gives_text():
    result = extract(INVOICES / "invoice_01.pdf")

    assert result.source == "text"
    assert "INV-2026-7771" in result.text
    assert "T8999900000001" in result.text
    assert result.images == []


def test_radicals_are_fixed_in_pdf_text():
    # invoice_01 の「瀬戸内」「発行日」は、PDFの中では康熙部首の「⼾」「⾏」「⽇」で入っている
    text = extract(INVOICES / "invoice_01.pdf").text
    assert "瀬戸内サンプル物流株式会社" in text
    assert "発行日" in text


def test_fix_radicals():
    assert fix_radicals("瀬⼾内 発⾏⽇ ⾦額") == "瀬戸内 発行日 金額"
    assert fix_radicals("１２３ＡＢＣ（株）") == "１２３ＡＢＣ（株）"  # 部首以外は変えない


def test_every_sample_pdf_has_text():
    for sample in load_samples():
        if not sample.scan:
            assert extract(sample.path).source == "text", sample.file


def test_jpg_is_sent_as_image():
    result = extract(INVOICES / "invoice_05.jpg")

    assert result.source == "image"
    assert result.text is None
    assert result.images[0].media_type == "image/jpeg"
    assert result.images[0].data[:2] == b"\xff\xd8"  # JPEGの先頭


def test_pdf_can_be_forced_to_image():
    result = extract(INVOICES / "invoice_01.pdf", mode="image")

    assert result.source == "image"
    assert len(result.images) == 1
    assert result.images[0].media_type == "image/png"
    assert result.images[0].data[:8] == b"\x89PNG\r\n\x1a\n"


def test_broken_pdf(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")
    with pytest.raises(ExtractError):
        extract(broken)


def test_unsupported_format(tmp_path):
    other = tmp_path / "invoice.xlsx"
    other.write_bytes(b"x")
    with pytest.raises(ExtractError):
        extract(other)


def test_missing_file(tmp_path):
    with pytest.raises(ExtractError):
        extract(tmp_path / "nothing.pdf")
