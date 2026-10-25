# Copyright (c) 2023 Carter Lasalle
"""Extract searchable text from an attachment file: plain text, PDF (pypdf), image (OCR)."""

from __future__ import annotations

from pathlib import Path

# Plain-text-ish extensions we decode directly even when chat.db has no MIME
# type (or a generic one) for the attachment.
_TEXT_EXTENSIONS = {
    "txt",
    "md",
    "csv",
    "tsv",
    "json",
    "log",
    "xml",
    "html",
    "htm",
    "yaml",
    "yml",
    "srt",
    "vtt",
}


# PDFs can be huge; only the first pages carry the human-readable content.
_MAX_PDF_PAGES = 50


def _read_text_file(path: str, max_file_bytes: int) -> tuple[str | None, str | None]:
    """Read a text-like file, returning ``(text, skip_reason)``."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_file_bytes)
    except OSError:
        return None, "unreadable"
    return raw.decode("utf-8", errors="replace"), None


def _read_pdf_file(path: str) -> tuple[str | None, str | None]:
    """Extract text from the first pages of a PDF, or explain the skip."""
    try:
        import pypdf  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:
        return None, "pdf_unsupported"

    try:
        reader = pypdf.PdfReader(path)
        pages = reader.pages[:_MAX_PDF_PAGES]
        texts = [page.extract_text() or "" for page in pages]
    except OSError:
        return None, "unreadable"
    except Exception:
        # Malformed/encrypted PDFs must not abort the whole search.
        return None, "unreadable"
    return "\n".join(texts), None


def _read_image_file(path: str) -> tuple[str | None, str | None]:
    """OCR an image when tesseract is available, or explain the skip."""
    try:
        import pytesseract  # type: ignore[import-not-found,import-untyped,unused-ignore]

        pytesseract.get_tesseract_version()
    except Exception:
        return None, "ocr_unsupported"

    try:
        from PIL import Image

        with Image.open(path) as image:
            text = pytesseract.image_to_string(image)
    except OSError:
        return None, "unreadable"
    except Exception:
        return None, "unreadable"
    return text, None


def extract_attachment_text(
    path: str,
    mime_type: str | None,
    max_file_bytes: int,
) -> tuple[str | None, str | None]:
    """Extract searchable text from one attachment file.

    Returns ``(text, skip_reason)``. ``text`` is None when the row could not
    be extracted; ``skip_reason`` is one of ``unreadable``,
    ``pdf_unsupported``, ``ocr_unsupported`` or ``unsupported_type``.
    """
    mime = (mime_type or "").lower()
    suffix = Path(path).suffix.lstrip(".").lower()

    if mime.startswith("text/") or suffix in _TEXT_EXTENSIONS:
        return _read_text_file(path, max_file_bytes)
    if mime == "application/pdf":
        return _read_pdf_file(path)
    if mime.startswith("image/"):
        return _read_image_file(path)
    return None, "unsupported_type"
