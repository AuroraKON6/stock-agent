"""Auditable research workpapers for institution-style research cases.

The append-only hash chain is the source of truth. ``latest.json`` and the
Markdown files are rebuildable projections for operators. Automated analysis
may create or revise a draft, but protected quality, compliance, publication,
assignment, and trading decisions remain outside this module.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .audit_log import (
    AuditLogIntegrityError,
    append_hash_chained_jsonl,
    atomic_write_text,
    load_hash_chained_jsonl,
    object_hash,
)
from .config import project_path, project_path_override
from .evidence_vault import (
    evidence_vault_config,
    find_retained_evidence_for_signal,
    load_evidence_vault,
    validate_evidence_vault_policy,
    verify_evidence_vault,
)
from .models import now_iso
from .pi_skills import pi_skill_supply_chain_status
from .storage import SignalStore


WORKPAPER_SCHEMA_VERSION = "27.0"
WORKPAPER_POLICY_VERSION = "27.0"
SAFETY_BOUNDARY = (
    "Internal research assistance only. No automatic trading, protected-gate "
    "approval, external publication, assignment, or configuration mutation."
)
AUTOMATED_ACTOR_PREFIXES = (
    "system",
    "llm",
    "agent",
    "deepseek",
    "pi",
    "openclaw",
    "bot",
    "stock_agent",
)
OFFICIAL_PRIMARY_DOMAINS = {
    "csrc.gov.cn",
    "sac.net.cn",
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "cninfo.com.cn",
}
MODEL_ROLES = {
    "signal_llm": "signal_analyst",
    "role_llm": "research_desk",
    "case_llm": "case_analyst",
}
PASS_ACTIONS = {"quality_approve", "compliance_clear", "publication_approve"}


class ResearchWorkpaperError(ValueError):
    pass


@contextmanager
def _active_project_path(resolver: Optional[Any]) -> Iterator[None]:
    with project_path_override(resolver):
        yield


def research_workpaper_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = (config or {}).get("research_workpapers", {}) or {}
    required_priorities = _clean_list(raw.get("require_official_primary_for", ["P0", "P1"]))
    try:
        retention_years = int(raw.get("retention_years", 5) or 0)
    except (TypeError, ValueError):
        retention_years = 0
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce": bool(raw.get("enforce", False)),
        "events_path": str(raw.get("events_path") or "data/research_workpapers/events.jsonl"),
        "projection_path": str(raw.get("projection_path") or "data/research_workpapers/latest.json"),
        "report_dir": str(raw.get("report_dir") or "reports/research_workpapers"),
        "retention_years": retention_years,
        "require_official_primary_for": required_priorities or ["P0", "P1"],
        "require_exact_evidence_refs": bool(raw.get("require_exact_evidence_refs", True)),
        "require_market_governance_pass": bool(raw.get("require_market_governance_pass", True)),
        "require_distinct_human_roles": bool(raw.get("require_distinct_human_roles", True)),
        "require_independent_challenge": bool(raw.get("require_independent_challenge", True)),
        "archive_backend": str(raw.get("archive_backend") or "local_append_only_shadow"),
        "production_archive_approved": bool(raw.get("production_archive_approved", False)),
        "production_archive_evidence_ref": str(raw.get("production_archive_evidence_ref") or ""),
        "automatic_approval": False,
        "automatic_assignment": False,
        "automatic_publication": False,
        "automatic_trading": False,
    }


def validate_workpaper_policy(config: Optional[Dict[str, Any]] = None) -> List[str]:
    raw = (config or {}).get("research_workpapers", {}) or {}
    cfg = research_workpaper_config(config)
    errors: List[str] = []
    for key in (
        "automatic_approval",
        "automatic_assignment",
        "automatic_publication",
        "automatic_trading",
    ):
        if raw.get(key) not in (None, False):
            errors.append("research_workpapers.{0} must be false".format(key))
    if cfg["retention_years"] < 5:
        errors.append("research workpaper retention must be at least five years")
    if cfg["enabled"] and cfg["enforce"]:
        required_controls = {
            "require_exact_evidence_refs": "exact retained evidence references",
            "require_market_governance_pass": "market-data governance PASS",
            "require_distinct_human_roles": "distinct protected-gate human roles",
            "require_independent_challenge": "independent human challenge",
        }
        for key, label in required_controls.items():
            if not cfg[key]:
                errors.append("enforced research workpapers require {0}".format(label))
        missing_priorities = sorted({"P0", "P1"}.difference(cfg["require_official_primary_for"]))
        if missing_priorities:
            errors.append(
                "enforced research workpapers require official primary sources for {0}".format(
                    ", ".join(missing_priorities)
                )
            )
    if cfg["production_archive_approved"] and not cfg["production_archive_evidence_ref"]:
        errors.append("approved production archive requires a retained evidence reference")
    if evidence_vault_config(config)["enabled"]:
        errors.extend("official_evidence_vault: {0}".format(item) for item in validate_evidence_vault_policy(config))
    return errors


def sync_research_workpapers(
    config: Dict[str, Any],
    *,
    cases: Optional[Sequence[Dict[str, Any]]] = None,
    signals: Optional[Sequence[Any]] = None,
    runtime_cases: Optional[Sequence[Dict[str, Any]]] = None,
    report_path: str = "",
    project_path_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Create idempotent, versioned workpaper revisions for research cases."""
    cfg = research_workpaper_config(config)
    if not cfg["enabled"]:
        return _disabled_result()

    with _active_project_path(project_path_fn):
        policy_errors = validate_workpaper_policy(config)
        chain = load_hash_chained_jsonl(project_path(cfg["events_path"]))
        if chain["integrity_status"] != "OK":
            projection = _projection_from_chain(chain, cfg)
            paths = write_research_workpaper_reports(projection, config)
            return _result(projection, paths, created=0, unchanged=0, preserved=0, policy_errors=policy_errors)

        report = _load_report(report_path)
        incoming_cases = list(cases) if cases is not None else _cases_from_sources(report)
        incoming_signals = list(signals) if signals is not None else list(report.get("signals", []) or [])
        runtime_rows = list(runtime_cases) if runtime_cases is not None else _runtime_cases()
        runtime_map = {
            str(item.get("case_id") or ""): item
            for item in runtime_rows
            if isinstance(item, dict) and item.get("case_id")
        }
        existing_projection = _projection_from_chain(chain, cfg)
        existing_map = {
            str(item.get("workpaper_id") or ""): item
            for item in existing_projection.get("workpapers", [])
            if isinstance(item, dict) and item.get("workpaper_id")
        }
        signal_map = _signal_map(incoming_signals)
        required_ids = _clean_list(
            signal_id
            for case in incoming_cases
            if isinstance(case, dict)
            for signal_id in case.get("signal_ids", []) or []
        )
        missing = [item for item in required_ids if item not in signal_map]
        if missing:
            signal_map.update(_signal_map(_load_persisted_signals(config, missing)))

        skill_status = _safe_pi_skill_status(config)
        evidence_vault = load_evidence_vault(config)
        archive_state = _production_archive_state(config, cfg)
        created = 0
        unchanged = 0
        preserved = 0
        degradation: List[str] = []
        events_path = project_path(cfg["events_path"])

        for source_case in incoming_cases:
            if not isinstance(source_case, dict):
                continue
            case_id = str(source_case.get("case_id") or "").strip()
            if not case_id:
                continue
            workpaper_id = _workpaper_id(case_id)
            previous = existing_map.get(workpaper_id, {})
            case_signal_ids = _clean_list(source_case.get("signal_ids", []))
            unresolved = [item for item in case_signal_ids if item not in signal_map]
            if unresolved and previous:
                preserved += 1
                degradation.append(
                    "case {0}: retained prior workpaper because {1} signal record(s) are unavailable".format(
                        case_id, len(unresolved)
                    )
                )
                continue
            matched = [signal_map[item] for item in case_signal_ids if item in signal_map]
            workpaper = build_research_workpaper(
                source_case,
                matched,
                runtime_map.get(case_id, {}),
                config,
                previous=previous,
                skill_status=skill_status,
                unavailable_signal_ids=unresolved,
                evidence_vault=evidence_vault,
                archive_state=archive_state,
            )
            if previous and previous.get("revision_basis_sha256") == workpaper.get("revision_basis_sha256"):
                unchanged += 1
                continue
            event = {
                "event_type": "research_workpaper_revision_created",
                "occurred_at": now_iso(),
                "actor": "stock_agent_workpaper_service",
                "actor_role": "research_workpaper_service",
                "principal_type": "service",
                "workpaper_id": workpaper_id,
                "case_id": case_id,
                "version": workpaper["version"],
                "revision_basis_sha256": workpaper["revision_basis_sha256"],
                "workpaper": workpaper,
                "automatic_approval_executed": False,
                "automatic_assignment_executed": False,
                "automatic_publication_executed": False,
                "automatic_trading_executed": False,
                "safety_boundary": SAFETY_BOUNDARY,
            }
            append_hash_chained_jsonl(events_path, event)
            existing_map[workpaper_id] = workpaper
            created += 1

        projection = load_research_workpapers(config)
        projection["degradation"] = {
            "degraded": bool(degradation),
            "reasons": degradation,
            "unavailable_signal_ids": sorted(set(missing).difference(signal_map)),
            "preserved_workpaper_count": preserved,
        }
        projection["policy_errors"] = policy_errors
        _write_projection(projection, cfg)
        paths = write_research_workpaper_reports(projection, config)
        return _result(
            projection,
            paths,
            created=created,
            unchanged=unchanged,
            preserved=preserved,
            policy_errors=policy_errors,
        )


def build_research_workpaper(
    source_case: Dict[str, Any],
    signals: Sequence[Dict[str, Any]],
    runtime_case: Dict[str, Any],
    config: Dict[str, Any],
    *,
    previous: Optional[Dict[str, Any]] = None,
    skill_status: Optional[Dict[str, Any]] = None,
    unavailable_signal_ids: Optional[Sequence[str]] = None,
    evidence_vault: Optional[Dict[str, Any]] = None,
    archive_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = research_workpaper_config(config)
    previous = previous or {}
    skill_status = skill_status or _safe_pi_skill_status(config)
    case_id = str(source_case.get("case_id") or runtime_case.get("case_id") or "").strip()
    if not case_id:
        raise ResearchWorkpaperError("case_id is required")
    workpaper_id = _workpaper_id(case_id)
    evidence_vault = evidence_vault if evidence_vault is not None else load_evidence_vault(config)
    archive_state = archive_state or _production_archive_state(config, cfg)
    source_records = [_source_record(signal, config, evidence_vault) for signal in signals]
    claims = _claims_for_signals(signals, source_records)
    workflow = _workflow_state(runtime_case, source_case)
    analysis = _analysis_state(source_case, signals)
    challenge = _challenge_state(runtime_case, source_case, signals)
    methods = _method_state(signals, source_case, skill_status, config)
    governance = _governance_state(signals)
    now = now_iso()
    created_at = str(previous.get("created_at") or source_case.get("created_at") or now)
    retention_until = _retention_until(created_at, cfg["retention_years"])
    signal_ids = _clean_list(source_case.get("signal_ids", []))
    unavailable = _clean_list(unavailable_signal_ids or [])

    core = {
        "case": {
            "case_id": case_id,
            "title": str(source_case.get("title") or runtime_case.get("title") or case_id),
            "entity": str(source_case.get("entity") or runtime_case.get("entity") or "unknown"),
            "topic": str(source_case.get("topic") or runtime_case.get("topic") or "unknown"),
            "catalyst_path": str(source_case.get("catalyst_path") or runtime_case.get("catalyst_path") or "unknown"),
            "priority": str(source_case.get("top_priority") or runtime_case.get("top_priority") or "P3"),
            "signal_ids": signal_ids,
            "unavailable_signal_ids": unavailable,
        },
        "source_records": source_records,
        "claims": claims,
        "analysis": analysis,
        "challenge": challenge,
        "workflow": workflow,
        "governance": governance,
        "methods": methods,
        "retention": {
            "record_class": "research_workpaper",
            "minimum_years": cfg["retention_years"],
            "retain_until": retention_until,
            "archive_backend": archive_state["backend"],
            "production_archive_approved": archive_state["ready"],
            "production_archive_evidence_ref": archive_state["evidence_ref"],
            "archive_policy_fingerprint": archive_state.get("policy_fingerprint", ""),
            "local_chain_is_production_archive": False,
        },
        "policy": _policy_projection(cfg),
        "safety_boundary": SAFETY_BOUNDARY,
    }
    revision_basis = object_hash(core)
    workpaper = {
        "schema_version": WORKPAPER_SCHEMA_VERSION,
        "policy_version": WORKPAPER_POLICY_VERSION,
        "workpaper_id": workpaper_id,
        "case_id": case_id,
        "version": int(previous.get("version", 0) or 0) + 1,
        "created_at": created_at,
        "updated_at": now,
        "status": _workpaper_status(workflow),
        "author": {
            "actor": "stock_agent_workpaper_service",
            "actor_role": "research_workpaper_service",
            "principal_type": "service",
            "may_pass_protected_gate": False,
        },
        **core,
        "revision_basis_sha256": revision_basis,
        "content_sha256": object_hash(core),
        "automatic_approval_executed": False,
        "automatic_assignment_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }
    workpaper["validation"] = validate_research_workpaper(workpaper, config)
    return workpaper


def validate_research_workpaper(
    workpaper: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = research_workpaper_config(config)
    technical_errors: List[str] = []
    approval_blockers: List[str] = []
    warnings: List[str] = []
    for field in ("workpaper_id", "case_id", "version", "case", "source_records", "claims", "workflow"):
        if workpaper.get(field) in (None, ""):
            technical_errors.append("required workpaper field is missing: {0}".format(field))
    if str(workpaper.get("schema_version") or "") != WORKPAPER_SCHEMA_VERSION:
        technical_errors.append("unsupported research workpaper schema version")

    source_records = [item for item in workpaper.get("source_records", []) if isinstance(item, dict)]
    evidence_ids = [str(item.get("evidence_id") or "") for item in source_records]
    if len(evidence_ids) != len(set(evidence_ids)) or any(not item for item in evidence_ids):
        technical_errors.append("source evidence IDs must be present and unique")
    if not source_records:
        approval_blockers.append("workpaper has no retained source record")
    for source in source_records:
        if not str(source.get("content_sha256") or ""):
            technical_errors.append("source record lacks content SHA-256: {0}".format(source.get("evidence_id", "")))
        if cfg["require_exact_evidence_refs"] and not str(source.get("source_locator") or ""):
            technical_errors.append("source record lacks an exact retained locator: {0}".format(source.get("evidence_id", "")))
        if source.get("original_document_retained") is not True:
            warnings.append("original source document is not retained: {0}".format(source.get("evidence_id", "")))

    priority = str((workpaper.get("case", {}) or {}).get("priority") or "P3")
    if priority in set(cfg["require_official_primary_for"]):
        if not any(item.get("primary_source_eligible") is True for item in source_records):
            approval_blockers.append("{0} workpaper requires an official primary source".format(priority))

    evidence_set = set(evidence_ids)
    claim_counts = Counter()
    for claim in workpaper.get("claims", []) or []:
        if not isinstance(claim, dict):
            technical_errors.append("claim must be an object")
            continue
        claim_type = str(claim.get("claim_type") or "unknown")
        claim_counts[claim_type] += 1
        refs = _clean_list(claim.get("evidence_refs", []))
        unknown = sorted(set(refs).difference(evidence_set))
        if unknown:
            technical_errors.append("claim {0} references unknown evidence: {1}".format(claim.get("claim_id", ""), ", ".join(unknown)))
        if claim_type == "model_inference" and not refs:
            approval_blockers.append("model inference lacks source evidence: {0}".format(claim.get("claim_id", "")))
        support = str(claim.get("support_status") or "unsupported")
        if support in {"unsupported", "blocked", "conflicted"}:
            approval_blockers.append("claim is not approval-eligible ({0}): {1}".format(support, claim.get("claim_id", "")))
        elif support == "partial_grounding":
            warnings.append("claim has partial grounding: {0}".format(claim.get("claim_id", "")))

    workflow = workpaper.get("workflow", {}) or {}
    approvals = workflow.get("approvals", {}) or {}
    approved_actors: List[str] = []
    for gate in ("quality_review", "compliance_review", "publication"):
        approval = approvals.get(gate, {}) if isinstance(approvals.get(gate), dict) else {}
        if str(approval.get("status") or "pending") != "approved":
            continue
        actor = str(approval.get("actor") or "").strip()
        if not actor or _is_automated_actor(actor):
            technical_errors.append("{0} approval must be performed by a named human actor".format(gate))
        else:
            approved_actors.append(actor)
    lead = str((workflow.get("assignments", {}) or {}).get("lead_analyst") or "").strip()
    separation = [item for item in [lead] + approved_actors if item]
    if cfg["require_distinct_human_roles"] and len(separation) != len(set(separation)):
        technical_errors.append("lead analyst, quality reviewer, compliance officer, and publisher must be distinct")

    challenge = workpaper.get("challenge", {}) or {}
    if cfg["require_independent_challenge"] and workflow.get("stage") in {
        "quality_review", "compliance_review", "human_approval", "published", "monitoring", "closed"
    }:
        if challenge.get("independent_human_challenge_completed") is not True:
            approval_blockers.append("independent human challenge is required before protected review gates")

    retention = workpaper.get("retention", {}) or {}
    if int(retention.get("minimum_years", 0) or 0) < 5:
        technical_errors.append("workpaper retention period is less than five years")
    if retention.get("production_archive_approved") is not True:
        warnings.append("local append-only storage is not an approved production archive")
    if (workpaper.get("case", {}) or {}).get("unavailable_signal_ids"):
        approval_blockers.append("one or more referenced signal records are unavailable")

    errors = _unique(list(technical_errors) + list(approval_blockers))

    return {
        "evaluated_at": now_iso(),
        "status": "FAIL" if technical_errors else ("WARN" if approval_blockers or warnings else "PASS"),
        "approval_eligible": not errors,
        "errors": errors,
        "technical_errors": _unique(technical_errors),
        "approval_blockers": _unique(approval_blockers),
        "warnings": _unique(warnings),
        "claim_type_counts": dict(sorted(claim_counts.items())),
        "source_record_count": len(source_records),
        "claim_count": sum(claim_counts.values()),
        "facts_and_inferences_separated": bool(claim_counts) and all(
            str(item.get("claim_type") or "") in {
                "source_statement", "data_observation", "model_inference", "human_judgment"
            }
            for item in workpaper.get("claims", []) or []
            if isinstance(item, dict)
        ),
    }


def load_research_workpapers(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = research_workpaper_config(config)
    chain = load_hash_chained_jsonl(project_path(cfg["events_path"]))
    return _projection_from_chain(chain, cfg)


def verify_research_workpapers(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = research_workpaper_config(config)
    if not cfg["enabled"]:
        return {
            "schema_version": WORKPAPER_SCHEMA_VERSION,
            "generated_at": now_iso(),
            "enabled": False,
            "technical_status": "DISABLED",
            "business_status": "DISABLED",
            "integrity_status": "DISABLED",
            "event_count": 0,
            "workpaper_count": 0,
            "production_archive_ready": False,
            "errors": [],
            "approval_blockers": [],
            "warnings": [],
            "validations": [],
            "safety_boundary": SAFETY_BOUNDARY,
        }
    projection = load_research_workpapers(config)
    errors = list(validate_workpaper_policy(config))
    warnings: List[str] = []
    approval_blockers: List[str] = []
    if projection.get("integrity_status") != "OK":
        errors.append("research workpaper event hash chain failed integrity verification")
    validations = []
    for workpaper in projection.get("workpapers", []) or []:
        validation = validate_research_workpaper(workpaper, config)
        validations.append({"workpaper_id": workpaper.get("workpaper_id", ""), **validation})
        errors.extend(
            "{0}: {1}".format(workpaper.get("workpaper_id", ""), item)
            for item in validation.get("technical_errors", [])
        )
        approval_blockers.extend(
            "{0}: {1}".format(workpaper.get("workpaper_id", ""), item)
            for item in validation.get("approval_blockers", [])
        )
        warnings.extend(
            "{0}: {1}".format(workpaper.get("workpaper_id", ""), item)
            for item in validation.get("warnings", [])
        )
    archive_state = _production_archive_state(config or {}, cfg)
    archive_ready = bool(archive_state["ready"])
    if not archive_ready:
        warnings.append("production archive is not approved; business publication readiness remains blocked")
    return {
        "schema_version": WORKPAPER_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "enabled": cfg["enabled"],
        "technical_status": "FAIL" if errors else "READY",
        "business_status": "BLOCKED" if errors or approval_blockers or warnings else "READY",
        "integrity_status": projection.get("integrity_status", "UNKNOWN"),
        "event_count": projection.get("event_count", 0),
        "workpaper_count": len(projection.get("workpapers", []) or []),
        "production_archive_ready": archive_ready,
        "errors": _unique(errors),
        "approval_blockers": _unique(approval_blockers),
        "warnings": _unique(warnings),
        "validations": validations,
        "safety_boundary": SAFETY_BOUNDARY,
    }


def validate_workpaper_gate(case_id: str, action: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed before a protected pass action when workpaper enforcement is on."""
    cfg = research_workpaper_config(config)
    result = {"enabled": cfg["enabled"], "enforced": cfg["enabled"] and cfg["enforce"], "allowed": True, "reasons": []}
    if not result["enforced"] or action not in PASS_ACTIONS:
        return result
    projection = load_research_workpapers(config)
    if projection.get("integrity_status") != "OK":
        result["reasons"].append("research workpaper hash chain integrity failed")
    workpaper = next(
        (item for item in projection.get("workpapers", []) if str(item.get("case_id") or "") == str(case_id or "")),
        None,
    )
    if workpaper is None:
        result["reasons"].append("structured research workpaper does not exist")
    else:
        validation = validate_research_workpaper(workpaper, config)
        result["reasons"].extend(validation.get("errors", []))
        workflow = workpaper.get("workflow", {}) or {}
        approvals = workflow.get("approvals", {}) or {}
        if action == "quality_approve" and (workpaper.get("challenge", {}) or {}).get("independent_human_challenge_completed") is not True:
            result["reasons"].append("independent human challenge is not recorded in the workpaper")
        if action == "compliance_clear" and str((approvals.get("quality_review", {}) or {}).get("status")) != "approved":
            result["reasons"].append("quality approval is not reflected in the workpaper")
        if action == "publication_approve":
            if str((approvals.get("quality_review", {}) or {}).get("status")) != "approved":
                result["reasons"].append("quality approval is not reflected in the workpaper")
            if str((approvals.get("compliance_review", {}) or {}).get("status")) != "approved":
                result["reasons"].append("compliance approval is not reflected in the workpaper")
            archive_state = _production_archive_state(config, cfg)
            if not archive_state["ready"]:
                result["reasons"].append("approved production archive evidence is required before publication approval")
    result["reasons"] = _unique(result["reasons"])
    result["allowed"] = not result["reasons"]
    if not result["allowed"]:
        raise ResearchWorkpaperError("Research workpaper gate denied action: {0}".format("; ".join(result["reasons"])))
    return result


def refresh_workpaper_from_runtime_action(
    runtime_case: Dict[str, Any],
    action_record: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Append a workpaper revision after a successfully persisted runtime action."""
    cfg = research_workpaper_config(config)
    if not cfg["enabled"]:
        return {"enabled": False, "updated": False}
    projection = load_research_workpapers(config)
    if projection.get("integrity_status") != "OK":
        raise AuditLogIntegrityError("Research workpaper audit chain failed before runtime-action synchronization")
    case_id = str(runtime_case.get("case_id") or "")
    previous = next(
        (item for item in projection.get("workpapers", []) if str(item.get("case_id") or "") == case_id),
        None,
    )
    if previous is None:
        return {"enabled": True, "updated": False, "reason": "workpaper_missing"}
    revised = copy.deepcopy(previous)
    revised.pop("audit_event_id", None)
    revised.pop("audit_event_hash", None)
    revised["version"] = int(previous.get("version", 0) or 0) + 1
    revised["updated_at"] = now_iso()
    revised["workflow"] = _workflow_state(runtime_case, runtime_case.get("source_case", {}) or {})
    revised["challenge"] = _challenge_state(runtime_case, runtime_case.get("source_case", {}) or {}, [])
    revised["status"] = _workpaper_status(revised["workflow"])
    core = _revision_core(revised)
    revised["revision_basis_sha256"] = object_hash(core)
    revised["content_sha256"] = object_hash(core)
    revised["validation"] = validate_research_workpaper(revised, config)
    if revised["revision_basis_sha256"] == previous.get("revision_basis_sha256"):
        return {"enabled": True, "updated": False, "reason": "unchanged"}
    event = append_hash_chained_jsonl(project_path(cfg["events_path"]), {
        "event_type": "research_workpaper_runtime_action_synced",
        "occurred_at": now_iso(),
        "actor": str(action_record.get("actor") or ""),
        "actor_role": str(action_record.get("actor_role") or ""),
        "principal_type": str(action_record.get("principal_type") or "unknown"),
        "runtime_action_id": str(action_record.get("action_id") or ""),
        "runtime_action": str(action_record.get("action") or ""),
        "workpaper_id": revised["workpaper_id"],
        "case_id": case_id,
        "version": revised["version"],
        "revision_basis_sha256": revised["revision_basis_sha256"],
        "workpaper": revised,
        "automatic_approval_executed": False,
        "automatic_assignment_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
        "safety_boundary": SAFETY_BOUNDARY,
    })
    updated = load_research_workpapers(config)
    _write_projection(updated, cfg)
    write_research_workpaper_reports(updated, config)
    return {"enabled": True, "updated": True, "event_id": event.get("event_id", ""), "version": revised["version"]}


def write_research_workpaper_reports(
    projection: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    cfg = research_workpaper_config(config)
    projection = projection or load_research_workpapers(config)
    report_dir = project_path(cfg["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_time(projection.get("generated_at") or now_iso())
    json_path = report_dir / "workpapers_{0}.json".format(stamp)
    md_path = report_dir / "workpapers_{0}.md".format(stamp)
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    payload = json.dumps(projection, ensure_ascii=False, indent=2, default=str) + "\n"
    markdown = _render_markdown(projection, cfg)
    for path in (json_path, latest_json):
        atomic_write_text(path, payload)
    for path in (md_path, latest_md):
        atomic_write_text(path, markdown)
    return {
        "research_workpaper_json_path": str(json_path),
        "research_workpaper_report_path": str(md_path),
        "research_workpaper_latest_json_path": str(latest_json),
        "research_workpaper_latest_path": str(latest_md),
    }


def _source_record(
    signal: Dict[str, Any],
    config: Dict[str, Any],
    evidence_vault: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    signal_id = str(signal.get("signal_id") or "")
    payload = signal.get("payload", {}) if isinstance(signal.get("payload"), dict) else {}
    signal_url = str(signal.get("url") or "")
    retained = find_retained_evidence_for_signal(signal, config, vault=evidence_vault)
    url = str(retained.get("source_url") or signal_url)
    domain = _domain(url)
    market = payload.get("market_data_governance", {}) if isinstance(payload.get("market_data_governance"), dict) else {}
    authority_tier = _authority_tier(signal, domain, market)
    primary = authority_tier in {"official_regulator_or_exchange", "official_disclosure_platform"}
    classification = _signal_classification(signal)
    source_material = {
        "signal_id": signal_id,
        "source": signal.get("source", ""),
        "source_type": signal.get("source_type", ""),
        "title": signal.get("title", ""),
        "summary": signal.get("summary", ""),
        "url": url,
        "published_at": signal.get("published_at", ""),
        "collected_at": signal.get("collected_at", ""),
        "payload": payload,
    }
    limitations: List[str] = []
    if not signal_url and not url:
        limitations.append("no web URL; retained structured signal record is the only locator")
    if authority_tier in {"secondary_aggregator", "unknown"}:
        limitations.append("source is not an eligible official primary source")
    if str(signal.get("source_type") or "") == "price" and str(market.get("status") or "") != "PASS":
        limitations.append("market-data governance did not pass")
    return {
        "evidence_id": "evidence:{0}:source".format(signal_id),
        "signal_id": signal_id,
        "record_type": "signal_source_record",
        "source_name": str(signal.get("source") or "unknown"),
        "source_type": str(signal.get("source_type") or "unknown"),
        "source_url": url,
        "origin_signal_url": signal_url,
        "source_domain": domain,
        "published_at": str(signal.get("published_at") or ""),
        "observed_at": str(signal.get("collected_at") or payload.get("observed_at") or ""),
        "quote_timestamp": str(payload.get("quote_timestamp") or market.get("quote_timestamp") or ""),
        "authority_tier": authority_tier,
        "primary_source_eligible": primary,
        "data_classification": classification,
        "provider_governance_status": str(market.get("status") or "not_applicable"),
        "provider_internally_attested": bool(market.get("provider_internally_attested", False)),
        "source_locator": str(retained.get("object_locator") or "sqlite:signals:{0}".format(signal_id)),
        "original_document_retained": bool(retained) or _is_retained_local_document(url),
        "content_sha256": str(retained.get("sha256") or object_hash(source_material)),
        "vault_document_id": str(retained.get("document_id") or ""),
        "vault_capture_mode": str(retained.get("source_binding_status") or ""),
        "vault_captured_at": str(retained.get("captured_at") or ""),
        "vault_content_type": str(retained.get("content_type") or ""),
        "raw_document_sent_to_external_llm": False,
        "limitations": limitations,
    }


def _claims_for_signals(
    signals: Sequence[Dict[str, Any]],
    source_records: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    records = {str(item.get("signal_id") or ""): item for item in source_records}
    claims: List[Dict[str, Any]] = []
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        evidence = records.get(signal_id, {})
        evidence_id = str(evidence.get("evidence_id") or "")
        title = str(signal.get("title") or "").strip()
        summary = str(signal.get("summary") or "").strip()
        if title:
            claims.append(_claim(
                signal_id, "source_statement", title, [evidence_id], "source_attributed",
                as_of=str(signal.get("published_at") or ""),
                limitations=["attributed to the retained source record; not independently verified by the system"],
            ))
        if summary and summary != title:
            claims.append(_claim(
                signal_id, "source_statement", summary, [evidence_id], "source_attributed",
                as_of=str(signal.get("published_at") or ""),
                limitations=["source summary may be extracted or normalized; compare with the original document"],
            ))
        payload = signal.get("payload", {}) if isinstance(signal.get("payload"), dict) else {}
        if str(signal.get("source_type") or "") == "price":
            claims.extend(_market_claims(signal, payload, evidence))
        claims.extend(_filing_claims(signal, payload, evidence_id))
        llm = signal.get("llm_analysis", {}) if isinstance(signal.get("llm_analysis"), dict) else {}
        if llm.get("key_thesis"):
            claims.append(_model_claim(signal, llm, str(llm.get("key_thesis")), evidence_id, "signal_llm"))
        for role in signal.get("role_analyses", []) or []:
            if isinstance(role, dict) and role.get("thesis"):
                claims.append(_model_claim(signal, role, str(role.get("thesis")), evidence_id, "role_llm"))
    unique: Dict[str, Dict[str, Any]] = {}
    for claim in claims:
        unique[str(claim.get("claim_id") or object_hash(claim))] = claim
    return list(unique.values())


def _market_claims(signal: Dict[str, Any], payload: Dict[str, Any], evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics = {
        "latest": ("latest price", "CNY/share", "CNY"),
        "pct_change": ("percentage change", "%", ""),
        "price_change": ("price change", "CNY/share", "CNY"),
        "amount": ("turnover amount", "CNY", "CNY"),
        "main_net_inflow": ("reported main net inflow", "CNY", "CNY"),
        "volume": ("reported volume", "unknown", ""),
    }
    governance = payload.get("market_data_governance", {}) if isinstance(payload.get("market_data_governance"), dict) else {}
    passed = str(governance.get("status") or "") == "PASS" and bool(governance.get("provider_internally_attested", False))
    support = "verified_for_internal_research" if passed else "blocked"
    limitations = [] if passed else _clean_list(governance.get("reason_codes", [])) or ["market-data governance not passed"]
    rows = []
    for key, (label, unit, currency) in metrics.items():
        if payload.get(key) in (None, ""):
            continue
        text = "{0}={1}".format(label, payload.get(key))
        rows.append(_claim(
            str(signal.get("signal_id") or ""),
            "data_observation",
            text,
            [str(evidence.get("evidence_id") or "")],
            support,
            as_of=str(payload.get("quote_timestamp") or signal.get("published_at") or ""),
            unit=unit,
            currency=currency,
            scope="market_snapshot.{0}".format(key),
            limitations=limitations,
        ))
    return rows


def _filing_claims(signal: Dict[str, Any], payload: Dict[str, Any], evidence_id: str) -> List[Dict[str, Any]]:
    filing = payload.get("filing_evidence", {}) if isinstance(payload.get("filing_evidence"), dict) else {}
    period = str(filing.get("report_period") or payload.get("report_period") or "unknown")
    rows = []
    for point in _clean_list(filing.get("key_points", []) or payload.get("key_points", [])):
        rows.append(_claim(
            str(signal.get("signal_id") or ""), "source_statement", point, [evidence_id], "source_attributed",
            period=period, as_of=str(filing.get("published_at") or signal.get("published_at") or ""),
            limitations=["extracted key point; verify wording and context against the original filing"],
        ))
    metrics = filing.get("metrics", {}) if isinstance(filing.get("metrics"), dict) else {}
    if not metrics and isinstance(payload.get("metrics"), dict):
        metrics = payload.get("metrics", {})
    for name, value in sorted(metrics.items()):
        if isinstance(value, dict):
            metric_value = value.get("value")
            unit = str(value.get("unit") or "unknown")
            currency = str(value.get("currency") or "")
        else:
            metric_value = value
            unit = "unknown"
            currency = ""
        rows.append(_claim(
            str(signal.get("signal_id") or ""), "data_observation",
            "{0}={1}".format(name, metric_value), [evidence_id], "source_attributed",
            period=period, as_of=str(filing.get("published_at") or signal.get("published_at") or ""),
            unit=unit, currency=currency, scope="filing_metric.{0}".format(name),
            limitations=["unit and accounting scope remain unknown unless explicitly supplied by the source"],
        ))
    return rows


def _model_claim(
    signal: Dict[str, Any],
    result: Dict[str, Any],
    text: str,
    evidence_id: str,
    component: str,
) -> Dict[str, Any]:
    grounding = str(result.get("grounding_status") or "unknown")
    valid_refs = _clean_list(result.get("valid_evidence_refs", []))
    unsupported = _clean_list(result.get("unsupported_evidence_refs", []))
    if grounding == "grounded" and valid_refs and not unsupported:
        support = "grounded_in_input"
    elif grounding == "partial" and valid_refs:
        support = "partial_grounding"
    else:
        support = "blocked"
    limitations = _clean_list(result.get("governance_warnings", []))
    if not valid_refs:
        limitations.append("no validated model evidence reference")
    execution = result.get("model_execution", {}) if isinstance(result.get("model_execution"), dict) else {}
    return _claim(
        str(signal.get("signal_id") or ""), "model_inference", text, [evidence_id] if evidence_id else [], support,
        as_of=str(signal.get("published_at") or ""), confidence=result.get("confidence"),
        scope=component, limitations=limitations,
        model_evidence_refs=_clean_list(result.get("evidence_refs", [])),
        valid_model_evidence_refs=valid_refs,
        model_execution=execution,
    )


def _claim(
    signal_id: str,
    claim_type: str,
    text: str,
    evidence_refs: Sequence[str],
    support_status: str,
    *,
    period: str = "unknown",
    as_of: str = "",
    unit: str = "not_applicable",
    currency: str = "",
    scope: str = "",
    confidence: Any = None,
    limitations: Optional[Sequence[str]] = None,
    model_evidence_refs: Optional[Sequence[str]] = None,
    valid_model_evidence_refs: Optional[Sequence[str]] = None,
    model_execution: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    basis = {
        "signal_id": signal_id,
        "claim_type": claim_type,
        "text": text,
        "scope": scope,
        "period": period,
    }
    return {
        "claim_id": "claim:{0}".format(object_hash(basis)[:20]),
        "signal_id": signal_id,
        "claim_type": claim_type,
        "text": str(text),
        "support_status": support_status,
        "evidence_refs": _clean_list(evidence_refs),
        "model_evidence_refs": _clean_list(model_evidence_refs or []),
        "valid_model_evidence_refs": _clean_list(valid_model_evidence_refs or []),
        "period": str(period or "unknown"),
        "as_of": str(as_of or ""),
        "unit": str(unit or "unknown"),
        "currency": str(currency or ""),
        "scope": str(scope or ""),
        "confidence": _optional_float(confidence),
        "conflicts": [],
        "limitations": _unique(limitations or []),
        "model_execution": model_execution or {},
    }


def _analysis_state(source_case: Dict[str, Any], signals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    case_analysis = source_case.get("case_analysis", {}) if isinstance(source_case.get("case_analysis"), dict) else {}
    signal_analyses = []
    role_views = []
    for signal in signals:
        llm = signal.get("llm_analysis", {}) if isinstance(signal.get("llm_analysis"), dict) else {}
        if llm:
            signal_analyses.append({"signal_id": signal.get("signal_id", ""), **llm})
        for role in signal.get("role_analyses", []) or []:
            if isinstance(role, dict):
                role_views.append({"signal_id": signal.get("signal_id", ""), **role})
    return {
        "case_analysis": case_analysis,
        "signal_llm_analyses": signal_analyses,
        "role_analyses": role_views,
        "analysis_is_automated_draft": True,
        "human_judgment_recorded": False,
        "facts_must_not_be_inferred_from_model_output": True,
    }


def _challenge_state(
    runtime_case: Dict[str, Any],
    source_case: Dict[str, Any],
    signals: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    counter_questions = []
    alternatives = []
    falsification = []
    case_analysis = source_case.get("case_analysis", {}) if isinstance(source_case.get("case_analysis"), dict) else {}
    counter_questions.extend(_clean_list(case_analysis.get("counter_questions", [])))
    for signal in signals:
        llm = signal.get("llm_analysis", {}) if isinstance(signal.get("llm_analysis"), dict) else {}
        counter_questions.extend(_clean_list(llm.get("review_questions", [])))
        falsification.extend(_clean_list(llm.get("risk_points", [])))
        for role in signal.get("role_analyses", []) or []:
            if not isinstance(role, dict):
                continue
            counter_questions.extend(_clean_list(role.get("review_questions", [])))
            alternatives.extend(_clean_list(role.get("counter_arguments", [])))
            falsification.extend(_clean_list(role.get("risk_points", [])))
        synthesis = signal.get("role_synthesis", {}) if isinstance(signal.get("role_synthesis"), dict) else {}
        if synthesis.get("key_disagreement"):
            alternatives.append(str(synthesis.get("key_disagreement")))
    override = (runtime_case.get("task_overrides", {}) or {}).get("independent_challenge", {})
    if not isinstance(override, dict):
        override = {}
    references = _clean_list(override.get("references", []))
    completed = (
        override.get("status") == "completed"
        and str(override.get("actor_role") or "") == "risk_reviewer"
        and any(item.lower().startswith("risk_note:") for item in references)
        and not _is_automated_actor(str(override.get("actor") or ""))
    )
    return {
        "agent_generated_counter_questions": _unique(counter_questions)[:100],
        "agent_generated_alternative_explanations": _unique(alternatives)[:100],
        "agent_generated_falsification_points": _unique(falsification)[:100],
        "agent_output_is_not_independent_human_challenge": True,
        "independent_human_challenge_completed": completed,
        "human_challenge": {
            "status": "completed" if completed else "pending",
            "actor": str(override.get("actor") or ""),
            "actor_role": str(override.get("actor_role") or ""),
            "completed_at": str(override.get("completed_at") or ""),
            "references": references,
            "notes": str(override.get("notes") or ""),
        },
    }


def _workflow_state(runtime_case: Dict[str, Any], source_case: Dict[str, Any]) -> Dict[str, Any]:
    approvals = runtime_case.get("approvals", {}) if isinstance(runtime_case.get("approvals"), dict) else {}
    normalized = {}
    for gate in ("quality_review", "compliance_review", "publication"):
        raw = approvals.get(gate, {}) if isinstance(approvals.get(gate), dict) else {}
        normalized[gate] = {
            "status": str(raw.get("status") or "pending"),
            "actor": str(raw.get("actor") or ""),
            "actor_role": str(raw.get("actor_role") or ""),
            "notes": str(raw.get("notes") or ""),
            "decided_at": str(raw.get("decided_at") or ""),
            "checks": _clean_list(raw.get("checks", [])),
        }
    return {
        "stage": str(runtime_case.get("stage") or "analysis_draft"),
        "runtime_status": str(runtime_case.get("status") or source_case.get("status") or "active"),
        "assignments": dict(runtime_case.get("assignments", {}) or {}),
        "task_assignments": dict(runtime_case.get("task_assignments", {}) or {}),
        "approvals": normalized,
        "published_at": str(runtime_case.get("published_at") or ""),
        "monitoring_started_at": str(runtime_case.get("monitoring_started_at") or ""),
        "closed_at": str(runtime_case.get("closed_at") or ""),
        "runtime_action_ids": _clean_list(
            item.get("action_id")
            for item in runtime_case.get("action_history", []) or []
            if isinstance(item, dict)
        ),
        "protected_gates_require_rostered_humans": True,
    }


def _governance_state(signals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    llm_audits = []
    market_gates = []
    classifications = set()
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        payload = signal.get("payload", {}) if isinstance(signal.get("payload"), dict) else {}
        market = payload.get("market_data_governance", {}) if isinstance(payload.get("market_data_governance"), dict) else {}
        if market:
            market_gates.append({
                "signal_id": signal_id,
                "status": str(market.get("status") or "UNKNOWN"),
                "provider": str(market.get("provider") or signal.get("source") or ""),
                "provider_internally_attested": bool(market.get("provider_internally_attested", False)),
                "quote_timestamp": str(market.get("quote_timestamp") or ""),
                "reason_codes": _clean_list(market.get("reason_codes", [])),
            })
        governance = signal.get("llm_governance", {}) if isinstance(signal.get("llm_governance"), dict) else {}
        for component, decision in governance.items():
            if not isinstance(decision, dict):
                continue
            classifications.add(str(decision.get("classification") or "unknown"))
            llm_audits.append({
                "signal_id": signal_id,
                "component": str(component),
                "audit_id": str(decision.get("audit_id") or ""),
                "classification": str(decision.get("classification") or "unknown"),
                "decision": str(decision.get("decision") or "unknown"),
                "payload_sha256": str(decision.get("payload_sha256") or ""),
                "postflight_status": str(((decision.get("postflight", {}) or {}).get("status") or "")),
            })
    return {
        "data_classifications": sorted(item for item in classifications if item),
        "llm_governance_audits": llm_audits,
        "market_data_gates": market_gates,
        "all_market_data_passed": all(item.get("status") == "PASS" for item in market_gates) if market_gates else True,
        "external_model_exception_auto_approved": False,
    }


def _method_state(
    signals: Sequence[Dict[str, Any]],
    source_case: Dict[str, Any],
    skill_status: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    executions: List[Dict[str, Any]] = []
    used_roles = set()
    for signal in signals:
        llm = signal.get("llm_analysis", {}) if isinstance(signal.get("llm_analysis"), dict) else {}
        if llm:
            used_roles.add("signal_analyst")
            executions.extend(_execution_rows("signal_llm", signal.get("signal_id", ""), [llm]))
        roles = [item for item in signal.get("role_analyses", []) or [] if isinstance(item, dict)]
        if roles:
            used_roles.add("research_desk")
            synthesis = signal.get("role_synthesis", {}) if isinstance(signal.get("role_synthesis"), dict) else {}
            executions.extend(_execution_rows("role_llm", signal.get("signal_id", ""), roles + [synthesis]))
    if isinstance(source_case.get("case_analysis"), dict) and source_case.get("case_analysis"):
        used_roles.add("case_analyst")
    bundles = skill_status.get("role_bundles", {}) if isinstance(skill_status.get("role_bundles"), dict) else {}
    role_methods = []
    for role in sorted(used_roles):
        bundle = bundles.get(role, {}) if isinstance(bundles.get(role), dict) else {}
        role_methods.append({
            "role_id": role,
            "skill_ids": _clean_list(bundle.get("skill_ids", [])),
            "role_bundle_sha256": str(bundle.get("sha256") or ""),
            "application_status": "audited_execution" if any(item.get("role_id") == role and item.get("pi_audit_event_id") for item in executions) else "prompt_context_declared_not_per_output_verified",
        })
    return {
        "supply_chain_status": str(skill_status.get("status") or "UNKNOWN"),
        "supply_chain_sha256": str(skill_status.get("bundle_sha256") or ""),
        "registry_sha256": str(skill_status.get("registry_sha256") or ""),
        "lock_sha256": str(skill_status.get("lock_sha256") or ""),
        "role_methods": role_methods,
        "model_executions": _dedupe_dicts(executions),
        "dynamic_skills_enabled": False,
        "tools_enabled": False,
        "method_application_limit": "A locked role bundle proves prompt context; per-call Pi audit IDs prove execution, not the correctness of a conclusion.",
    }


def _execution_rows(component: str, signal_id: Any, values: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    role_id = MODEL_ROLES.get(component, "")
    for value in values:
        execution = value.get("model_execution", {}) if isinstance(value.get("model_execution"), dict) else {}
        if not execution:
            continue
        fingerprint = str(execution.get("system_fingerprint") or "")
        event_id = str(execution.get("pi_audit_event_id") or "")
        if not event_id and fingerprint.startswith("pi:"):
            event_id = fingerprint.rsplit(":", 1)[-1]
        rows.append({
            "component": component,
            "role_id": role_id,
            "signal_id": str(signal_id or ""),
            "runtime": str(execution.get("runtime") or ""),
            "provider": str(execution.get("provider") or ""),
            "model": str(execution.get("model") or ""),
            "request_id": str(execution.get("request_id") or ""),
            "system_fingerprint": fingerprint,
            "pi_audit_event_id": event_id,
        })
    return rows


def _projection_from_chain(chain: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    latest: Dict[str, Tuple[int, Dict[str, Any], Dict[str, Any]]] = {}
    for event in chain.get("records", []) or []:
        workpaper = event.get("workpaper")
        if not isinstance(workpaper, dict):
            continue
        workpaper_id = str(workpaper.get("workpaper_id") or event.get("workpaper_id") or "")
        version = int(workpaper.get("version", 0) or 0)
        current = latest.get(workpaper_id)
        if current is None or version >= current[0]:
            latest[workpaper_id] = (version, workpaper, event)
    workpapers = []
    for _, workpaper, event in latest.values():
        item = copy.deepcopy(workpaper)
        item["audit_event_id"] = str(event.get("event_id") or "")
        item["audit_event_hash"] = str(event.get("event_hash") or "")
        workpapers.append(item)
    workpapers.sort(key=lambda item: (str(item.get("case_id") or ""), int(item.get("version", 0) or 0)))
    summary = _summarize(workpapers, chain, cfg)
    return {
        "schema_version": WORKPAPER_SCHEMA_VERSION,
        "policy_version": WORKPAPER_POLICY_VERSION,
        "generated_at": now_iso(),
        "integrity_status": chain.get("integrity_status", "UNKNOWN"),
        "event_count": chain.get("record_count", 0),
        "invalid_event_count": chain.get("invalid_record_count", 0),
        "last_event_hash": chain.get("last_hash", ""),
        "integrity_errors": chain.get("errors", []),
        "policy": _policy_projection(cfg),
        "summary": summary,
        "workpapers": workpapers,
        "safety_boundary": SAFETY_BOUNDARY,
    }


def _summarize(workpapers: Sequence[Dict[str, Any]], chain: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    statuses = Counter(str(item.get("status") or "unknown") for item in workpapers)
    validation = Counter(str((item.get("validation", {}) or {}).get("status") or "UNKNOWN") for item in workpapers)
    claim_types = Counter(
        str(claim.get("claim_type") or "unknown")
        for item in workpapers
        for claim in item.get("claims", []) or []
        if isinstance(claim, dict)
    )
    source_tiers = Counter(
        str(source.get("authority_tier") or "unknown")
        for item in workpapers
        for source in item.get("source_records", []) or []
        if isinstance(source, dict)
    )
    pending_human = sum(
        str((item.get("workflow", {}) or {}).get("stage") or "") in {"challenge", "quality_review", "compliance_review", "human_approval"}
        for item in workpapers
    )
    approval_eligible = sum(bool((item.get("validation", {}) or {}).get("approval_eligible")) for item in workpapers)
    archive_ready = bool(workpapers) and all(
        bool((item.get("retention", {}) or {}).get("production_archive_approved"))
        for item in workpapers
    )
    return {
        "workpaper_count": len(workpapers),
        "event_count": chain.get("record_count", 0),
        "invalid_event_count": chain.get("invalid_record_count", 0),
        "integrity_status": chain.get("integrity_status", "UNKNOWN"),
        "status_counts": dict(sorted(statuses.items())),
        "validation_status_counts": dict(sorted(validation.items())),
        "claim_type_counts": dict(sorted(claim_types.items())),
        "source_authority_counts": dict(sorted(source_tiers.items())),
        "approval_eligible_count": approval_eligible,
        "pending_human_count": pending_human,
        "production_archive_ready": archive_ready,
        "business_status": "READY" if workpapers and approval_eligible == len(workpapers) and archive_ready else "BLOCKED",
    }


def _result(
    projection: Dict[str, Any],
    paths: Dict[str, str],
    *,
    created: int,
    unchanged: int,
    preserved: int,
    policy_errors: Sequence[str],
) -> Dict[str, Any]:
    summary = projection.get("summary", {}) or {}
    return {
        "research_workpapers_enabled": True,
        "research_workpaper_created_count": created,
        "research_workpaper_unchanged_count": unchanged,
        "research_workpaper_preserved_count": preserved,
        "research_workpaper_count": summary.get("workpaper_count", 0),
        "research_workpaper_event_count": summary.get("event_count", 0),
        "research_workpaper_integrity_status": projection.get("integrity_status", "UNKNOWN"),
        "research_workpaper_business_status": summary.get("business_status", "BLOCKED"),
        "research_workpaper_policy_errors": list(policy_errors),
        "research_workpapers": summary,
        "research_workpaper_items": [_compact_workpaper(item) for item in projection.get("workpapers", [])],
        **paths,
    }


def _disabled_result() -> Dict[str, Any]:
    return {
        "research_workpapers_enabled": False,
        "research_workpaper_created_count": 0,
        "research_workpaper_unchanged_count": 0,
        "research_workpaper_preserved_count": 0,
        "research_workpaper_count": 0,
        "research_workpaper_event_count": 0,
        "research_workpaper_integrity_status": "DISABLED",
        "research_workpaper_business_status": "DISABLED",
        "research_workpapers": {},
        "research_workpaper_items": [],
    }


def _write_projection(projection: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    atomic_write_text(
        project_path(cfg["projection_path"]),
        json.dumps(projection, ensure_ascii=False, indent=2, default=str) + "\n",
    )


def _render_markdown(projection: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    summary = projection.get("summary", {}) or {}
    lines = [
        "# Research Workpapers",
        "",
        "- Generated at: {0}".format(projection.get("generated_at", "")),
        "- Hash-chain integrity: {0}".format(projection.get("integrity_status", "UNKNOWN")),
        "- Workpapers / immutable events: {0} / {1}".format(summary.get("workpaper_count", 0), summary.get("event_count", 0)),
        "- Approval eligible drafts: {0}".format(summary.get("approval_eligible_count", 0)),
        "- Pending human gates: {0}".format(summary.get("pending_human_count", 0)),
        "- Business status: {0}".format(summary.get("business_status", "BLOCKED")),
        "- Production archive approved: {0}".format("yes" if summary.get("production_archive_ready") else "no"),
        "- Safety: {0}".format(SAFETY_BOUNDARY),
        "",
        "The local hash chain is an auditable shadow ledger, not an approved production archive. "
        "Facts, source-attributed statements, data observations, model inferences, and human approvals are recorded separately.",
        "",
        "## Case Workpapers",
        "",
        "| Case | Version | Priority | Stage | Validation | Sources | Claims | Official primary | Archive |",
        "|---|---:|---|---|---|---:|---:|---|---|",
    ]
    for item in projection.get("workpapers", []) or []:
        validation = item.get("validation", {}) or {}
        source_records = item.get("source_records", []) or []
        official = any(source.get("primary_source_eligible") for source in source_records if isinstance(source, dict))
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |".format(
            item.get("case_id", ""),
            item.get("version", 0),
            (item.get("case", {}) or {}).get("priority", ""),
            (item.get("workflow", {}) or {}).get("stage", ""),
            validation.get("status", "UNKNOWN"),
            len(source_records),
            len(item.get("claims", []) or []),
            "yes" if official else "no",
            "approved" if (item.get("retention", {}) or {}).get("production_archive_approved") else "shadow",
        ))
        if validation.get("errors"):
            lines.append("")
            lines.append("- {0} blockers: {1}".format(item.get("case_id", ""), "; ".join(validation.get("errors", [])[:5])))
    if not projection.get("workpapers"):
        lines.append("| - | 0 | - | - | - | 0 | 0 | no | shadow |")
    if projection.get("integrity_errors"):
        lines.extend(["", "## Integrity Errors", ""])
        lines.extend("- {0}".format(item) for item in projection.get("integrity_errors", [])[:20])
    if projection.get("degradation", {}).get("reasons"):
        lines.extend(["", "## Degradation", ""])
        lines.extend("- {0}".format(item) for item in projection["degradation"]["reasons"])
    return "\n".join(lines) + "\n"


def _cases_from_sources(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    cases = report.get("research_cases", []) if isinstance(report, dict) else []
    if cases:
        return [item for item in cases if isinstance(item, dict)]
    try:
        from .research_cases import list_research_cases

        return list_research_cases()
    except Exception:
        return []


def _runtime_cases() -> List[Dict[str, Any]]:
    try:
        from .research_runtime import load_research_runtime

        return [item for item in load_research_runtime().get("cases", []) if isinstance(item, dict)]
    except Exception:
        return []


def _load_report(report_path: str = "") -> Dict[str, Any]:
    path = project_path(report_path) if report_path else _latest_file(project_path("reports/json"), "*.json")
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _signal_map(signals: Sequence[Any]) -> Dict[str, Dict[str, Any]]:
    result = {}
    for signal in signals:
        if isinstance(signal, dict):
            item = dict(signal)
        elif hasattr(signal, "to_dict"):
            item = signal.to_dict()
        else:
            continue
        signal_id = str(item.get("signal_id") or "").strip()
        if signal_id:
            result[signal_id] = item
    return result


def _load_persisted_signals(config: Dict[str, Any], signal_ids: Sequence[str]) -> List[Dict[str, Any]]:
    ids = _clean_list(signal_ids)
    if not ids:
        return []
    path = project_path(str((config.get("storage", {}) or {}).get("sqlite_path") or "data/stock_agent.sqlite3"))
    if not path.exists():
        return []
    store = SignalStore(path)
    try:
        return store.fetch_many(ids)
    finally:
        store.close()


def _safe_pi_skill_status(config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return pi_skill_supply_chain_status(config)
    except Exception as exc:
        return {"status": "BLOCKED", "errors": [str(exc)], "role_bundles": {}}


def _authority_tier(signal: Dict[str, Any], domain: str, market: Dict[str, Any]) -> str:
    if domain in {"csrc.gov.cn", "sac.net.cn", "sse.com.cn", "szse.cn", "bse.cn"}:
        return "official_regulator_or_exchange"
    if domain == "cninfo.com.cn":
        return "official_disclosure_platform"
    if str(signal.get("source_type") or "") == "price" and market.get("provider_internally_attested") is True:
        return "approved_internal_market_data"
    if domain or str(signal.get("source") or "") in {"eastmoney", "eastmoney_filing", "akshare", "pysnowball"}:
        return "secondary_aggregator"
    return "unknown"


def _signal_classification(signal: Dict[str, Any]) -> str:
    governance = signal.get("llm_governance", {}) if isinstance(signal.get("llm_governance"), dict) else {}
    values = [
        str(item.get("classification") or "")
        for item in governance.values()
        if isinstance(item, dict) and item.get("classification")
    ]
    if values:
        order = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
        return max(values, key=lambda item: order.get(item, 4))
    payload = signal.get("payload", {}) if isinstance(signal.get("payload"), dict) else {}
    for key in ("data_classification", "information_classification", "access_classification"):
        if payload.get(key):
            return str(payload.get(key))
    return "internal" if str(signal.get("source") or "") in {"local_jsonl", "manual"} else "public"


def _production_archive_state(config: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    vault_cfg = evidence_vault_config(config)
    if vault_cfg["enabled"]:
        verification = verify_evidence_vault(config)
        control = verification.get("archive_control", {}) or {}
        return {
            "ready": bool(
                verification.get("technical_status") == "READY"
                and verification.get("production_archive_ready") is True
            ),
            "backend": str(control.get("backend") or vault_cfg["archive_backend"]),
            "evidence_ref": str(control.get("evidence_ref") or vault_cfg["archive_evidence_ref"]),
            "policy_fingerprint": str(control.get("policy_fingerprint") or ""),
            "source": "official_evidence_vault_archive_control",
        }
    return {
        "ready": bool(cfg["production_archive_approved"] and cfg["production_archive_evidence_ref"]),
        "backend": cfg["archive_backend"],
        "evidence_ref": cfg["production_archive_evidence_ref"],
        "policy_fingerprint": "",
        "source": "legacy_research_workpaper_config",
    }


def _policy_projection(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "policy_version": WORKPAPER_POLICY_VERSION,
        "retention_years": cfg["retention_years"],
        "require_official_primary_for": cfg["require_official_primary_for"],
        "require_exact_evidence_refs": cfg["require_exact_evidence_refs"],
        "require_market_governance_pass": cfg["require_market_governance_pass"],
        "require_distinct_human_roles": cfg["require_distinct_human_roles"],
        "require_independent_challenge": cfg["require_independent_challenge"],
        "archive_backend": cfg["archive_backend"],
        "production_archive_approved": cfg["production_archive_approved"],
        "automatic_approval": False,
        "automatic_assignment": False,
        "automatic_publication": False,
        "automatic_trading": False,
    }


def _revision_core(workpaper: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "case", "source_records", "claims", "analysis", "challenge", "workflow",
        "governance", "methods", "retention", "policy", "safety_boundary",
    )
    return {key: workpaper.get(key) for key in keys}


def _workpaper_status(workflow: Dict[str, Any]) -> str:
    stage = str(workflow.get("stage") or "analysis_draft")
    mapping = {
        "intake": "draft_intake",
        "triage": "draft_triage",
        "evidence_collection": "draft_evidence",
        "analysis": "analysis_draft",
        "analysis_draft": "analysis_draft",
        "challenge": "challenge_pending",
        "quality_review": "quality_review_pending",
        "compliance_review": "compliance_review_pending",
        "human_approval": "publication_approval_pending",
        "published": "published_internal",
        "monitoring": "monitoring",
        "closed": "closed",
    }
    return mapping.get(stage, "analysis_draft")


def _workpaper_id(case_id: str) -> str:
    return "workpaper:{0}".format(hashlib.sha256(str(case_id).encode("utf-8")).hexdigest()[:20])


def _retention_until(value: str, years: int) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        try:
            retained = parsed.replace(year=parsed.year + years)
        except ValueError:
            retained = parsed.replace(month=2, day=28, year=parsed.year + years)
        return retained.isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def _domain(url: str) -> str:
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return ""
    for candidate in OFFICIAL_PRIMARY_DOMAINS:
        if host == candidate or host.endswith("." + candidate):
            return candidate
    return host


def _is_retained_local_document(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "file":
        return Path(parsed.path).exists()
    if not parsed.scheme:
        return project_path(url).exists()
    return False


def _is_automated_actor(actor: str) -> bool:
    normalized = str(actor or "").strip().lower().replace("-", "_")
    return any(normalized.startswith(prefix) for prefix in AUTOMATED_ACTOR_PREFIXES)


def _compact_workpaper(item: Dict[str, Any]) -> Dict[str, Any]:
    validation = item.get("validation", {}) or {}
    return {
        "workpaper_id": item.get("workpaper_id", ""),
        "case_id": item.get("case_id", ""),
        "version": item.get("version", 0),
        "status": item.get("status", ""),
        "priority": (item.get("case", {}) or {}).get("priority", ""),
        "entity": (item.get("case", {}) or {}).get("entity", ""),
        "workflow_stage": (item.get("workflow", {}) or {}).get("stage", ""),
        "validation_status": validation.get("status", "UNKNOWN"),
        "approval_eligible": validation.get("approval_eligible", False),
        "source_count": len(item.get("source_records", []) or []),
        "claim_count": len(item.get("claims", []) or []),
        "official_primary_present": any(
            source.get("primary_source_eligible") is True
            for source in item.get("source_records", []) or []
            if isinstance(source, dict)
        ),
        "production_archive_approved": (item.get("retention", {}) or {}).get("production_archive_approved", False),
        "audit_event_id": item.get("audit_event_id", ""),
        "updated_at": item.get("updated_at", ""),
        "blockers": validation.get("errors", [])[:10],
    }


def _dedupe_dicts(values: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for value in values:
        key = object_hash(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _latest_file(directory: Path, pattern: str) -> Optional[Path]:
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def _compact_time(value: Any) -> str:
    return str(value or now_iso()).replace("-", "").replace(":", "").replace("+", "_").replace(".", "")


def _clean_list(values: Iterable[Any]) -> List[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    return _unique(str(item).strip() for item in values if str(item).strip())


def _unique(values: Iterable[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
