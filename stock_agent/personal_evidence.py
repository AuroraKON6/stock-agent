"""R12a: personal evidence namespace — isolated owner directory and index.

Personal research runs on the SAME verification machinery as the institutional
vault (hash-addressed objects, content-type whitelist, chain verification) but
in a SEPARATE root, index and ledger, under a namespace bound to ONE real
owner. Sharing code never shares approval state:

- A personal document is NEVER visible to the institutional projection, and
  an institutional document is never visible here — the two namespaces cannot
  even address each other's objects (different roots).
- Moving a personal document into an institutional case is a separate,
  explicit admission step that this module does NOT provide (institutional
  gates stay exactly as strict as before).
- Imports record the R10 policy fields: real owner, source URL, namespace
  marker, retention/external-send policy, and an append-only audit event.

The reuse strategy is a config VIEW: ``personal_vault_view`` returns a config
whose vault paths point at the personal root — every downstream check
(object hashing, content-type whitelist, chain verification, excerpts, facts)
runs the production code against the personal namespace unchanged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import project_path
from .evidence_vault import evidence_vault_config, import_official_document

PERSONAL_NAMESPACE = "personal_research"
PERSONAL_ROOT_DEFAULT = "data/personal_evidence"
_PERSONAL_ROOT_OVERRIDE = {"personal_evidence": "root"}

# Personal import policy (fixed for the first case; widening needs a review):
# retained for the owner's own review only, never sent to external models
# from this namespace (R13 fact bundles decide egress separately).
PERSONAL_RETENTION_POLICY = "owner_review_only"
PERSONAL_EXTERNAL_SEND_POLICY = False


class PersonalEvidenceError(ValueError):
    pass


def personal_root(config: Dict[str, Any], owner: str) -> Path:
    owner = str(owner or "").strip()
    if not owner:
        raise PersonalEvidenceError("personal namespace requires a real owner actor")
    root_setting = str(((config or {}).get("personal_evidence") or {}).get("root")
                       or PERSONAL_ROOT_DEFAULT)
    base = project_path(root_setting)
    # Owner directory is a hash, never a raw actor string (paths must stay
    # safe whatever characters an actor id contains).
    owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    return base / owner_key


def personal_vault_view(config: Dict[str, Any], owner: str) -> Dict[str, Any]:
    """Config VIEW whose vault paths point at one owner's personal root.

    Everything downstream (import, verification, excerpts, facts) runs the
    production code against these paths — no forked logic to drift."""
    root = personal_root(config, owner)
    cfg = evidence_vault_config(config)
    view = dict(config or {})
    view["official_evidence_vault"] = {
        **{key: value for key, value in cfg.items() if key not in
           ("events_path", "index_path", "objects_dir", "quarantine_dir", "report_dir")},
        "events_path": str(root / "events.jsonl"),
        "index_path": str(root / "index.json"),
        "objects_dir": str(root / "objects" / "sha256"),
        "quarantine_dir": str(root / "quarantine" / "sha256"),
        "report_dir": str(root / "reports"),
    }
    return view


def import_personal_document(config: Dict[str, Any], owner: str, file_path: str, *,
                             source_url: str, title: str, published_at: str,
                             content_type: str = "") -> Dict[str, Any]:
    """Import one document into the OWNER's personal namespace.

    The owner must be a real human actor (R10 rule: automated/service
    identities can never own personal evidence). The vault import machinery
    enforces its own capture-actor policy on top; here we bind the owner into
    the audit trail and stamp the namespace/policy fields."""
    owner = str(owner or "").strip()
    if not owner:
        raise PersonalEvidenceError("personal import requires a real owner")
    from .research_runtime import AUTOMATED_ACTOR_PREFIXES

    lowered = owner.lower()
    if lowered.startswith(AUTOMATED_ACTOR_PREFIXES) or any(
            marker in lowered for marker in ("dify:", "svc-", "service", "system:")):
        raise PersonalEvidenceError(
            "automated/service identities cannot own personal evidence")
    view = personal_vault_view(config, owner)
    # The vault machinery keeps its INSTITUTIONAL capture-role whitelist
    # unchanged (taskbook: existing roster/role gates stay valid); personal
    # imports use the plain evidence-collection role, and the OWNER binding
    # lives in the namespace stamp below — never in the role claim.
    result = import_official_document(
        view, file_path,
        source_url=source_url, title=title, published_at=published_at,
        actor=owner, actor_role="information_analyst",
        content_type=content_type)
    document = result.get("document") or result
    stamp = {
        "record_type": "personal_import",
        "namespace": PERSONAL_NAMESPACE,
        "owner": owner,
        "document_id": str(document.get("document_id") or ""),
        "document_sha256": str(document.get("sha256") or ""),
        "source_url": source_url,
        "title": title,
        "retention_policy": PERSONAL_RETENTION_POLICY,
        "external_send_allowed": PERSONAL_EXTERNAL_SEND_POLICY,
        "institutional_admission": "NOT_REQUESTED",
    }
    audit_path = personal_root(config, owner) / "imports.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stamp, ensure_ascii=False, sort_keys=True,
                                default=str) + "\n")
    return {"document": document, "namespace_stamp": stamp}


def personal_documents(config: Dict[str, Any], owner: str) -> Dict[str, List[Dict[str, Any]]]:
    """Read-only namespace projection: documents grouped by kind marker.

    Returns the raw verified projection of the OWNER's personal view — never
    merged with the institutional projection."""
    from .evidence_context import verified_evidence_documents

    view = personal_vault_view(config, owner)
    return {"owner": owner, "namespace": PERSONAL_NAMESPACE,
            "documents": list(verified_evidence_documents(view).values())}


def personal_import_audit(config: Dict[str, Any], owner: str) -> List[Dict[str, Any]]:
    audit_path = personal_root(config, owner) / "imports.jsonl"
    if not audit_path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
