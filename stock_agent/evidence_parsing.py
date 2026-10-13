"""GLM-03 body extraction with explicit statuses; no fake extraction.

Deterministic, offline, version-locked extraction for retained evidence
bytes. Every failure mode is an explicit status string so downstream code
can never mistake a failed extraction for an empty document: scanned PDFs
report needs_ocr, non-UTF-8 bytes report not_utf8, and unsupported formats
report unsupported_format instead of silently returning partial text.
HTML gets a controlled stdlib text extraction (tags/scripts/styles dropped,
entities decoded, whitespace collapsed) - no external resources, no script
execution, same size caps as plain text.
"""
from __future__ import annotations

import html as _html
from html.parser import HTMLParser
import re
from typing import Any, Dict

PARSER_VERSION = "body-extract-v2-html"
MAX_BODY_BYTES = 2 * 1024 * 1024
_DECODE = "utf-8-sig"

_PDF_MAGIC = b"%PDF-"
_HTML_MARKERS = (b"<!doctype html", b"<html")
# Any document opening like a tag (element, comment, or doctype) is HTML.
_HTML_TAG_OPEN = re.compile(rb"\A\s*<[a-zA-Z!/]")

_DROP_CONTENT_TAGS = {"script", "style", "noscript", "template"}
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "table", "thead", "tbody", "section", "article", "blockquote"}


class _TextExtractor(HTMLParser):
    """Collect visible text only; drops script/style content and all attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_endtag(self, tag) -> None:
        if tag in _DROP_CONTENT_TAGS and self._drop_depth:
            self._drop_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_data(self, data) -> None:
        if not self._drop_depth and data:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse whitespace runs (incl. newlines/tabs) to single spaces.
        collapsed = " ".join(raw.split())
        return _html.unescape(collapsed)


def _extract_html(data: bytes) -> Dict[str, Any]:
    try:
        source = data.decode(_DECODE, errors="strict")
    except UnicodeError:
        return {"status": "not_utf8"}
    extractor = _TextExtractor()
    try:
        extractor.feed(source)
        extractor.close()
    except Exception:
        return {"status": "unsupported_content",
                "reason": "html parsing failed"}
    text = extractor.text().strip()
    if not text:
        return {"status": "empty"}
    return {"status": "ok", "text": text, "char_count": len(text)}


def _detect_format(data: bytes) -> str:
    head = data[:512].lstrip()
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    lowered = head[:256].lower()
    if any(marker in lowered for marker in _HTML_MARKERS) or _HTML_TAG_OPEN.match(head):
        return "html"
    return "text"


def extract_body(data: bytes) -> Dict[str, Any]:
    """Extract UTF-8 or HTML text from retained bytes with explicit statuses.

    ok / empty / not_utf8 / too_large / needs_ocr / unsupported_format /
    unsupported_content. `text` is present only for status ok.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("extract_body accepts bytes only")
    payload = bytes(data)
    byte_size = len(payload)
    base: Dict[str, Any] = {
        "parser_version": PARSER_VERSION,
        "byte_size": byte_size,
    }
    if byte_size == 0:
        return {**base, "status": "empty", "detected_format": "text"}
    if byte_size > MAX_BODY_BYTES:
        return {**base, "status": "too_large", "detected_format": _detect_format(payload)}
    detected = _detect_format(payload)
    base["detected_format"] = detected
    if detected == "pdf":
        # A PDF without font/text operators is almost certainly a scan: report
        # the OCR need honestly instead of guessing at embedded text.
        if b"/Font" not in payload and b"BT" not in payload:
            return {**base, "status": "needs_ocr",
                    "reason": "pdf has no text layer; scanned document requires OCR"}
        return {**base, "status": "unsupported_format",
                "reason": "pdf has a text layer but local pdf text extraction is not implemented yet"}
    if detected == "html":
        result = _extract_html(payload)
        return {**base, **result}
    try:
        text = payload.decode(_DECODE, errors="strict")
    except UnicodeError:
        return {**base, "status": "not_utf8"}
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        return {**base, "status": "unsupported_content",
                "reason": "document contains control characters outside tab/lf/cr"}
    return {
        **base,
        "status": "ok",
        "text": text,
        "char_count": len(text),
    }

