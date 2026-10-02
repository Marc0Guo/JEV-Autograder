"""Convert uploaded documents to Markdown with MarkItDown."""

from __future__ import annotations

import io
from pathlib import Path

DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".zip"}

_converter = None


def document_suffix(name: str, content_type: str = "") -> str:
    suffix = Path(name).suffix.lower()
    if suffix in DOCUMENT_SUFFIXES:
        return suffix
    kind = (content_type or "").lower()
    if "pdf" in kind:
        return ".pdf"
    if "wordprocessingml" in kind:
        return ".docx"
    if "presentationml" in kind or "powerpoint" in kind:
        return ".pptx"
    if "spreadsheetml" in kind or "excel" in kind:
        return ".xlsx"
    if kind in {"application/zip", "application/x-zip-compressed"} or kind.endswith("/zip"):
        return ".zip"
    return ""


def bytes_to_markdown(data: bytes, name: str, content_type: str = "") -> str | None:
    """Return Markdown for a PDF or Office file.

    Returns None when the file is not one of those formats, so callers can
    keep their own plain-text path.
    """
    suffix = document_suffix(name, content_type)
    if not suffix:
        return None
    if not data:
        return f"[{name}: no text extracted]"
    try:
        converter = _markitdown()
    except ImportError:
        return f"[{name}: MarkItDown is not installed]"
    try:
        result = converter.convert_stream(io.BytesIO(data), file_extension=suffix)
    except Exception as exc:
        return f"[{name}: conversion failed: {exc}]"
    text = getattr(result, "markdown", None) or getattr(result, "text_content", None) or ""
    text = str(text).strip()
    if not text:
        return f"[{name}: no text extracted]"
    return text


def _markitdown():
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        _converter = MarkItDown(enable_plugins=False)
    return _converter
