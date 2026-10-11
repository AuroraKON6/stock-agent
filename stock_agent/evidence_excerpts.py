"""Local-only exact excerpts and unapproved summary lineage; no ledger writes."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from .config import project_path
from .evidence_context import verified_evidence_documents
from .evidence_vault import evidence_vault_config

EXTRACTOR_VERSION = "utf8-codepoints-v1"
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_EXCERPT_CHARS = 1200
SELECTOR_FIELDS = (
    "document_id", "document_sha256", "extractor_version", "source_text_sha256",
    "start", "end", "excerpt_sha256",
)


class EvidenceExcerptError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _document_text(config: Dict[str, Any], document: Dict[str, Any]) -> str:
    if document.get("content_type") != "text/plain":
        raise EvidenceExcerptError("unsupported format: only retained UTF-8 text/plain is supported")
    cfg = evidence_vault_config(config)
    sha = document["sha256"]
    root = project_path(cfg["objects_dir"])
    expected = root / sha[:2] / sha
    path = project_path(str(document.get("object_path") or ""))
    if path.resolve() != expected.resolve():
        raise EvidenceExcerptError("object locator is not the canonical vault path")
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise EvidenceExcerptError("reparse point in evidence object path")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_TEXT_BYTES + 1)
    except OSError as exc:
        raise EvidenceExcerptError("retained object cannot be read") from exc
    if len(data) > MAX_TEXT_BYTES:
        raise EvidenceExcerptError("document exceeds local extraction size limit")
    # Rehash the bytes actually decoded, not a cached verification result.
    if len(data) != document.get("byte_size") or hashlib.sha256(data).hexdigest() != sha:
        raise EvidenceExcerptError("retained bytes changed after vault verification")
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise EvidenceExcerptError("document is not strict UTF-8 text") from exc
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise EvidenceExcerptError("document contains unsupported control characters")
    return text


def create_excerpt(config: Dict[str, Any], document_id: str, start: int, end: int) -> Dict[str, Any]:
    """Select [start, end) Unicode code points after UTF-8 BOM removal only."""
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (start, end)):
        raise EvidenceExcerptError("excerpt offsets must be integers")
    if start < 0 or end <= start or end - start > MAX_EXCERPT_CHARS:
        raise EvidenceExcerptError("invalid or oversized excerpt range")
    document = next((doc for doc in verified_evidence_documents(config).values()
                     if doc.get("document_id") == document_id), None)
    if document is None:
        raise EvidenceExcerptError("document is not currently verified and retained")
    text = _document_text(config, document)
    if end > len(text):
        raise EvidenceExcerptError("excerpt range is outside the retained text")
    excerpt = text[start:end]
    selector = {
        "document_id": document_id, "document_sha256": document["sha256"],
        "extractor_version": EXTRACTOR_VERSION,
        "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "start": start, "end": end,
        "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }
    return {
        **selector, "excerpt_id": "excerpt:sha256:" + _digest(selector),
        "text": excerpt, "content_kind": "source_statement",
        "source_url": str((document.get("source_urls") or [""])[-1]),
        "document_title": str(document.get("title") or ""),
        "line_start": text.count("\n", 0, start) + 1,
        "line_end": text.count("\n", 0, end - 1) + 1,
        "scope": "local_review_only", "review_status": "unreviewed",
        "external_eligible": False,
    }


def excerpt_selector(excerpt: Dict[str, Any]) -> Dict[str, Any]:
    return {field: excerpt[field] for field in SELECTOR_FIELDS}


def verify_excerpt_selection(config: Dict[str, Any], selector: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(selector, dict) or set(selector) != set(SELECTOR_FIELDS):
        raise EvidenceExcerptError("excerpt selector has missing or unexpected fields")
    result = create_excerpt(config, selector["document_id"], selector["start"], selector["end"])
    if excerpt_selector(result) != selector:
        raise EvidenceExcerptError("excerpt selector does not match retained source/version/hash")
    return result


def bind_summary_draft(config: Dict[str, Any], selectors: List[Dict[str, Any]],
                       text: str, kind: str) -> Dict[str, Any]:
    """Bind a draft's provenance, never certify its semantic support or author."""
    if not isinstance(kind, str) or kind not in {"human_judgment", "model_inference"}:
        raise EvidenceExcerptError("summary kind must be human_judgment or model_inference")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise EvidenceExcerptError("summary draft must contain 1..4000 characters")
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 5:
        raise EvidenceExcerptError("summary requires 1..5 excerpt selectors")
    excerpts = [verify_excerpt_selection(config, selection) for selection in selectors]
    sources = sorted({excerpt["excerpt_id"] for excerpt in excerpts})
    version = {"schema_version": "1.0", "text": text, "content_kind": kind, "source_excerpt_ids": sources}
    return {**version, "summary_id": "summary-draft:sha256:" + _digest(version),
            "summary_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "review_status": "unreviewed", "semantic_support_verified": False,
            "scope": "local_review_only", "external_eligible": False}
