"""R13: immutable research fact bundles — host-policy egress, re-verified use.

The supply chain freezes ONE fact bundle per outbound research attempt
(GLM_REFACTOR_TASKBOOK R13):

- **Binding**: task id + input_version (R01 authority) + the verified evidence
  documents the facts came from + the policy inputs' hash.
- **Host policy decides egress, never a self-report** (A61): a fact that
  names a source-registry id is checked against the registry's CURRENT
  external-send right; a payload claiming ``classification=public`` changes
  nothing. Registry-denied facts are EXCLUDED with a recorded reason (A60).
- **Re-verification at every use** (A62): selectors re-run against the
  CURRENT verified vault projection; a revoked or unverified source drops
  the fact and invalidates any bundle that still references it.
- **Payload-hash linkage** (A63): the bundle records the exact outbound
  payload hash the attempt delivered, so the model context is provably the
  approved payload.

Raw document BYTES never enter the bundle or the model context — facts carry
verified excerpt text and locators only.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .config import project_path
from .evidence_context import verified_evidence_documents
from .evidence_excerpts import EvidenceExcerptError, verify_excerpt_selection
from .sources.registry import load_source_registry, registry_status

FACT_BUNDLE_SCHEMA = "1.0"
MAX_BUNDLE_FACTS = 50


class FactBundleError(ValueError):
    pass


def payload_digest(payload: Any) -> str:
    """Canonical payload hash (the single form used for A63 linkage)."""
    return _digest(payload)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def build_research_fact_bundle(config: Dict[str, Any], task: Dict[str, Any], *,
                               fact_selectors: Optional[List[Dict[str, Any]]] = None,
                               extra_facts: Optional[List[Dict[str, Any]]] = None,
                               now: Optional[str] = None) -> Dict[str, Any]:
    """Freeze the deliverable fact set for ONE research attempt.

    ``fact_selectors``: excerpt selectors (R12a) whose excerpt text becomes
    the fact content — each is re-verified against the CURRENT vault now.
    ``extra_facts``: fact records that may carry a ``source_id`` (a
    source-registry id) for host-side egress rights; their content is only
    delivered when the registry's CURRENT external-send right allows it.
    """
    task_id = str(task.get("task_id") or "")
    input_version = int(task.get("input_version") or 1)
    selectors = list(fact_selectors or [])[:MAX_BUNDLE_FACTS]
    records = list(extra_facts or [])[:MAX_BUNDLE_FACTS]
    if not selectors and not records:
        raise FactBundleError("a fact bundle needs at least one fact source")

    verified = verified_evidence_documents(config)
    registry = load_source_registry(config)
    evidence_seen: Dict[str, str] = {}
    delivered: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for selector in selectors:
        label = str(selector.get("document_id") or "unknown-document")
        try:
            excerpt = verify_excerpt_selection(config, selector)
        except EvidenceExcerptError as exc:
            excluded.append({"source": "excerpt", "ref": label,
                             "reason": "source_not_verified",
                             "detail": str(exc)[:300]})
            continue
        sha = str(excerpt["document_sha256"])
        document = verified.get(sha)
        if document is None:
            excluded.append({"source": "excerpt", "ref": label,
                             "reason": "source_not_verified",
                             "detail": "document not in the current verified projection"})
            continue
        evidence_seen[sha] = str(document.get("document_id") or label)
        delivered.append({
            "kind": "excerpt_statement",
            "fact_key": str(excerpt["excerpt_id"]),
            "content": str(excerpt["text"]),
            "document_sha256": sha,
            "locator": {"start": excerpt["start"], "end": excerpt["end"],
                        "extractor_version": excerpt["extractor_version"]},
            "egress_basis": "verified_excerpt_host_policy",
        })

    for record in records:
        if not isinstance(record, dict):
            excluded.append({"source": "fact_record", "ref": "?",
                             "reason": "malformed_record", "detail": "not an object"})
            continue
        fact_id = str(record.get("fact_id") or "")
        source_id = str(record.get("source_id") or "")
        self_reported = str(record.get("classification") or "")
        if source_id:
            status = registry_status(registry, source_id)
            if not status.get("external_send_allowed"):
                excluded.append({
                    "source": "fact_record", "ref": fact_id or source_id,
                    "reason": "external_send_not_allowed",
                    "detail": "host registry denies external send for {0}".format(source_id)})
                continue
            delivered.append({
                "kind": "registry_fact",
                "fact_key": fact_id,
                "content": str(record.get("statement") or record.get("content") or ""),
                "source_id": source_id,
                "egress_basis": "registry_external_send_allowed",
                # A self-reported classification is recorded as data, never
                # as the decision basis (A61).
                **({"self_reported_class_ignored": self_reported} if self_reported else {}),
            })
        else:
            excluded.append({
                "source": "fact_record", "ref": fact_id or "?",
                "reason": "no_host_policy_basis",
                "detail": "fact records require a registered source id for egress"})

    bundle_core = {
        "schema_version": FACT_BUNDLE_SCHEMA,
        "task_id": task_id,
        "input_version": input_version,
        "evidence_documents": [{"sha256": sha, "document_id": doc}
                               for sha, doc in sorted(evidence_seen.items())],
        "facts": delivered,
        "excluded": excluded,
        "policy": {"decision_source": "host_registry_and_vault",
                   "self_reported_classification_ignored": True},
        "created_at": now or "",
    }
    bundle_sha = _digest(bundle_core)
    return {**bundle_core, "bundle_id": "fb-task-{0}-v{1}-{2}".format(
        task_id[:24] or "x", input_version, bundle_sha[:12]),
        "bundle_sha256": bundle_sha,
        "outbound_payload_sha256": ""}  # bound at delivery time (A63)


def bind_outbound_payload(bundle: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """A63: freeze the EXACT payload hash this attempt delivers."""
    updated = dict(bundle)
    updated["outbound_payload_sha256"] = _digest(payload)
    return updated


def verify_research_fact_bundle(config: Dict[str, Any], bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Re-verify a bundle against CURRENT state (A62).

    Every bound evidence document must still sit in the verified vault
    projection; every registry-based fact must still hold its external-send
    right. Any lapse invalidates the WHOLE bundle — stale permissions never
    keep working."""
    if not isinstance(bundle, dict) or str(bundle.get("schema_version")) != FACT_BUNDLE_SCHEMA:
        return {"valid": False, "reason": "malformed_bundle"}
    core = {k: v for k, v in bundle.items()
            if k not in ("bundle_id", "bundle_sha256", "outbound_payload_sha256")}
    if _digest(core) != str(bundle.get("bundle_sha256") or ""):
        return {"valid": False, "reason": "bundle_hash_mismatch"}
    verified = verified_evidence_documents(config)
    for document in bundle.get("evidence_documents") or []:
        if str(document.get("sha256")) not in verified:
            return {"valid": False, "reason": "evidence_revoked_or_unverified",
                    "detail": "document {0} left the verified projection".format(
                        document.get("document_id") or document.get("sha256"))}
    registry = load_source_registry(config)
    for fact in bundle.get("facts") or []:
        source_id = str(fact.get("source_id") or "")
        if source_id and not registry_status(registry, source_id).get("external_send_allowed"):
            return {"valid": False, "reason": "registry_right_lapsed",
                    "detail": "source {0} no longer allows external send".format(source_id)}
    return {"valid": True, "reason": "ok"}
