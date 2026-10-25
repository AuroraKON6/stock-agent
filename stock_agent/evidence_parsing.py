"""GLM-03 body extraction with explicit statuses; no fake extraction.

Deterministic, offline, version-locked extraction for retained evidence
bytes. Every failure mode is an explicit status string so downstream code
can never mistake a failed extraction for an empty document: scanned PDFs
report needs_ocr, non-UTF-8 bytes report not_utf8, and unsupported formats
report unsupported_format instead of silently returning partial text.
HTML gets a controlled stdlib text extraction (tags/scripts/styles dropped,
entities decoded, whitespace collapsed) - no external resources, no script
execution, same size caps as plain text.

R12b: PDFs WITH a text layer get a controlled stdlib extraction too
(``pdf-text-v1``): classic object layout (no object streams), simple fonts
(no CID/Type0), FlateDecode or raw content streams. Text-showing operators
are decoded with positional tracking (Tm/Td rows) and every page carries
its own text hash; a table-alignment heuristic FLAGS table-like regions
instead of claiming semantic tables. Everything outside the subset is an
explicit refusal (encrypted / unsupported_pdf_feature / corrupt / empty),
never a partial success. No external parser dependency: adopting a mature
PDF library stays a separate, evaluated dependency decision.
"""
from __future__ import annotations

import html as _html
from html.parser import HTMLParser
import re
import zlib
from typing import Any, Dict, List, Optional, Tuple

PARSER_VERSION = "body-extract-v2-html"
PDF_TEXT_PARSER_VERSION = "pdf-text-v1"
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
        result = extract_pdf_text(payload)
        if result.get("status") == "ok":
            return {**base, **result}
        return {**base, "status": result.get("status"),
                "reason": result.get("reason"),
                "parser_version": PDF_TEXT_PARSER_VERSION}
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


# ---------------------------------------------------------------------- #
# R12b: text-layer PDF extraction (stdlib, explicit subset)              #
# ---------------------------------------------------------------------- #

_PDF_OBJ_HEADER = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_PDF_PAGES_TYPE = re.compile(rb"/Type\s*/Pages\b")
_PDF_PAGE_TYPE = re.compile(rb"/Type\s*/Page\b")
_PDF_KIDS = re.compile(rb"/Kids\s*\[\s*((?:\d+\s+\d+\s+R[\s]*)*)\]")
_PDF_CONTENTS = re.compile(rb"/Contents\s*(\d+\s+\d+\s+R|\[[^\]]*\])")
_PDF_REF = re.compile(rb"(\d+)\s+\d+\s+R")
_PDF_FILTER = re.compile(rb"/Filter\s*(/[A-Za-z0-9]+|\[[^\]]*\])")
_PDF_CID_MARKERS = (b"/Type0", b"/CIDFontType0", b"/CIDFontType2",
                    b"/Identity-H", b"/Identity-V")


class _PdfRefusal(Exception):
    """Internal: carry an explicit refusal status + reason upward."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _pdf_scan_objects(data: bytes) -> Dict[int, Dict[str, Any]]:
    """Tolerant object scan (header regex, body cut at endobj/next header)."""
    objects: Dict[int, Dict[str, Any]] = {}
    matches = list(_PDF_OBJ_HEADER.finditer(data))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(data)
        body = data[match.end():end]
        endobj = body.find(b"endobj")
        if endobj != -1:
            body = body[:endobj]
        stream_at = body.find(b"stream")
        if stream_at == -1:
            objects[int(match.group(1))] = {"dict": body, "stream": None}
            continue
        stream_body = body[stream_at + len(b"stream"):]
        if stream_body.startswith(b"\r\n"):
            stream_body = stream_body[2:]
        elif stream_body[:1] in (b"\n", b"\r"):
            stream_body = stream_body[1:]
        endstream = stream_body.rfind(b"endstream")
        if endstream != -1:
            stream_body = stream_body[:endstream]
        objects[int(match.group(1))] = {"dict": body[:stream_at], "stream": stream_body}
    return objects


def _pdf_stream_bytes(obj: Dict[str, Any]) -> bytes:
    """Decode one object's content stream: raw or exactly FlateDecode."""
    stream = obj.get("stream")
    if stream is None:
        return b""
    match = _PDF_FILTER.search(obj["dict"])
    if match is None:
        return stream
    spec = match.group(1)
    filters = ([spec] if spec.startswith(b"/")
               else re.findall(rb"/[A-Za-z0-9]+", spec))
    names = [f.decode("latin-1") for f in filters]
    if names == ["/FlateDecode"]:
        try:
            return zlib.decompress(stream)
        except zlib.error as exc:
            raise _PdfRefusal("corrupt",
                              "flate stream failed to decompress: {0}".format(exc))
    raise _PdfRefusal(
        "unsupported_pdf_feature",
        "stream filter {0} is outside the text-layer subset".format(names))


def _pdf_decode_string(raw: bytes, notes: Dict[str, int]) -> str:
    """WinAnsi-ish decode via cp1252 with honest replacement accounting."""
    text = raw.decode("cp1252", errors="replace")
    cleaned = []
    replaced = 0
    for char in text:
        code = ord(char)
        if code < 32 and char not in "\t\n\r":
            replaced += 1
            cleaned.append(" ")
        else:
            cleaned.append(char)
    joined = "".join(cleaned)
    if replaced:
        notes["control_replaced"] = notes.get("control_replaced", 0) + replaced
    if "\ufffd" in joined:
        notes["replacement_chars"] = notes.get("replacement_chars", 0) + joined.count("\ufffd")
    return joined


def _pdf_tokenize(content: str) -> List[Tuple[str, Any]]:
    """Scan a content stream into (kind, value) tokens.

    Kinds: 'str' with raw bytes (literal or hex), 'num' with float, 'op' with
    the operator name, 'arr_open'/'arr_close' for TJ arrays. Names,
    dictionaries and inline images are skipped as noise."""
    tokens: List[Tuple[str, Any]] = []
    index, size = 0, len(content)
    while index < size:
        char = content[index]
        if char in " \t\r\n\f\x00":
            index += 1
            continue
        if char == "(":
            raw = bytearray()
            depth = 1
            index += 1
            while index < size and depth:
                current = content[index]
                if current == "\\" and index + 1 < size:
                    nxt = content[index + 1]
                    if nxt == "n":
                        raw.append(10)
                        index += 2
                    elif nxt == "r":
                        raw.append(13)
                        index += 2
                    elif nxt == "t":
                        raw.append(9)
                        index += 2
                    elif nxt == "b":
                        raw.append(8)
                        index += 2
                    elif nxt == "f":
                        raw.append(12)
                        index += 2
                    elif nxt in "()\\":
                        raw.append(ord(nxt))
                        index += 2
                    elif nxt == "\n":
                        index += 2
                    elif nxt == "\r":
                        index += 2
                        if index < size and content[index] == "\n":
                            index += 1
                    elif nxt in "01234567":
                        digits = ""
                        offset = index + 1
                        while offset < size and len(digits) < 3 and content[offset] in "01234567":
                            digits += content[offset]
                            offset += 1
                        raw.append(int(digits, 8) & 0xFF)
                        index = offset
                    else:
                        raw.append(ord(nxt))
                        index += 2
                    continue
                if current == "(":
                    depth += 1
                elif current == ")":
                    depth -= 1
                    if not depth:
                        index += 1
                        break
                raw.append(ord(current) & 0xFF)
                index += 1
            tokens.append(("str", bytes(raw)))
            continue
        if char == "<" and index + 1 < size and content[index + 1] == "<":
            depth = 0
            index += 2
            while index < size:
                if content.startswith("<<", index):
                    depth += 1
                    index += 2
                elif content.startswith(">>", index):
                    if depth == 0:
                        index += 2
                        break
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        if char == "<":
            index += 1
            digits = ""
            while index < size and content[index] != ">":
                if content[index] in "0123456789abcdefABCDEF":
                    digits += content[index]
                index += 1
            index += 1
            if len(digits) % 2:
                digits += "0"
            tokens.append(("str", bytes.fromhex(digits)))
            continue
        if char == "[":
            tokens.append(("arr_open", None))
            index += 1
            continue
        if char == "]":
            tokens.append(("arr_close", None))
            index += 1
            continue
        match = re.match(r"[+-]?(?:\d+\.?\d*|\.\d+)", content[index:])
        if match and (char in "+-.0123456789"):
            tokens.append(("num", float(match.group(0))))
            index += len(match.group(0))
            continue
        match = re.match(r"/[A-Za-z0-9#+._-]*", content[index:])
        if match:
            index += len(match.group(0))
            continue
        match = re.match(r"[A-Za-z'\"*]{1,3}", content[index:])
        if match:
            tokens.append(("op", match.group(0)))
            index += len(match.group(0))
            continue
        index += 1
    return tokens


def _pdf_page_items(tokens: List[Tuple[str, Any]],
                    notes: Dict[str, int]) -> List[Dict[str, Any]]:
    """Interpret the text/positioning operators into positioned text items."""
    items: List[Dict[str, Any]] = []
    operands: List[Any] = []
    x = y = 0.0
    leading = 0.0
    for kind, value in tokens:
        if kind in ("num", "str"):
            operands.append(value)
        elif kind == "arr_open":
            operands.append("[")
        elif kind == "arr_close":
            collected: List[Any] = []
            while operands and operands[-1] != "[":
                collected.insert(0, operands.pop())
            if operands:
                operands.pop()
            operands.append(collected)
        elif kind == "op":
            if value == "BT":
                x = y = 0.0
                operands.clear()
            elif value == "ET":
                operands.clear()
            elif value == "Tm" and len(operands) >= 6 and all(
                    isinstance(v, float) for v in operands[-6:]):
                x, y = operands[-2], operands[-1]
                operands.clear()
            elif value in ("Td", "TD") and len(operands) >= 2 and all(
                    isinstance(v, float) for v in operands[-2:]):
                tx, ty = operands[-2], operands[-1]
                if value == "TD":
                    leading = -ty
                x, y = x + tx, y + ty
                operands.clear()
            elif value == "TL" and operands and isinstance(operands[-1], float):
                leading = operands[-1]
                operands.clear()
            elif value == "T*":
                y -= leading
                operands.clear()
            elif value == "Tf":
                operands.clear()
            elif value == "Tj" and operands and isinstance(operands[-1], bytes):
                text = _pdf_decode_string(operands[-1], notes)
                if text:
                    items.append({"x": round(x, 2), "y": round(y, 2), "text": text})
                operands.clear()
            elif value == "TJ" and operands and isinstance(operands[-1], list):
                chunks = [part for part in operands[-1] if isinstance(part, bytes)]
                text = "".join(_pdf_decode_string(part, notes) for part in chunks)
                if text:
                    items.append({"x": round(x, 2), "y": round(y, 2), "text": text})
                operands.clear()
            elif value in ("'", '"'):
                y -= leading
                if operands and isinstance(operands[-1], bytes):
                    text = _pdf_decode_string(operands[-1], notes)
                    if text:
                        items.append({"x": round(x, 2), "y": round(y, 2), "text": text})
                operands.clear()
            else:
                operands.clear()
    return items


def _pdf_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group positioned items into rows (same rounded y), x-ordered."""
    rows: Dict[float, List[Dict[str, Any]]] = {}
    for item in items:
        rows.setdefault(item["y"], []).append(item)
    ordered = []
    for y in sorted(rows, reverse=True):  # PDF y grows upward: top row first
        cells = sorted(rows[y], key=lambda cell: cell["x"])
        text = " ".join(str(cell["text"]).strip() for cell in cells
                        if str(cell["text"]).strip())
        if text:
            ordered.append({"y": y, "text": text,
                            "cells": [cell["x"] for cell in cells]})
    return ordered


def _pdf_table_like(rows: List[Dict[str, Any]]) -> bool:
    """Heuristic FLAG (not a semantic claim): >=3 rows and >=2 multi-cell
    rows sharing >=2 aligned cell x-starts — the visual signature of a
    table region. Flagged for downstream review, never parsed into data."""
    multi = [row for row in rows if len(row["cells"]) >= 2]
    if len(rows) < 3 or len(multi) < 2:
        return False
    aligned_pairs = 0
    for i in range(len(multi)):
        for j in range(i + 1, len(multi)):
            shared = ({round(v) for v in multi[i]["cells"]}
                      & {round(v) for v in multi[j]["cells"]})
            if len(shared) >= 2:
                aligned_pairs += 1
    return aligned_pairs >= max(1, len(multi) - 1)


def extract_pdf_text(data: bytes) -> Dict[str, Any]:
    """Text-layer PDF extraction for the stdlib subset; explicit refusals.

    ok / needs_ocr / encrypted / corrupt / empty / unsupported_pdf_feature.
    The ok artifact carries per-page text + hashes, a table_like flag per
    page, and honest accounting notes (cp1252 replacements, page-order
    method). `text` joins the page texts with newlines — deterministic, so
    excerpt addressing over it is stable."""
    import hashlib as _hashlib

    base: Dict[str, Any] = {"parser_version": PDF_TEXT_PARSER_VERSION}
    if b"/Encrypt" in data:
        return {**base, "status": "encrypted",
                "reason": "encrypted pdf requires a password; refusing to guess"}
    if any(marker in data for marker in _PDF_CID_MARKERS):
        return {**base, "status": "unsupported_pdf_feature",
                "reason": "CID/Type0 fonts are outside the simple-font text subset"}
    if b"/ObjStm" in data:
        return {**base, "status": "unsupported_pdf_feature",
                "reason": "compressed object streams need a fuller parser"}
    notes: Dict[str, int] = {}
    try:
        objects = _pdf_scan_objects(data)
        page_order_note = "pages_tree"
        page_ids: List[int] = []
        for obj in objects.values():
            if _PDF_PAGES_TYPE.search(obj["dict"]):
                kids_match = _PDF_KIDS.search(obj["dict"])
                if kids_match:
                    page_ids = [int(ref) for ref in _PDF_REF.findall(kids_match.group(1))]
                break
        valid_page_ids: List[int] = []
        if page_ids:
            for page_id in page_ids:
                page_obj = objects.get(page_id)
                if page_obj is None:
                    continue
                if _PDF_PAGES_TYPE.search(page_obj["dict"]):
                    return {**base, "status": "unsupported_pdf_feature",
                            "reason": "nested page trees need a fuller parser"}
                if _PDF_PAGE_TYPE.search(page_obj["dict"]):
                    valid_page_ids.append(page_id)
        else:
            valid_page_ids = sorted(num for num, obj in objects.items()
                                    if _PDF_PAGE_TYPE.search(obj["dict"]))
            page_order_note = "object_scan"
        contents: List[bytes] = []
        if valid_page_ids:
            for page_id in valid_page_ids:
                match = _PDF_CONTENTS.search(objects[page_id]["dict"])
                if match is None:
                    continue
                refs = ([int(ref) for ref in _PDF_REF.findall(match.group(1))]
                        if b"[" in match.group(1)
                        else [int(match.group(1).split()[0])])
                for ref in refs:
                    target = objects.get(ref)
                    if target is not None:
                        contents.append(_pdf_stream_bytes(target))
        else:
            # Degenerate input with no object structure at all (a bare content
            # fragment): parse the whole payload as one anonymous stream. The
            # artifact says so — a documented fallback, not a silent guess.
            page_order_note = "raw_fallback"
            contents = [data]
        if not contents:
            return {**base, "status": "needs_ocr",
                    "reason": "pdf has no readable page content streams"}
        pages: List[Dict[str, Any]] = []
        for number, stream in enumerate(contents, start=1):
            tokens = _pdf_tokenize(stream.decode("latin-1"))
            items = _pdf_page_items(tokens, notes)
            rows = _pdf_rows(items)
            text = "\n".join(row["text"] for row in rows)
            pages.append({
                "page": number,
                "text": text,
                "char_count": len(text),
                "text_sha256": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "row_count": len(rows),
                "table_like": _pdf_table_like(rows),
            })
        if not any(page["text"] for page in pages):
            return {**base, "status": "empty",
                    "reason": "text layer present but no decodable text was extracted"}
        return {
            **base,
            "status": "ok",
            "text": "\n".join(page["text"] for page in pages),
            "char_count": sum(page["char_count"] for page in pages),
            "page_count": len(pages),
            "pages": pages,
            "notes": {"encoding": "winansi-cp1252",
                      "page_order": page_order_note,
                      **({"accounting": notes} if notes else {})},
        }
    except _PdfRefusal as refusal:
        return {**base, "status": refusal.status, "reason": refusal.reason}

