import pytest
from pdf_builder import blank_pdf, encrypted_pdf, text_pdf

from mailroom.config import settings
from mailroom.extraction import TextStatus, extract_text

PDF = "application/pdf"
PLAIN = "text/plain"


def test_text_pdf_is_read():
    result = extract_text(text_pdf("Complaint for damages", "Client: Aldermere"), PDF)

    assert result.status is TextStatus.OK
    assert "Complaint for damages" in result.text
    assert "Client: Aldermere" in result.text


def test_pdf_with_no_text_is_reported_not_guessed():
    assert extract_text(blank_pdf(), PDF).status is TextStatus.NO_TEXT


def test_encrypted_pdf_is_reported():
    assert extract_text(encrypted_pdf("Secret"), PDF).status is TextStatus.ENCRYPTED


@pytest.mark.parametrize(
    "raw",
    [
        b"%PDF-1.4\n",
        b"%PDF-1.4\ngarbage that is not a pdf at all\n",
        b"%PDF-" + b"\xff" * 2000,
        text_pdf("Complaint")[:80],
    ],
)
def test_corrupt_pdf_is_reported_without_raising(raw):
    result = extract_text(raw, PDF)

    assert result.status in (TextStatus.CORRUPT, TextStatus.NO_TEXT)
    assert result.text == ""


def test_too_many_pages_is_reported(monkeypatch):
    monkeypatch.setattr("mailroom.extraction.settings", _limited(max_pdf_pages=0))

    assert extract_text(text_pdf("Complaint"), PDF).status is TextStatus.TOO_LARGE


def test_too_much_text_in_a_pdf_is_reported(monkeypatch):
    monkeypatch.setattr("mailroom.extraction.settings", _limited(max_text_chars=3))

    assert extract_text(text_pdf("Complaint"), PDF).status is TextStatus.TOO_LARGE


def test_too_much_plain_text_is_reported(monkeypatch):
    monkeypatch.setattr("mailroom.extraction.settings", _limited(max_text_chars=3))

    assert extract_text(b"Complaint", PLAIN).status is TextStatus.TOO_LARGE


def test_whitespace_only_text_is_empty():
    assert extract_text(b" \n\t ", PLAIN).status is TextStatus.EMPTY


def test_characters_postgres_rejects_never_leave_extraction():
    result = extract_text("a\x00b\ud800c".encode("utf-8", "surrogatepass"), PLAIN)

    assert result.status is TextStatus.OK
    assert "\x00" not in result.text
    result.text.encode("utf-8")


def _limited(**overrides):
    class Limited:
        max_pdf_pages = overrides.get("max_pdf_pages", settings.max_pdf_pages)
        max_text_chars = overrides.get("max_text_chars", settings.max_text_chars)

    return Limited
