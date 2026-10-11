"""Shared, authoritative evidence-vault context for supply and the worker.

Single source of truth for "which evidence documents may reach a model":
the vault's own verification result (chain, object bytes, hash, size) —
never a hand-rolled index scan. A document is deliverable only when the
WHOLE vault verifies READY (any missing/tampered object blocks delivery
fail-closed) and the individual document is captured (not quarantined)
with a valid publication timestamp.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from .evidence_vault import load_evidence_vault, verify_evidence_vault

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def verified_evidence_documents(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return {sha256: document} for vault documents verified by the vault itself."""

    verification = verify_evidence_vault(config or {})
    if not verification.get("enabled"):
        return {}
    if verification.get("integrity_status") != "OK" or verification.get("technical_status") != "READY":
        # Missing or tampered objects anywhere block ALL delivery: fail closed
        # instead of silently delivering an unverifiable subset.
        return {}
    vault = load_evidence_vault(config or {})
    deliverable: Dict[str, Dict[str, Any]] = {}
    for document in (vault.get("documents") or {}).values():
        if not isinstance(document, dict):
            continue
        if str(document.get("status") or "") != "captured":
            continue
        if not document.get("published_at_valid"):
            continue
        sha = str(document.get("sha256") or "")
        if _SHA256_RE.fullmatch(sha):
            deliverable[sha] = document
    return deliverable


def verified_evidence_index(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return {document_id: entry} summaries for supply, verified as above."""

    documents = verified_evidence_documents(config or {})
    entries: Dict[str, Dict[str, Any]] = {}
    for sha, document in documents.items():
        document_id = str(document.get("document_id") or "document:sha256:{0}".format(sha))
        entries[document_id] = {
            "signal_ids": list(document.get("signal_ids") or []),
            "summary": str(document.get("title") or "")[:1000] or "vault document",
            "sha256": sha,
            "source_url": str((document.get("source_urls") or [""])[-1] or ""),
            "published_at": str(document.get("published_at") or ""),
        }
    return entries
