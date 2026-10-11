from __future__ import annotations

import json
import mimetypes
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .audit_log import append_hash_chained_jsonl, atomic_write_text, file_hash, load_hash_chained_jsonl, object_hash
from .config import project_path
from .models import now_iso
from .research_operations import load_operations_inputs


CONTROL_EVIDENCE_VERSION = "29.0"
CONTROL_EVIDENCE_EVENT_TYPES = {
    "control_evidence_registered",
    "control_evidence_review_recorded",
    "control_evidence_revoked",
}
DEFAULT_CATEGORY_REVIEW_ROLES = {
    "governance_mandate": ["compliance_officer"],
    "internal_research_scope": ["compliance_officer"],
    "prohibited_actions_policy": ["compliance_officer", "information_security_officer"],
    "human_employment": ["research_coordinator"],
    "human_qualification": ["compliance_officer"],
    "human_confidentiality": ["compliance_officer"],
    "human_training": ["compliance_officer"],
    "role_appointment": ["research_coordinator"],
    "provider_legal_contract": ["compliance_officer"],
    "provider_data_rights": ["data_steward", "compliance_officer"],
    "provider_security": ["information_security_officer"],
    "provider_operations": ["research_coordinator"],
    "production_archive_contract": ["compliance_officer"],
    "production_archive_security": ["information_security_officer"],
    "production_archive_retention": ["compliance_officer"],
    "approval_support": ["compliance_officer"],
}
DEFAULT_EXPIRING_CATEGORIES = {
    "human_qualification",
    "human_training",
    "provider_data_rights",
    "provider_security",
    "provider_operations",
}
ROLE_AUTHORIZATIONS = {
    "compliance_officer": "compliance_authorized",
    "information_security_officer": "information_security_authorized",
    "data_steward": "vendor_data_steward_authorized",
    "research_coordinator": "research_coordination_authorized",
}
SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=])", re.IGNORECASE)
WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
SAFE_ARCHIVE_SCHEMES = {"archive", "dms", "records", "https"}


class ControlEvidencePolicyError(ValueError):
    pass


def control_evidence_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("business_onboarding", {}) if isinstance(config, dict) else {}
    category_roles = raw.get("control_evidence_review_roles", DEFAULT_CATEGORY_REVIEW_ROLES)
    if not isinstance(category_roles, dict):
        category_roles = dict(DEFAULT_CATEGORY_REVIEW_ROLES)
    normalized_roles = {
        str(category).strip(): _clean_list(roles)
        for category, roles in category_roles.items()
        if str(category).strip() and _clean_list(roles)
    }
    if not normalized_roles:
        normalized_roles = {key: list(value) for key, value in DEFAULT_CATEGORY_REVIEW_ROLES.items()}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce": bool(raw.get("enforce", False)),
        "event_path": str(raw.get("control_evidence_event_path") or "data/business_onboarding/control_evidence_events.jsonl"),
        "report_dir": str(raw.get("report_dir") or "reports/business_onboarding"),
        "category_review_roles": normalized_roles,
        "expiring_categories": set(_clean_list(raw.get("control_evidence_expiring_categories", DEFAULT_EXPIRING_CATEGORIES))),
        "max_file_bytes": max(1, int(raw.get("control_evidence_max_file_bytes", 100 * 1024 * 1024) or 100 * 1024 * 1024)),
        "allowed_archive_schemes": set(_clean_list(raw.get("allowed_control_archive_schemes", SAFE_ARCHIVE_SCHEMES))),
    }


def register_control_evidence(
    config: Dict[str, Any],
    *,
    file_path: str,
    category: str,
    title: str,
    subject_id: str,
    owner_role: str,
    external_archive_ref: str,
    actor: str,
    actor_role: str,
    valid_from: str = "",
    valid_to: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    cfg = control_evidence_config(config)
    category = str(category or "").strip()
    if category not in cfg["category_review_roles"]:
        raise ControlEvidencePolicyError("unsupported control evidence category: {0}".format(category))
    source = Path(file_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ControlEvidencePolicyError("control evidence source file does not exist")
    size = source.stat().st_size
    if size <= 0 or size > cfg["max_file_bytes"]:
        raise ControlEvidencePolicyError("control evidence file size is outside the allowed range")
    title = str(title or "").strip()
    subject_id = str(subject_id or "").strip()
    owner_role = str(owner_role or "").strip()
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    if not all((title, subject_id, owner_role, actor, actor_role)):
        raise ControlEvidencePolicyError("title, subject_id, owner_role, actor, and actor_role are required")
    _authorize_human(config, actor, actor_role)
    _reject_secret_text([title, subject_id, owner_role, actor, actor_role, notes])
    archive_ref = _validate_archive_ref(external_archive_ref, cfg)
    start = _normalize_time(valid_from, allow_empty=True)
    end = _normalize_time(valid_to, allow_empty=True)
    if category in cfg["expiring_categories"] and not end:
        raise ControlEvidencePolicyError("valid_to is required for expiring control evidence")
    if start and end and _parse_time(end) <= _parse_time(start):
        raise ControlEvidencePolicyError("valid_to must be later than valid_from")
    digest = file_hash(source)
    evidence_id = "control:{0}".format(object_hash({
        "category": category,
        "subject_id": subject_id,
        "sha256": digest,
        "external_archive_ref": archive_ref,
    })[:24])
    registry = load_control_evidence_registry(config)
    if evidence_id in (registry.get("evidence") or {}):
        raise ControlEvidencePolicyError("control evidence is already registered: {0}".format(evidence_id))
    event = _append_event(config, {
        "event_type": "control_evidence_registered",
        "evidence_id": evidence_id,
        "actor": actor,
        "actor_role": actor_role,
        "payload": {
            "category": category,
            "title": title,
            "subject_id": subject_id,
            "owner_role": owner_role,
            "external_archive_ref": archive_ref,
            "sha256": digest,
            "byte_size": size,
            "media_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "valid_from": start,
            "valid_to": end,
            "notes": str(notes or "").strip(),
            "required_review_roles": list(cfg["category_review_roles"][category]),
            "raw_file_copied_to_project": False,
            "approval_granted": False,
        },
    })
    snapshot = load_control_evidence_registry(config)
    snapshot.update(write_control_evidence_snapshot(snapshot))
    return {"record": event, "registry": snapshot}


def review_control_evidence(
    config: Dict[str, Any],
    *,
    evidence_id: str,
    decision: str,
    actor: str,
    actor_role: str,
    notes: str,
    review_record_ref: str,
) -> Dict[str, Any]:
    cfg = control_evidence_config(config)
    decision = str(decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise ControlEvidencePolicyError("decision must be approve or reject")
    evidence_id = str(evidence_id or "").strip()
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    registry = load_control_evidence_registry(config)
    item = _require_evidence(registry, evidence_id)
    if actor_role not in (item.get("required_review_roles") or []):
        raise ControlEvidencePolicyError("actor role is not required for this evidence category")
    _authorize_human(config, actor, actor_role)
    if actor == str(item.get("registered_by") or "") or actor == str(item.get("subject_id") or ""):
        raise ControlEvidencePolicyError("control evidence reviewer must be independent from preparer and subject")
    for role, review in (item.get("reviews") or {}).items():
        if role != actor_role and str(review.get("actor") or "") == actor:
            raise ControlEvidencePolicyError("required control evidence reviews must use distinct human actors")
    if not str(notes or "").strip():
        raise ControlEvidencePolicyError("review notes are required")
    _reject_secret_text([actor, actor_role, notes])
    review_ref = _validate_archive_ref(review_record_ref, cfg)
    event = _append_event(config, {
        "event_type": "control_evidence_review_recorded",
        "evidence_id": evidence_id,
        "actor": actor,
        "actor_role": actor_role,
        "payload": {
            "decision": decision,
            "notes": str(notes or "").strip(),
            "review_record_ref": review_ref,
            "approval_granted": decision == "approve",
        },
    })
    snapshot = load_control_evidence_registry(config)
    snapshot.update(write_control_evidence_snapshot(snapshot))
    return {"record": event, "registry": snapshot}


def revoke_control_evidence(
    config: Dict[str, Any],
    *,
    evidence_id: str,
    actor: str,
    actor_role: str,
    notes: str,
) -> Dict[str, Any]:
    if actor_role not in {"compliance_officer", "information_security_officer", "data_steward"}:
        raise ControlEvidencePolicyError("actor role is not allowed to revoke control evidence")
    _authorize_human(config, actor, actor_role)
    _require_evidence(load_control_evidence_registry(config), evidence_id)
    if not str(notes or "").strip():
        raise ControlEvidencePolicyError("revocation notes are required")
    _reject_secret_text([actor, actor_role, notes])
    event = _append_event(config, {
        "event_type": "control_evidence_revoked",
        "evidence_id": str(evidence_id or "").strip(),
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {"notes": str(notes or "").strip(), "approval_granted": False},
    })
    snapshot = load_control_evidence_registry(config)
    snapshot.update(write_control_evidence_snapshot(snapshot))
    return {"record": event, "registry": snapshot}


def load_control_evidence_registry(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = control_evidence_config(config)
    log = load_hash_chained_jsonl(project_path(cfg["event_path"]))
    evidence = _project_evidence(log["records"] if log["integrity_status"] == "OK" else [], cfg)
    counts = Counter(str(item.get("status") or "unknown") for item in evidence.values())
    return {
        "version": CONTROL_EVIDENCE_VERSION,
        "generated_at": now_iso(),
        "technical_status": "READY" if log["integrity_status"] == "OK" else "FAIL",
        "integrity_status": log["integrity_status"],
        "integrity_errors": log["errors"],
        "event_count": log["record_count"],
        "last_event_hash": log["last_hash"],
        "evidence_count": len(evidence),
        "status_counts": dict(sorted(counts.items())),
        "verified_evidence_ids": sorted(key for key, item in evidence.items() if item.get("status") == "verified"),
        "evidence": evidence,
        "event_path": str(project_path(cfg["event_path"])),
        "report_dir": cfg["report_dir"],
        "raw_files_stored": False,
        "automatic_approval_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }


def resolve_verified_control_evidence(
    config: Dict[str, Any],
    evidence_id: str,
    *,
    categories: Optional[Sequence[str]] = None,
    subject_id: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    registry = load_control_evidence_registry(config)
    if registry.get("integrity_status") != "OK":
        return False, "control evidence registry integrity failed", {}
    item = (registry.get("evidence") or {}).get(str(evidence_id or "").strip())
    if not item:
        return False, "control evidence reference was not found", {}
    if item.get("status") != "verified":
        return False, "control evidence is not verified", item
    allowed = set(_clean_list(categories or []))
    if allowed and item.get("category") not in allowed:
        return False, "control evidence category is not allowed", item
    if subject_id and str(item.get("subject_id") or "") != str(subject_id):
        return False, "control evidence subject does not match", item
    return True, "verified", item


def validate_managed_archive_reference(config: Dict[str, Any], value: str) -> str:
    return _validate_archive_ref(value, control_evidence_config(config))


def write_control_evidence_snapshot(registry: Dict[str, Any]) -> Dict[str, str]:
    report_dir = project_path(str(registry.get("report_dir") or "reports/business_onboarding"))
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "control_evidence_latest.json"
    md_path = report_dir / "control_evidence_latest.md"
    atomic_write_text(json_path, json.dumps(registry, ensure_ascii=False, indent=2, default=str))
    atomic_write_text(md_path, render_control_evidence_markdown(registry), encoding="utf-8-sig")
    return {
        "control_evidence_json_path": str(json_path),
        "control_evidence_path": str(md_path),
    }


def render_control_evidence_markdown(registry: Dict[str, Any]) -> str:
    lines = [
        "# Internal Control Evidence Registry",
        "",
        "- technical: {0}".format(registry.get("technical_status", "UNKNOWN")),
        "- integrity: {0}".format(registry.get("integrity_status", "UNKNOWN")),
        "- evidence / verified: {0} / {1}".format(
            registry.get("evidence_count", 0), len(registry.get("verified_evidence_ids", []))
        ),
        "- raw files stored in project: false",
        "- automatic approval / publication / trading: false / false / false",
        "",
        "| ID | Category | Subject | Status | Valid to | Reviews | External archive |",
        "|---|---|---|---|---|---|---|",
    ]
    for evidence_id, item in sorted((registry.get("evidence") or {}).items()):
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
            evidence_id,
            item.get("category", ""),
            item.get("subject_id", ""),
            item.get("status", ""),
            item.get("valid_to", "") or "-",
            ", ".join(sorted((item.get("reviews") or {}).keys())) or "-",
            item.get("external_archive_ref", ""),
        ))
    if not registry.get("evidence"):
        lines.append("| - | - | - | empty | - | - | Register real externally archived evidence; do not upload raw sensitive files. |")
    if registry.get("integrity_errors"):
        lines.extend(["", "## Integrity Errors", ""])
        for error in registry["integrity_errors"]:
            lines.append("- line {0}: {1}".format(error.get("line", ""), error.get("reason", "")))
    return "\n".join(lines).rstrip() + "\n"


def _project_evidence(events: Iterable[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    for event in events:
        evidence_id = str(event.get("evidence_id") or "").strip()
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not evidence_id or event_type not in CONTROL_EVIDENCE_EVENT_TYPES:
            continue
        if event_type == "control_evidence_registered":
            if evidence_id in items:
                continue
            items[evidence_id] = {
                "evidence_id": evidence_id,
                "category": str(payload.get("category") or ""),
                "title": str(payload.get("title") or ""),
                "subject_id": str(payload.get("subject_id") or ""),
                "owner_role": str(payload.get("owner_role") or ""),
                "external_archive_ref": str(payload.get("external_archive_ref") or ""),
                "sha256": str(payload.get("sha256") or ""),
                "byte_size": int(payload.get("byte_size", 0) or 0),
                "media_type": str(payload.get("media_type") or ""),
                "valid_from": str(payload.get("valid_from") or ""),
                "valid_to": str(payload.get("valid_to") or ""),
                "required_review_roles": _clean_list(payload.get("required_review_roles", [])),
                "registered_at": str(event.get("recorded_at") or ""),
                "registered_by": str(event.get("actor") or ""),
                "registered_by_role": str(event.get("actor_role") or ""),
                "reviews": {},
                "revocation": {},
            }
        elif evidence_id in items and event_type == "control_evidence_review_recorded":
            role = str(event.get("actor_role") or "")
            items[evidence_id]["reviews"][role] = {
                "decision": str(payload.get("decision") or ""),
                "actor": str(event.get("actor") or ""),
                "recorded_at": str(event.get("recorded_at") or ""),
                "notes": str(payload.get("notes") or ""),
                "review_record_ref": str(payload.get("review_record_ref") or ""),
            }
        elif evidence_id in items and event_type == "control_evidence_revoked":
            items[evidence_id]["revocation"] = {
                "actor": str(event.get("actor") or ""),
                "actor_role": str(event.get("actor_role") or ""),
                "recorded_at": str(event.get("recorded_at") or ""),
                "notes": str(payload.get("notes") or ""),
            }
    now = datetime.now(timezone.utc)
    for item in items.values():
        item["status"] = _evidence_status(item, now)
    return items


def _evidence_status(item: Dict[str, Any], now: datetime) -> str:
    if item.get("revocation"):
        return "revoked"
    valid_from = _parse_time(str(item.get("valid_from") or ""), allow_empty=True)
    if valid_from and valid_from > now:
        return "not_yet_valid"
    valid_to = _parse_time(str(item.get("valid_to") or ""), allow_empty=True)
    if valid_to and valid_to <= now:
        return "expired"
    reviews = item.get("reviews") or {}
    required = item.get("required_review_roles") or []
    if any(str(review.get("decision") or "") == "reject" for review in reviews.values()):
        return "rejected"
    if required and all((reviews.get(role) or {}).get("decision") == "approve" for role in required):
        actors = [str((reviews.get(role) or {}).get("actor") or "") for role in required]
        if all(actors) and len(set(actors)) == len(actors):
            return "verified"
    return "under_review" if reviews else "registered"


def _append_event(config: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    cfg = control_evidence_config(config)
    value = dict(event)
    if value.get("event_type") not in CONTROL_EVIDENCE_EVENT_TYPES:
        raise ControlEvidencePolicyError("unsupported control evidence event")
    value["schema_version"] = CONTROL_EVIDENCE_VERSION
    value["recorded_at"] = now_iso()
    return append_hash_chained_jsonl(project_path(cfg["event_path"]), value)


def _authorize_human(config: Dict[str, Any], actor: str, actor_role: str) -> Dict[str, Any]:
    inputs = load_operations_inputs(config)
    if inputs.get("errors"):
        raise ControlEvidencePolicyError("research roster is invalid: {0}".format("; ".join(inputs["errors"][:5])))
    principal = (inputs.get("principal_map") or {}).get(str(actor or "").strip())
    if not principal:
        raise ControlEvidencePolicyError("actor is not registered in the research operations roster")
    if principal.get("principal_type") != "human" or not principal.get("active"):
        raise ControlEvidencePolicyError("actor must be an active human principal")
    if actor_role not in (principal.get("roles") or []):
        raise ControlEvidencePolicyError("actor is not registered for role {0}".format(actor_role))
    required = ROLE_AUTHORIZATIONS.get(actor_role)
    if required and required not in (principal.get("authorizations") or []):
        raise ControlEvidencePolicyError("actor lacks required authorization: {0}".format(required))
    return principal


def _require_evidence(registry: Dict[str, Any], evidence_id: str) -> Dict[str, Any]:
    if registry.get("integrity_status") != "OK":
        raise ControlEvidencePolicyError("control evidence registry integrity check failed")
    item = (registry.get("evidence") or {}).get(str(evidence_id or "").strip())
    if not item:
        raise ControlEvidencePolicyError("control evidence not found: {0}".format(evidence_id))
    if item.get("status") in {"revoked", "expired"}:
        raise ControlEvidencePolicyError("control evidence is not reviewable in status {0}".format(item.get("status")))
    return item


def _validate_archive_ref(value: str, cfg: Dict[str, Any]) -> str:
    text = str(value or "").strip()
    if not text or SECRET_PATTERN.search(text) or WINDOWS_PATH_PATTERN.match(text) or text.startswith(("/", "\\")):
        raise ControlEvidencePolicyError("external archive reference must be a non-secret managed archive URI")
    parsed = urlparse(text)
    if parsed.scheme.lower() not in cfg["allowed_archive_schemes"]:
        raise ControlEvidencePolicyError("external archive reference scheme is not allowed")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ControlEvidencePolicyError(
            "external archive reference must not contain credentials, query parameters, or fragments"
        )
    if parsed.scheme.lower() == "https" and (not parsed.hostname or parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ControlEvidencePolicyError("external archive HTTPS reference must use a non-local host")
    if parsed.scheme.lower() != "https" and not (parsed.netloc or parsed.path):
        raise ControlEvidencePolicyError("external archive reference is incomplete")
    return text


def _reject_secret_text(values: Sequence[str]) -> None:
    if any(SECRET_PATTERN.search(str(value or "")) for value in values):
        raise ControlEvidencePolicyError("control evidence metadata must not contain API keys or secrets")


def _normalize_time(value: str, *, allow_empty: bool) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    parsed = _parse_time(text)
    return parsed.isoformat()


def _parse_time(value: str, allow_empty: bool = False) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text and allow_empty:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlEvidencePolicyError("invalid ISO datetime: {0}".format(text)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_list(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
