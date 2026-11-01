"""Turns stored bytes into text, and says plainly when it cannot.

A document that can't be read is not an error in the pipeline. It is a normal outcome that
goes to a person, so nothing is ever classified from partial or missing text.
"""

import io
import logging
from dataclasses import dataclass
from enum import StrEnum

from pypdf import PdfReader

from mailroom.config import settings

logger = logging.getLogger(__name__)


class TextStatus(StrEnum):
    OK = "OK"
    NO_TEXT = "NO_TEXT"
    EMPTY = "EMPTY"
    CORRUPT = "CORRUPT"
    ENCRYPTED = "ENCRYPTED"
    TOO_LARGE = "TOO_LARGE"


@dataclass(frozen=True)
class ExtractedText:
    status: TextStatus
    text: str = ""


def _clean(text: str) -> str:
    """Postgres text and JSONB reject NUL and lone surrogates, so they never leave here."""
    return text.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


def _extract_pdf(raw: bytes) -> ExtractedText:
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            return ExtractedText(TextStatus.ENCRYPTED)
        if len(reader.pages) > settings.max_pdf_pages:
            return ExtractedText(TextStatus.TOO_LARGE)

        pages: list[str] = []
        total = 0
        for page in reader.pages:
            page_text = page.extract_text()
            total += len(page_text)
            if total > settings.max_text_chars:
                return ExtractedText(TextStatus.TOO_LARGE)
            pages.append(page_text)
    except Exception as exc:
        # The trust boundary for hostile input. pypdf raises many types on malformed files,
        # including its own limit errors for decompression bombs.
        logger.info("pdf could not be read", extra={"error_type": type(exc).__name__})
        return ExtractedText(TextStatus.CORRUPT)

    text = _clean("\n".join(pages)).strip()
    return ExtractedText(TextStatus.OK, text) if text else ExtractedText(TextStatus.NO_TEXT)


def _extract_plain(raw: bytes) -> ExtractedText:
    if len(raw) > settings.max_text_chars:
        return ExtractedText(TextStatus.TOO_LARGE)
    text = _clean(raw.decode("utf-8", "replace")).strip()
    return ExtractedText(TextStatus.OK, text) if text else ExtractedText(TextStatus.EMPTY)


def extract_text(raw: bytes, mime_type: str) -> ExtractedText:
    if mime_type == "application/pdf":
        return _extract_pdf(raw)
    return _extract_plain(raw)
