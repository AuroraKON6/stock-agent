from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .audit_log import append_hash_chained_jsonl, atomic_write_text, load_hash_chained_jsonl
from .config import project_path
from .control_evidence import resolve_verified_control_evidence, validate_managed_archive_reference
from .models import now_iso
from .research_operations import load_operations_inputs


PROVIDER_REGISTRY_VERSION = "24.0"
PROVIDER_EVENT_TYPES = {
    "provider_candidate_created",
    "provider_due_diligence_submitted",
    "provider_review_recorded",
    "provider_activated",
    "provider_suspended",
    "provider_retired",
}
DEFAULT_REQUIRED_REVIEW_ROLES = ["compliance_officer", "information_security_officer"]
DEFAULT_REQUIRED_SCOPES = ["internal_research", "external_llm"]
DEFAULT_EVIDENCE_FIELDS = [
    "legal_entity_ref",
    "service_description_ref",
    "source_provenance_ref",
    "data_rights_license_ref",
    "contract_ref",
    "confidentiality_ref",
    "security_assessment_ref",
    "data_dictionary_ref",
    "timestamp_semantics_ref",
    "service_level_ref",
    "incident_response_ref",
    "continuity_exit_plan_ref",
    "external_model_use_ref",
]
ROLE_AUTHORIZATIONS = {
    "data_steward": "vendor_data_steward_authorized",
    "information_security_officer": "information_security_authorized",
    "compliance_officer": "compliance_authorized",
    "research_coordinator": "research_coordination_authorized",
}
PROVIDER_EVIDENCE_CATEGORIES = {
    "legal_entity_ref": ["provider_legal_contract"],
    "service_description_ref": ["provider_operations"],
    "source_provenance_ref": ["provider_data_rights"],
    "data_rights_license_ref": ["provider_data_rights"],
    "contract_ref": ["provider_legal_contract"],
    "confidentiality_ref": ["provider_legal_contract", "provider_security"],
    "security_assessment_ref": ["provider_security"],
    "data_dictionary_ref": ["provider_operations"],
    "timestamp_semantics_ref": ["provider_operations"],
    "service_level_ref": ["provider_operations"],
    "incident_response_ref": ["provider_security", "provider_operations"],
    "continuity_exit_plan_ref": ["provider_operations"],
    "external_model_use_ref": ["provider_data_rights", "provider_security"],
}
SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=])", re.IGNORECASE)


class ProviderRegistryPolicyError(ValueError):
    pass


def provider_registry_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("production_pilot", {}) if isinstance(config, dict) else {}
    fields = _clean_list(raw.get("required_provider_evidence", DEFAULT_EVIDENCE_FIELDS))
    roles = _clean_list(raw.get("required_provider_review_roles", DEFAULT_REQUIRED_REVIEW_ROLES))
    scopes = _clean_list(raw.get("required_provider_scopes", DEFAULT_REQUIRED_SCOPES))
    return {
        "enabled": bool(raw.get("enabled", False)),
        "event_path": str(raw.get("vendor_event_path") or "data/production_pilot/vendor_events.jsonl"),
        "report_dir": str(raw.get("report_dir") or "reports/production_pilot"),
        "required_evidence": fields or list(DEFAULT_EVIDENCE_FIELDS),
        "required_review_roles": roles or list(DEFAULT_REQUIRED_REVIEW_ROLES),
        "required_scopes": scopes or list(DEFAULT_REQUIRED_SCOPES),
        "activation_roles": _clean_list(raw.get("provider_activation_roles", ["data_steward", "research_coordinator"])),
        "suspension_roles": _clean_list(raw.get(
            "provider_suspension_roles",
            ["data_steward", "research_coordinator", "compliance_officer", "information_security_officer"],
        )),
    }


def initialize_provider_candidate(
    config: Dict[str, Any],
    provider_id: str,
    legal_name: str,
    service_scopes: Sequence[str],
    *,
    actor: str = "system:stock_agent",
    notes: str = "",
) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    provider_id = _provider_id(provider_id)
    legal_name = str(legal_name or "").strip()
    scopes = _clean_list(service_scopes)
    if not legal_name:
        raise ProviderRegistryPolicyError("legal_name is required")
    if not scopes:
        raise ProviderRegistryPolicyError("at least one service scope is required")
    registry = load_provider_registry(config)
    if provider_id in registry["providers"]:
        raise ProviderRegistryPolicyError("provider already exists: {0}".format(provider_id))
    record = _append_provider_event(config, {
        "event_type": "provider_candidate_created",
        "provider_id": provider_id,
        "actor": str(actor or "system:stock_agent").strip(),
        "actor_role": "system_candidate_intake",
        "payload": {
            "legal_name": legal_name,
            "service_scopes": scopes,
            "notes": str(notes or "").strip(),
            "approval_granted": False,
        },
    })
    return {"record": record, "registry": write_provider_registry_snapshot(build_provider_registry(config))}


def submit_provider_due_diligence(
    config: Dict[str, Any],
    provider_id: str,
    actor: str,
    actor_role: str,
    evidence: Dict[str, Any],
    *,
    notes: str,
) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    provider_id = _provider_id(provider_id)
    actor_role = str(actor_role or "").strip()
    if actor_role not in {"data_steward", "research_coordinator"}:
        raise ProviderRegistryPolicyError("due diligence submission requires data_steward or research_coordinator")
    _authorize_human(config, actor, actor_role)
    registry = load_provider_registry(config)
    provider = _require_provider(registry, provider_id)
    if provider["status"] in {"active", "retired"}:
        raise ProviderRegistryPolicyError("provider status does not allow due diligence submission")
    normalized = _validate_evidence(
        evidence, cfg["required_evidence"], config=config, provider_id=provider_id
    )
    if not str(notes or "").strip():
        raise ProviderRegistryPolicyError("due diligence notes are required")
    record = _append_provider_event(config, {
        "event_type": "provider_due_diligence_submitted",
        "provider_id": provider_id,
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {
            "evidence": normalized,
            "notes": str(notes or "").strip(),
            "approval_granted": False,
        },
    })
    return {"record": record, "registry": write_provider_registry_snapshot(build_provider_registry(config))}


def record_provider_review(
    config: Dict[str, Any],
    provider_id: str,
    decision: str,
    actor: str,
    actor_role: str,
    *,
    notes: str,
    evidence_refs: Sequence[str],
) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    provider_id = _provider_id(provider_id)
    decision = str(decision or "").strip().lower()
    actor_role = str(actor_role or "").strip()
    if decision not in {"approve", "reject"}:
        raise ProviderRegistryPolicyError("decision must be approve or reject")
    if actor_role not in cfg["required_review_roles"]:
        raise ProviderRegistryPolicyError(
            "provider review requires one of: {0}".format(", ".join(cfg["required_review_roles"]))
        )
    _authorize_human(config, actor, actor_role)
    registry = load_provider_registry(config)
    provider = _require_provider(registry, provider_id)
    if provider["status"] not in {"due_diligence_submitted", "approved_pending_activation", "rejected", "suspended"}:
        raise ProviderRegistryPolicyError("complete due diligence before recording a provider review")
    submitter = str((provider.get("due_diligence") or {}).get("submitted_by") or "")
    if submitter and submitter == str(actor or "").strip():
        raise ProviderRegistryPolicyError("provider reviewer must be independent from the due diligence submitter")
    for role, review in (provider.get("reviews") or {}).items():
        if role != actor_role and str(review.get("actor") or "") == str(actor or "").strip():
            raise ProviderRegistryPolicyError("required provider review roles must use distinct human actors")
    refs = _safe_references(evidence_refs)
    _validate_provider_review_references(config, provider_id, refs)
    if not str(notes or "").strip() or not refs:
        raise ProviderRegistryPolicyError("provider review requires notes and at least one evidence reference")
    record = _append_provider_event(config, {
        "event_type": "provider_review_recorded",
        "provider_id": provider_id,
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {
            "decision": decision,
            "notes": str(notes or "").strip(),
            "evidence_refs": refs,
            "approval_granted": decision == "approve",
        },
    })
    return {"record": record, "registry": write_provider_registry_snapshot(build_provider_registry(config))}


def activate_provider(
    config: Dict[str, Any],
    provider_id: str,
    actor: str,
    actor_role: str,
    *,
    notes: str,
) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    provider_id = _provider_id(provider_id)
    actor_role = str(actor_role or "").strip()
    if actor_role not in cfg["activation_roles"]:
        raise ProviderRegistryPolicyError(
            "provider activation requires one of: {0}".format(", ".join(cfg["activation_roles"]))
        )
    _authorize_human(config, actor, actor_role)
    registry = load_provider_registry(config)
    provider = _require_provider(registry, provider_id)
    eligible, reasons = provider_activation_eligibility(provider, cfg)
    reasons.extend(_provider_control_evidence_reasons(config, provider))
    eligible = not reasons
    if not eligible:
        raise ProviderRegistryPolicyError("provider cannot be activated: {0}".format("; ".join(reasons)))
    reviewers = {str(item.get("actor") or "") for item in (provider.get("reviews") or {}).values()}
    if str(actor or "").strip() in reviewers:
        raise ProviderRegistryPolicyError("activation actor must be independent from provider reviewers")
    if not str(notes or "").strip():
        raise ProviderRegistryPolicyError("activation notes are required")
    record = _append_provider_event(config, {
        "event_type": "provider_activated",
        "provider_id": provider_id,
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {
            "notes": str(notes or "").strip(),
            "approved_scopes": list(provider.get("service_scopes") or []),
            "approval_granted": True,
        },
    })
    return {"record": record, "registry": write_provider_registry_snapshot(build_provider_registry(config))}


def suspend_provider(
    config: Dict[str, Any],
    provider_id: str,
    actor: str,
    actor_role: str,
    *,
    notes: str,
) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    provider_id = _provider_id(provider_id)
    actor_role = str(actor_role or "").strip()
    if actor_role not in cfg["suspension_roles"]:
        raise ProviderRegistryPolicyError("actor role is not allowed to suspend a provider")
    _authorize_human(config, actor, actor_role)
    provider = _require_provider(load_provider_registry(config), provider_id)
    if provider["status"] != "active":
        raise ProviderRegistryPolicyError("only an active provider can be suspended")
    if not str(notes or "").strip():
        raise ProviderRegistryPolicyError("suspension reason is required")
    record = _append_provider_event(config, {
        "event_type": "provider_suspended",
        "provider_id": provider_id,
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {"notes": str(notes or "").strip(), "approval_granted": False},
    })
    return {"record": record, "registry": write_provider_registry_snapshot(build_provider_registry(config))}


def load_provider_registry(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    path = project_path(cfg["event_path"])
    log = load_hash_chained_jsonl(path)
    providers = _project_providers(log["records"], cfg)
    for provider in providers.values():
        provider["control_evidence_blockers"] = _provider_control_evidence_reasons(config, provider)
        provider["operationally_eligible"] = not provider["control_evidence_blockers"]
    active = sorted(
        provider_id for provider_id, item in providers.items()
        if item.get("status") == "active" and item.get("operationally_eligible")
    ) if log["integrity_status"] == "OK" else []
    counts = Counter(str(item.get("status") or "unknown") for item in providers.values())
    return {
        "version": PROVIDER_REGISTRY_VERSION,
        "generated_at": now_iso(),
        "event_path": str(path),
        "report_dir": cfg["report_dir"],
        "integrity_status": log["integrity_status"],
        "invalid_record_count": log["invalid_record_count"],
        "integrity_errors": log["errors"],
        "event_count": log["record_count"],
        "last_event_hash": log["last_hash"],
        "provider_count": len(providers),
        "status_counts": dict(sorted(counts.items())),
        "active_provider_ids": active,
        "providers": providers,
        "required_review_roles": cfg["required_review_roles"],
        "required_evidence": cfg["required_evidence"],
        "required_scopes": cfg["required_scopes"],
        "automatic_approval_executed": False,
    }


def build_provider_registry(config: Dict[str, Any]) -> Dict[str, Any]:
    return load_provider_registry(config)


def load_active_provider_ids(config: Dict[str, Any], required_scope: str = "") -> List[str]:
    registry = load_provider_registry(config)
    if registry["integrity_status"] != "OK":
        return []
    result = []
    for provider_id in registry["active_provider_ids"]:
        provider = registry["providers"].get(provider_id, {})
        scopes = set(provider.get("service_scopes") or [])
        if not required_scope or required_scope in scopes:
            result.append(provider_id)
    return sorted(result)


def provider_activation_eligibility(provider: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons = []
    if provider.get("status") == "retired":
        reasons.append("provider is retired")
    due = provider.get("due_diligence") or {}
    evidence = due.get("evidence") or {}
    missing_evidence = [field for field in cfg["required_evidence"] if not str(evidence.get(field) or "").strip()]
    if missing_evidence:
        reasons.append("missing evidence: {0}".format(", ".join(missing_evidence)))
    missing_scopes = sorted(set(cfg["required_scopes"]).difference(provider.get("service_scopes") or []))
    if missing_scopes:
        reasons.append("missing service scopes: {0}".format(", ".join(missing_scopes)))
    reviews = provider.get("reviews") or {}
    missing_roles = [role for role in cfg["required_review_roles"] if (reviews.get(role) or {}).get("decision") != "approve"]
    if missing_roles:
        reasons.append("missing approvals: {0}".format(", ".join(missing_roles)))
    actors = [str((reviews.get(role) or {}).get("actor") or "") for role in cfg["required_review_roles"]]
    if actors and (not all(actors) or len(set(actors)) != len(actors)):
        reasons.append("provider reviewers must be distinct human actors")
    submitter = str(due.get("submitted_by") or "")
    if submitter and submitter in actors:
        reasons.append("provider reviewers must be independent from the submitter")
    return (not reasons, reasons)


def write_provider_registry_snapshot(registry: Dict[str, Any]) -> Dict[str, str]:
    report_dir = project_path(str(registry.get("report_dir") or "reports/production_pilot"))
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "provider_registry_latest.json"
    md_path = report_dir / "provider_registry_latest.md"
    atomic_write_text(json_path, json.dumps(registry, ensure_ascii=False, indent=2, default=str))
    atomic_write_text(md_path, render_provider_registry_markdown(registry), encoding="utf-8-sig")
    return {"provider_registry_json_path": str(json_path), "provider_registry_path": str(md_path)}


def render_provider_registry_markdown(registry: Dict[str, Any]) -> str:
    lines = [
        "# Provider Registry",
        "",
        "- generated_at: {0}".format(registry.get("generated_at", "")),
        "- integrity: {0}".format(registry.get("integrity_status", "UNKNOWN")),
        "- providers / active: {0} / {1}".format(
            registry.get("provider_count", 0), len(registry.get("active_provider_ids", []))
        ),
        "- automatic approval: false",
        "",
        "## Providers",
        "",
    ]
    providers = registry.get("providers") or {}
    if not providers:
        lines.append("- none; create candidates only after identifying real providers and retain evidence references")
    for provider_id, item in sorted(providers.items()):
        eligible, reasons = provider_activation_eligibility(item, {
            "required_evidence": registry.get("required_evidence", []),
            "required_review_roles": registry.get("required_review_roles", []),
            "required_scopes": registry.get("required_scopes", []),
        })
        lines.extend([
            "### {0}".format(provider_id),
            "",
            "- legal name: {0}".format(item.get("legal_name", "")),
            "- status: {0}".format(item.get("status", "")),
            "- scopes: {0}".format(", ".join(item.get("service_scopes", [])) or "none"),
            "- activation eligible: {0}".format(str(eligible).lower()),
            "- blockers: {0}".format("; ".join(reasons) or "none"),
            "- control evidence blockers: {0}".format(
                "; ".join(item.get("control_evidence_blockers", [])) or "none"
            ),
            "",
        ])
    if registry.get("integrity_errors"):
        lines.extend(["## Integrity Errors", ""])
        for item in registry["integrity_errors"]:
            lines.append("- line {0}: {1}".format(item.get("line", ""), item.get("reason", "")))
    return "\n".join(lines).rstrip() + "\n"


def _append_provider_event(config: Dict[str, Any], value: Dict[str, Any]) -> Dict[str, Any]:
    cfg = provider_registry_config(config)
    event = dict(value)
    event["schema_version"] = PROVIDER_REGISTRY_VERSION
    event["recorded_at"] = now_iso()
    if event.get("event_type") not in PROVIDER_EVENT_TYPES:
        raise ProviderRegistryPolicyError("unsupported provider event type")
    return append_hash_chained_jsonl(project_path(cfg["event_path"]), event)


def _project_providers(events: Iterable[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    providers: Dict[str, Dict[str, Any]] = {}
    for event in events:
        provider_id = str(event.get("provider_id") or "").strip()
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not provider_id or event_type not in PROVIDER_EVENT_TYPES:
            continue
        if event_type == "provider_candidate_created":
            if provider_id in providers:
                continue
            providers[provider_id] = {
                "provider_id": provider_id,
                "legal_name": str(payload.get("legal_name") or ""),
                "service_scopes": _clean_list(payload.get("service_scopes", [])),
                "status": "draft",
                "created_at": event.get("recorded_at", ""),
                "created_by": event.get("actor", ""),
                "due_diligence": {},
                "reviews": {},
                "activated_at": "",
                "activated_by": "",
                "last_event_at": event.get("recorded_at", ""),
            }
            continue
        provider = providers.get(provider_id)
        if not provider:
            continue
        if event_type == "provider_due_diligence_submitted":
            provider["due_diligence"] = {
                "submitted_at": event.get("recorded_at", ""),
                "submitted_by": event.get("actor", ""),
                "submitted_by_role": event.get("actor_role", ""),
                "evidence": dict(payload.get("evidence") or {}),
                "notes": str(payload.get("notes") or ""),
            }
            provider["reviews"] = {}
            provider["status"] = "due_diligence_submitted"
            provider["activated_at"] = ""
            provider["activated_by"] = ""
        elif event_type == "provider_review_recorded":
            role = str(event.get("actor_role") or "")
            provider.setdefault("reviews", {})[role] = {
                "decision": str(payload.get("decision") or ""),
                "actor": event.get("actor", ""),
                "actor_role": role,
                "recorded_at": event.get("recorded_at", ""),
                "notes": str(payload.get("notes") or ""),
                "evidence_refs": _clean_list(payload.get("evidence_refs", [])),
            }
            if str(payload.get("decision") or "") == "reject":
                provider["status"] = "rejected"
            else:
                any_rejected = any(
                    str(review.get("decision") or "") == "reject"
                    for review in (provider.get("reviews") or {}).values()
                )
                eligible, _ = provider_activation_eligibility(provider, cfg)
                provider["status"] = (
                    "rejected" if any_rejected
                    else ("approved_pending_activation" if eligible else "due_diligence_submitted")
                )
        elif event_type == "provider_activated":
            provider["status"] = "active"
            provider["activated_at"] = event.get("recorded_at", "")
            provider["activated_by"] = event.get("actor", "")
        elif event_type == "provider_suspended":
            provider["status"] = "suspended"
            provider["suspension"] = {
                "recorded_at": event.get("recorded_at", ""),
                "actor": event.get("actor", ""),
                "actor_role": event.get("actor_role", ""),
                "notes": str(payload.get("notes") or ""),
            }
        elif event_type == "provider_retired":
            provider["status"] = "retired"
        provider["last_event_at"] = event.get("recorded_at", "")
    return providers


def _authorize_human(config: Dict[str, Any], actor: str, actor_role: str) -> Dict[str, Any]:
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    inputs = load_operations_inputs(config)
    if inputs.get("errors"):
        raise ProviderRegistryPolicyError("research roster is invalid: {0}".format("; ".join(inputs["errors"][:5])))
    principal = inputs.get("principal_map", {}).get(actor)
    if not principal:
        raise ProviderRegistryPolicyError("actor is not registered in the research operations roster")
    if principal.get("principal_type") != "human" or not principal.get("active"):
        raise ProviderRegistryPolicyError("actor must be an active human principal")
    if actor_role not in (principal.get("roles") or []):
        raise ProviderRegistryPolicyError("actor is not registered for role {0}".format(actor_role))
    required = ROLE_AUTHORIZATIONS.get(actor_role)
    if required and required not in (principal.get("authorizations") or []):
        raise ProviderRegistryPolicyError("actor lacks required authorization: {0}".format(required))
    return principal


def _validate_evidence(
    evidence: Dict[str, Any],
    required_fields: Sequence[str],
    *,
    config: Optional[Dict[str, Any]] = None,
    provider_id: str = "",
) -> Dict[str, str]:
    if not isinstance(evidence, dict):
        raise ProviderRegistryPolicyError("evidence must be an object")
    normalized = {str(key): str(value or "").strip() for key, value in evidence.items() if str(key).strip()}
    missing = [field for field in required_fields if not normalized.get(field)]
    if missing:
        raise ProviderRegistryPolicyError("missing provider evidence: {0}".format(", ".join(missing)))
    for value in normalized.values():
        if SECRET_PATTERN.search(value):
            raise ProviderRegistryPolicyError("evidence references must not contain API keys or secrets")
    if _control_evidence_enforced(config or {}):
        reasons = _provider_control_evidence_reasons(
            config or {}, {"provider_id": provider_id, "due_diligence": {"evidence": normalized}}
        )
        if reasons:
            raise ProviderRegistryPolicyError("provider control evidence is not ready: {0}".format("; ".join(reasons)))
    return normalized


def _control_evidence_enforced(config: Dict[str, Any]) -> bool:
    raw = config.get("business_onboarding", {}) if isinstance(config, dict) else {}
    return bool(raw.get("enabled", False) and raw.get("enforce", False) and raw.get("require_verified_control_evidence", True))


def _provider_control_evidence_reasons(config: Dict[str, Any], provider: Dict[str, Any]) -> List[str]:
    if not _control_evidence_enforced(config):
        return []
    provider_id = str(provider.get("provider_id") or "").strip()
    evidence = ((provider.get("due_diligence") or {}).get("evidence") or {})
    reasons = []
    for field, categories in PROVIDER_EVIDENCE_CATEGORIES.items():
        evidence_id = str(evidence.get(field) or "").strip()
        if not evidence_id:
            reasons.append("{0}: missing control evidence reference".format(field))
            continue
        ok, reason, _ = resolve_verified_control_evidence(
            config, evidence_id, categories=categories, subject_id=provider_id
        )
        if not ok:
            reasons.append("{0}: {1}".format(field, reason))
    return reasons


def _validate_provider_review_references(
    config: Dict[str, Any], provider_id: str, references: Sequence[str]
) -> None:
    if not _control_evidence_enforced(config):
        return
    allowed_categories = sorted({
        category for categories in PROVIDER_EVIDENCE_CATEGORIES.values() for category in categories
    })
    for reference in references:
        if reference.startswith("control:"):
            ok, reason, _ = resolve_verified_control_evidence(
                config, reference, categories=allowed_categories, subject_id=provider_id
            )
            if not ok:
                raise ProviderRegistryPolicyError(
                    "provider review control evidence is not ready: {0}".format(reason)
                )
        else:
            try:
                validate_managed_archive_reference(config, reference)
            except ValueError as exc:
                raise ProviderRegistryPolicyError(str(exc)) from exc


def _safe_references(values: Sequence[str]) -> List[str]:
    refs = _clean_list(values)
    if any(SECRET_PATTERN.search(value) for value in refs):
        raise ProviderRegistryPolicyError("evidence references must not contain API keys or secrets")
    return refs


def _provider_id(value: str) -> str:
    provider_id = str(value or "").strip().lower()
    if not provider_id or not re.match(r"^[a-z0-9][a-z0-9._-]{1,63}$", provider_id):
        raise ProviderRegistryPolicyError("provider_id must match [a-z0-9][a-z0-9._-]{1,63}")
    return provider_id


def _require_provider(registry: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    if registry.get("integrity_status") != "OK":
        raise ProviderRegistryPolicyError("provider registry integrity check failed")
    provider = (registry.get("providers") or {}).get(provider_id)
    if not provider:
        raise ProviderRegistryPolicyError("provider not found: {0}".format(provider_id))
    return provider


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
