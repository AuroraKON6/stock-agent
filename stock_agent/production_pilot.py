from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .audit_log import (
    AuditLogIntegrityError,
    append_hash_chained_jsonl,
    atomic_write_text,
    file_hash,
    load_hash_chained_jsonl,
    object_hash,
)
from .config import project_path
from .research_workpapers import load_research_workpapers, research_workpaper_config, verify_research_workpapers
from .evidence_vault import evidence_vault_config, verify_evidence_vault
from .llm_governance import load_latest_governance_summary
from .market_data_governance import load_latest_market_data_governance
from .model_operations import load_latest_model_operations
from .models import now_iso
from .agent_handoff import validate_agent_handoff
from .business_onboarding import build_business_onboarding_preflight
from .control_evidence import resolve_verified_control_evidence, validate_managed_archive_reference
from .openclaw_handoff import validate_openclaw_handoff
from .provider_registry import (
    build_provider_registry,
    provider_registry_config,
    write_provider_registry_snapshot,
)
from .research_operations import load_operations_inputs
from .review_queue import OPEN_STATUSES, build_review_queue


PRODUCTION_PILOT_VERSION = "25.0"
SAFETY_BOUNDARY = (
    "Internal research operations only. No automatic assignment, approval, external publication, "
    "trading, order placement, position change, or automatic production activation."
)
DEFAULT_APPROVAL_GATES = {
    "research_readiness": "lead_analyst",
    "quality_readiness": "quality_reviewer",
    "compliance_readiness": "compliance_officer",
    "information_security_readiness": "information_security_officer",
    "data_readiness": "data_steward",
    "publication_readiness": "publication_approver",
}
ROLE_AUTHORIZATIONS = {
    "compliance_officer": "compliance_authorized",
    "information_security_officer": "information_security_authorized",
    "data_steward": "vendor_data_steward_authorized",
    "publication_approver": "publication_authorized",
}
CONTROL_EVENT_TYPES = {
    "pilot_approval_recorded",
    "pilot_activation_recorded",
    "pilot_activation_revoked",
}
INCIDENT_EVENT_TYPES = {"incident_reported", "incident_action_recorded"}
INCIDENT_SEVERITIES = {"critical", "high", "medium", "low"}
INCIDENT_ACTIONS = {"acknowledge", "mitigate", "resolve", "reject_resolution"}
CRITICAL_REVIEW_ROLES = {"compliance_officer", "information_security_officer"}
TRADING_PATTERN = re.compile(
    r"(?:自动交易|自动下单|自动调仓|买入|卖出|下单|trade|order|position\s+change)", re.IGNORECASE
)


class ProductionPilotPolicyError(ValueError):
    pass


def production_pilot_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("production_pilot", {}) if isinstance(config, dict) else {}
    approval_gates = raw.get("required_approval_gates", DEFAULT_APPROVAL_GATES)
    if not isinstance(approval_gates, dict):
        approval_gates = dict(DEFAULT_APPROVAL_GATES)
    normalized_gates = {
        str(gate).strip(): str(role).strip()
        for gate, role in approval_gates.items()
        if str(gate).strip() and str(role).strip()
    }
    if not normalized_gates:
        normalized_gates = dict(DEFAULT_APPROVAL_GATES)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce": bool(raw.get("enforce", False)),
        "mode": str(raw.get("mode") or "shadow").strip().lower(),
        "control_event_path": str(raw.get("control_event_path") or "data/production_pilot/control_events.jsonl"),
        "incident_event_path": str(raw.get("incident_event_path") or "data/production_pilot/incident_events.jsonl"),
        "run_ledger_path": str(raw.get("run_ledger_path") or "data/production_pilot/run_ledger.jsonl"),
        "report_dir": str(raw.get("report_dir") or "reports/production_pilot"),
        "minimum_active_providers": max(1, int(raw.get("minimum_active_providers", 2) or 2)),
        "minimum_shadow_runs": max(1, int(raw.get("minimum_shadow_runs", 5) or 5)),
        "minimum_shadow_trading_days": max(1, int(raw.get("minimum_shadow_trading_days", 3) or 3)),
        "maximum_unreconciled_runs": max(0, int(raw.get("maximum_unreconciled_runs", 0) or 0)),
        "review_batch_size": max(1, int(raw.get("review_batch_size", 20) or 20)),
        "review_wip_limit_per_human": max(1, int(raw.get("review_wip_limit_per_human", 10) or 10)),
        "approval_validity_days": max(1, int(raw.get("approval_validity_days", 30) or 30)),
        "required_approval_gates": normalized_gates,
        "automatic_assignment": bool(raw.get("automatic_assignment", False)),
        "automatic_approval": bool(raw.get("automatic_approval", False)),
        "automatic_activation": bool(raw.get("automatic_activation", False)),
        "automatic_publication": bool(raw.get("automatic_publication", False)),
        "automatic_trading": bool(raw.get("automatic_trading", False)),
    }


def sync_production_pilot(
    config: Dict[str, Any],
    *,
    config_path: str = "",
    record_run: bool = True,
    scheduled_summary_path: str = "",
) -> Dict[str, Any]:
    cfg = production_pilot_config(config)
    provider_registry = build_provider_registry(config)
    write_provider_registry_snapshot(provider_registry)
    base = _collect_base_state(config, cfg, provider_registry, config_path=config_path,
                               scheduled_summary_path=scheduled_summary_path)
    reconciliation = _build_reconciliation(config, cfg, base, config_path=config_path,
                                            scheduled_summary_path=scheduled_summary_path)
    if record_run and reconciliation.get("run_identity") and reconciliation.get("scheduled_report_match"):
        _append_reconciliation_if_new(cfg, reconciliation)
    snapshot = _build_snapshot(config, cfg, base, reconciliation)
    paths = write_production_pilot_snapshot(snapshot, cfg)
    snapshot.update(paths)
    return snapshot


def load_latest_production_pilot(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = production_pilot_config(config or {})
    path = project_path(str(Path(cfg["report_dir"]) / "latest.json"))
    return _read_json(path)


def record_pilot_approval(
    config: Dict[str, Any],
    gate: str,
    decision: str,
    actor: str,
    actor_role: str,
    *,
    notes: str,
    evidence_refs: Sequence[str],
    config_path: str = "",
) -> Dict[str, Any]:
    cfg = production_pilot_config(config)
    gate = str(gate or "").strip()
    decision = str(decision or "").strip().lower()
    actor_role = str(actor_role or "").strip()
    expected_role = cfg["required_approval_gates"].get(gate)
    if not expected_role:
        raise ProductionPilotPolicyError("unsupported pilot approval gate")
    if actor_role != expected_role:
        raise ProductionPilotPolicyError("gate {0} requires actor role {1}".format(gate, expected_role))
    if decision not in {"approve", "reject"}:
        raise ProductionPilotPolicyError("decision must be approve or reject")
    _authorize_human(config, actor, actor_role)
    _require_onboarding_ready_approver(config, actor, actor_role)
    current = sync_production_pilot(config, config_path=config_path, record_run=False)
    fingerprint = str(current.get("candidate_fingerprint") or "")
    refs = _safe_references(
        evidence_refs, config=config, subject_id=fingerprint, categories=["approval_support"]
    )
    if not str(notes or "").strip() or not refs:
        raise ProductionPilotPolicyError("pilot approval requires notes and at least one evidence reference")
    control = _load_control_state(cfg, fingerprint)
    for other_gate, item in (control.get("approvals") or {}).items():
        if other_gate != gate and str(item.get("actor") or "") == str(actor or "").strip():
            raise ProductionPilotPolicyError("each required go-live gate must use a distinct human actor")
    record = _append_control_event(cfg, {
        "event_type": "pilot_approval_recorded",
        "candidate_fingerprint": fingerprint,
        "gate": gate,
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {
            "decision": decision,
            "notes": str(notes or "").strip(),
            "evidence_refs": refs,
            "approval_granted": decision == "approve",
        },
    })
    return {"record": record, "pilot": sync_production_pilot(config, config_path=config_path, record_run=False)}


def activate_production_candidate(
    config: Dict[str, Any],
    actor: str,
    actor_role: str,
    *,
    notes: str,
    evidence_refs: Sequence[str],
    config_path: str = "",
) -> Dict[str, Any]:
    cfg = production_pilot_config(config)
    if actor_role != "publication_approver":
        raise ProductionPilotPolicyError("production activation requires publication_approver")
    _authorize_human(config, actor, actor_role)
    _require_onboarding_ready_approver(config, actor, actor_role)
    current = sync_production_pilot(config, config_path=config_path, record_run=False)
    refs = _safe_references(
        evidence_refs,
        config=config,
        subject_id=str(current.get("candidate_fingerprint") or ""),
        categories=["approval_support"],
    )
    if not str(notes or "").strip() or not refs:
        raise ProductionPilotPolicyError("production activation requires notes and evidence references")
    if not current.get("candidate_ready_for_activation"):
        blockers = current.get("go_live_blockers", []) or []
        raise ProductionPilotPolicyError("candidate is not ready for activation: {0}".format("; ".join(blockers[:8])))
    publication = (current.get("approvals") or {}).get("publication_readiness", {})
    if str(publication.get("actor") or "") != str(actor or "").strip():
        raise ProductionPilotPolicyError("activation actor must be the recorded publication readiness approver")
    record = _append_control_event(cfg, {
        "event_type": "pilot_activation_recorded",
        "candidate_fingerprint": current["candidate_fingerprint"],
        "gate": "production_activation",
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {
            "notes": str(notes or "").strip(),
            "evidence_refs": refs,
            "approval_granted": True,
            "scope": "internal_research_operations_only",
        },
    })
    return {"record": record, "pilot": sync_production_pilot(config, config_path=config_path, record_run=False)}


def revoke_production_activation(
    config: Dict[str, Any],
    actor: str,
    actor_role: str,
    *,
    notes: str,
    config_path: str = "",
) -> Dict[str, Any]:
    if actor_role not in {"publication_approver", "compliance_officer", "information_security_officer"}:
        raise ProductionPilotPolicyError("activation revocation requires publication, compliance, or information security role")
    _authorize_human(config, actor, actor_role)
    if not str(notes or "").strip():
        raise ProductionPilotPolicyError("revocation reason is required")
    cfg = production_pilot_config(config)
    current = sync_production_pilot(config, config_path=config_path, record_run=False)
    record = _append_control_event(cfg, {
        "event_type": "pilot_activation_revoked",
        "candidate_fingerprint": current.get("candidate_fingerprint", ""),
        "gate": "production_activation",
        "actor": str(actor or "").strip(),
        "actor_role": actor_role,
        "payload": {"notes": str(notes or "").strip(), "approval_granted": False},
    })
    return {"record": record, "pilot": sync_production_pilot(config, config_path=config_path, record_run=False)}


def record_pilot_incident(
    config: Dict[str, Any],
    title: str,
    severity: str,
    category: str,
    actor: str,
    actor_role: str,
    *,
    notes: str,
    evidence_refs: Sequence[str],
) -> Dict[str, Any]:
    cfg = production_pilot_config(config)
    severity = str(severity or "").strip().lower()
    if severity not in INCIDENT_SEVERITIES:
        raise ProductionPilotPolicyError("invalid incident severity")
    title = str(title or "").strip()
    category = str(category or "").strip().lower()
    if not title or not category or not str(notes or "").strip():
        raise ProductionPilotPolicyError("incident title, category, and notes are required")
    refs = _safe_references(evidence_refs, config=config)
    if not refs:
        raise ProductionPilotPolicyError("incident evidence reference is required")
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    principal_type = "service"
    inputs = load_operations_inputs(config)
    principal = (inputs.get("principal_map") or {}).get(actor)
    if principal and principal.get("principal_type") == "human":
        _authorize_human(config, actor, actor_role)
        principal_type = "human"
    elif principal:
        if not principal.get("active"):
            raise ProductionPilotPolicyError("incident reporter service principal is inactive")
    elif actor not in {"", "system:stock_agent", "system:scheduler", "system:pi", "system:openclaw"}:
        raise ProductionPilotPolicyError("incident reporter must be a registered principal or approved system reporter")
    incident_id = "inc_{0}".format(object_hash({"title": title, "category": category, "at": now_iso()})[:16])
    record = _append_incident_event(cfg, {
        "event_type": "incident_reported",
        "incident_id": incident_id,
        "actor": actor or "system:stock_agent",
        "actor_role": actor_role or "system_monitor",
        "principal_type": principal_type,
        "payload": {
            "title": title,
            "severity": severity,
            "category": category,
            "notes": str(notes or "").strip(),
            "evidence_refs": refs,
        },
    })
    return {"record": record, "incidents": _load_incidents(cfg)}


def record_pilot_incident_action(
    config: Dict[str, Any],
    incident_id: str,
    action: str,
    actor: str,
    actor_role: str,
    *,
    notes: str,
    evidence_refs: Sequence[str],
) -> Dict[str, Any]:
    cfg = production_pilot_config(config)
    incident_id = str(incident_id or "").strip()
    action = str(action or "").strip().lower()
    if action not in INCIDENT_ACTIONS:
        raise ProductionPilotPolicyError("invalid incident action")
    incidents = _load_incidents(cfg)
    incident = (incidents.get("incidents") or {}).get(incident_id)
    if not incident:
        raise ProductionPilotPolicyError("incident not found")
    allowed_roles = (
        CRITICAL_REVIEW_ROLES
        if incident.get("severity") in {"critical", "high"}
        else {"research_coordinator", "data_steward", "compliance_officer", "information_security_officer"}
    )
    if actor_role not in allowed_roles:
        raise ProductionPilotPolicyError("actor role is not authorized for this incident severity")
    _authorize_human(config, actor, actor_role)
    refs = _safe_references(evidence_refs, config=config)
    if not str(notes or "").strip() or not refs:
        raise ProductionPilotPolicyError("incident action requires notes and evidence references")
    if action == "resolve" and str(incident.get("reported_by") or "") == str(actor or "").strip():
        raise ProductionPilotPolicyError("incident reporter cannot independently resolve the same incident")
    if action == "resolve" and incident.get("severity") in {"critical", "high"}:
        prior_resolvers = {
            str(row.get("actor") or "")
            for row in incident.get("actions", [])
            if row.get("action") == "resolve" and row.get("actor_role") != actor_role
        }
        if str(actor or "").strip() in prior_resolvers:
            raise ProductionPilotPolicyError("dual incident resolution requires distinct human actors")
    record = _append_incident_event(cfg, {
        "event_type": "incident_action_recorded",
        "incident_id": incident_id,
        "actor": str(actor or "").strip(),
        "actor_role": str(actor_role or "").strip(),
        "principal_type": "human",
        "payload": {
            "action": action,
            "notes": str(notes or "").strip(),
            "evidence_refs": refs,
        },
    })
    return {"record": record, "incidents": _load_incidents(cfg)}


def write_production_pilot_snapshot(snapshot: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    cfg = cfg or production_pilot_config({})
    report_dir = project_path(cfg["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_stamp(str(snapshot.get("generated_at") or now_iso()))
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    history_json = report_dir / "pilot_{0}.json".format(stamp)
    history_md = report_dir / "pilot_{0}.md".format(stamp)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    markdown = render_production_pilot_markdown(snapshot)
    for path in (latest_json, history_json):
        atomic_write_text(path, payload)
    for path in (latest_md, history_md):
        atomic_write_text(path, markdown, encoding="utf-8-sig")
    return {
        "production_pilot_json_path": str(history_json),
        "production_pilot_path": str(history_md),
        "production_pilot_latest_json_path": str(latest_json),
        "production_pilot_latest_path": str(latest_md),
    }


def render_production_pilot_markdown(snapshot: Dict[str, Any]) -> str:
    lines = [
        "# Controlled Production Pilot",
        "",
        "- generated_at: {0}".format(snapshot.get("generated_at", "")),
        "- configured / effective mode: {0} / {1}".format(
            snapshot.get("configured_mode", ""), snapshot.get("effective_mode", "")
        ),
        "- technical readiness: {0}".format(snapshot.get("technical_readiness", "")),
        "- candidate readiness: {0}".format(snapshot.get("candidate_readiness", "")),
        "- go-live decision: {0}".format(snapshot.get("go_live_decision", "")),
        "- candidate fingerprint: {0}".format(snapshot.get("candidate_fingerprint", "")),
        "- automatic activation / publication / trading: false / false / false",
        "",
        "## Go/No-Go Gates",
        "",
        "| Gate | Status | Detail |",
        "|---|---|---|",
    ]
    for gate in snapshot.get("gates", []) or []:
        detail = str(gate.get("detail") or "").replace("|", "/")
        lines.append("| {0} | {1} | {2} |".format(gate.get("gate", ""), gate.get("status", ""), detail))
    lines.extend(["", "## Blocking Actions", ""])
    blockers = snapshot.get("go_live_blockers", []) or []
    if blockers:
        for item in blockers:
            lines.append("- {0}".format(item))
    else:
        lines.append("- none")
    provider = snapshot.get("provider_registry", {}) or {}
    lines.extend([
        "",
        "## Provider Readiness",
        "",
        "- registry integrity: {0}".format(provider.get("integrity_status", "UNKNOWN")),
        "- active providers: {0}".format(", ".join(provider.get("active_provider_ids", [])) or "none"),
        "",
        "## Shadow Evidence",
        "",
    ])
    shadow = snapshot.get("shadow_evidence", {}) or {}
    lines.extend([
        "- qualifying runs: {0} / {1}".format(shadow.get("qualifying_run_count", 0), shadow.get("minimum_runs", 0)),
        "- qualifying trading days: {0} / {1}".format(
            shadow.get("qualifying_trading_day_count", 0), shadow.get("minimum_trading_days", 0)
        ),
        "- unreconciled runs: {0}".format(shadow.get("unreconciled_run_count", 0)),
        "",
        "## Review Capacity Plan",
        "",
    ])
    capacity = snapshot.get("review_capacity", {}) or {}
    lines.extend([
        "- outstanding / overdue / unassigned: {0} / {1} / {2}".format(
            capacity.get("outstanding_count", 0), capacity.get("overdue_count", 0), capacity.get("unassigned_count", 0)
        ),
        "- eligible human reviewers / WIP capacity: {0} / {1}".format(
            capacity.get("eligible_human_reviewer_count", 0), capacity.get("available_wip_capacity", 0)
        ),
        "- suggested batches: {0}; assignment remains manual".format(capacity.get("suggested_batch_count", 0)),
        "",
        "## Incidents",
        "",
    ])
    incidents = snapshot.get("incidents", {}) or {}
    lines.append("- open critical / high / total: {0} / {1} / {2}".format(
        incidents.get("open_critical_count", 0), incidents.get("open_high_count", 0), incidents.get("open_count", 0)
    ))
    lines.extend(["", "## Boundary", "", "- {0}".format(SAFETY_BOUNDARY)])
    return "\n".join(lines).rstrip() + "\n"


def _collect_base_state(
    config: Dict[str, Any],
    cfg: Dict[str, Any],
    provider_registry: Dict[str, Any],
    *,
    config_path: str,
    scheduled_summary_path: str = "",
) -> Dict[str, Any]:
    operations = load_operations_inputs(config)
    roster = _roster_readiness(operations, cfg)
    queue = build_review_queue(config)
    review_capacity = _review_capacity(queue, operations, cfg)
    market = load_latest_market_data_governance(config)
    llm = load_latest_governance_summary(config)
    model = load_latest_model_operations(config)
    workpapers = verify_research_workpapers(config)
    workpaper_projection = load_research_workpapers(config)
    evidence_vault = verify_evidence_vault(config)
    onboarding = build_business_onboarding_preflight(config)
    doctor = _read_json(project_path("reports/health/latest.json"))
    # An explicitly bound summary (verified by run-manifest-bind) replaces
    # latest-file discovery so every consumer reconciles the SAME run. When
    # the caller names a summary, ANY failure to load it is fatal: falling
    # back to discovery here would silently reconcile a different run.
    explicit_value = str(scheduled_summary_path or "").strip()
    if explicit_value:
        explicit = Path(explicit_value)
        if not explicit.is_file():
            raise ProductionPilotPolicyError("explicit scheduled summary not found: {0}".format(explicit_value))
        scheduled_path = explicit
        scheduled = _read_json(explicit)
        if not scheduled or not isinstance(scheduled, dict):
            raise ProductionPilotPolicyError("explicit scheduled summary is unreadable: {0}".format(explicit_value))
    else:
        scheduled_path = _latest_scheduled_summary()
        scheduled = _read_json(scheduled_path) if scheduled_path else {}
    incidents = _load_incidents(cfg)
    critical_hashes = _critical_input_hashes(config, config_path, provider_registry, operations)
    fingerprint = object_hash({
        "version": PRODUCTION_PILOT_VERSION,
        "critical_inputs": critical_hashes,
        "active_providers": provider_registry.get("active_provider_ids", []),
        "required_approval_gates": cfg["required_approval_gates"],
    })
    return {
        "operations": operations,
        "roster": roster,
        "review_queue": queue,
        "review_capacity": review_capacity,
        "market": market,
        "llm": llm,
        "model": model,
        "workpapers": workpapers,
        "workpaper_projection": workpaper_projection,
        "evidence_vault": evidence_vault,
        "business_onboarding": onboarding,
        "doctor": doctor,
        "scheduled": scheduled,
        "scheduled_path": str(scheduled_path or ""),
        "incidents": incidents,
        "provider_registry": provider_registry,
        "critical_input_hashes": critical_hashes,
        "candidate_fingerprint": fingerprint,
    }


def _build_snapshot(
    config: Dict[str, Any],
    cfg: Dict[str, Any],
    base: Dict[str, Any],
    reconciliation: Dict[str, Any],
) -> Dict[str, Any]:
    fingerprint = base["candidate_fingerprint"]
    control = _load_control_state(cfg, fingerprint)
    run_log = load_hash_chained_jsonl(project_path(cfg["run_ledger_path"]))
    shadow = _shadow_evidence(run_log, cfg, fingerprint)
    gates = _build_gates(config, cfg, base, reconciliation, control, shadow, run_log)
    base_gate_failures = [item for item in gates if item["mandatory"] and item["gate"] not in {"human_approvals", "production_activation"} and item["status"] != "PASS"]
    approval_gate = next(item for item in gates if item["gate"] == "human_approvals")
    activation_gate = next(item for item in gates if item["gate"] == "production_activation")
    technical_ids = {
        "safety_policy", "audit_integrity", "scheduled_operations", "artifact_reconciliation",
        "model_operations", "official_evidence_vault", "research_workpapers",
    }
    technical_failures = [item for item in gates if item["gate"] in technical_ids and item["status"] != "PASS"]
    candidate_ready = not base_gate_failures and approval_gate["status"] == "PASS"
    go_live = candidate_ready and activation_gate["status"] == "PASS"
    blockers = ["{0}: {1}".format(item["gate"], item["detail"]) for item in gates if item["mandatory"] and item["status"] != "PASS"]
    effective_mode = "production_internal_research" if go_live else "shadow"
    snapshot = {
        "version": PRODUCTION_PILOT_VERSION,
        "generated_at": now_iso(),
        "enabled": cfg["enabled"],
        "enforce": cfg["enforce"],
        "configured_mode": cfg["mode"],
        "effective_mode": effective_mode,
        "technical_readiness": "READY" if not technical_failures else "BLOCKED",
        "candidate_readiness": "READY_FOR_HUMAN_ACTIVATION" if candidate_ready else "BLOCKED",
        "candidate_ready_for_activation": candidate_ready,
        "go_live_decision": "GO" if go_live else "NO_GO",
        "candidate_fingerprint": fingerprint,
        "gates": gates,
        "go_live_blockers": blockers,
        "approvals": control.get("approvals", {}),
        "activation": control.get("activation", {}),
        "control_log_integrity": control.get("integrity_status", "UNKNOWN"),
        "provider_registry": base["provider_registry"],
        "roster_readiness": base["roster"],
        "review_capacity": base["review_capacity"],
        "official_evidence_vault": base["evidence_vault"],
        "business_onboarding_preflight": base["business_onboarding"],
        "shadow_evidence": shadow,
        "incidents": base["incidents"],
        "reconciliation": reconciliation,
        "critical_input_hashes": base["critical_input_hashes"],
        "requirements": {
            "minimum_active_providers": cfg["minimum_active_providers"],
            "minimum_shadow_runs": cfg["minimum_shadow_runs"],
            "minimum_shadow_trading_days": cfg["minimum_shadow_trading_days"],
            "maximum_unreconciled_runs": cfg["maximum_unreconciled_runs"],
            "required_approval_gates": cfg["required_approval_gates"],
        },
        "safety_boundary": SAFETY_BOUNDARY,
        "automatic_assignment_executed": False,
        "automatic_approval_executed": False,
        "automatic_activation_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }
    return snapshot


def _build_gates(
    config: Dict[str, Any],
    cfg: Dict[str, Any],
    base: Dict[str, Any],
    reconciliation: Dict[str, Any],
    control: Dict[str, Any],
    shadow: Dict[str, Any],
    run_log: Dict[str, Any],
) -> List[Dict[str, Any]]:
    gates = []

    def add(gate: str, passed: bool, detail: str, *, mandatory: bool = True) -> None:
        gates.append({"gate": gate, "status": "PASS" if passed else "BLOCKED", "mandatory": mandatory, "detail": detail})

    unsafe_flags = [
        key for key in (
            "automatic_assignment", "automatic_approval", "automatic_activation", "automatic_publication", "automatic_trading"
        ) if cfg[key]
    ]
    policy_ok = cfg["enabled"] and cfg["enforce"] and cfg["mode"] == "shadow" and not unsafe_flags
    add("safety_policy", policy_ok, (
        "enabled/enforced shadow mode; all automatic protected actions disabled"
        if policy_ok else "enable enforced shadow mode and disable: {0}".format(", ".join(unsafe_flags) or "unsafe mode/config")
    ))

    integrity_parts = {
        "provider": base["provider_registry"].get("integrity_status"),
        "control": control.get("integrity_status"),
        "incident": base["incidents"].get("integrity_status"),
        "run_ledger": run_log.get("integrity_status"),
        "control_evidence": (base.get("business_onboarding") or {}).get("control_evidence", {}).get("integrity_status"),
    }
    if evidence_vault_config(config)["enabled"]:
        integrity_parts["official_evidence_vault"] = base["evidence_vault"].get("integrity_status")
    integrity_ok = all(value == "OK" for value in integrity_parts.values())
    add("audit_integrity", integrity_ok, "hash-chained logs: {0}".format(
        ", ".join("{0}={1}".format(key, value) for key, value in integrity_parts.items())
    ))

    onboarding = base.get("business_onboarding") or {}
    onboarding_required = bool(onboarding.get("enabled") and onboarding.get("enforced"))
    onboarding_ok = not onboarding_required or (
        onboarding.get("technical_status") == "READY" and onboarding.get("business_status") == "READY"
    )
    add("business_onboarding_preflight", onboarding_ok, "technical={0}; business={1}; blockers={2}".format(
        onboarding.get("technical_status", "MISSING"),
        onboarding.get("business_status", "BLOCKED"),
        len(onboarding.get("business_blockers", []) or []),
    ))

    roster = base["roster"]
    add("human_roster", roster["ready"], "verified roles {0}/{1}; missing: {2}".format(
        roster["ready_role_count"], roster["required_role_count"], ", ".join(roster["missing_roles"]) or "none"
    ))

    providers = base["provider_registry"]
    active_count = len(providers.get("active_provider_ids", []))
    provider_ok = providers.get("integrity_status") == "OK" and active_count >= cfg["minimum_active_providers"]
    add("provider_authorization", provider_ok, "active evidence-approved providers {0}/{1}".format(
        active_count, cfg["minimum_active_providers"]
    ))

    market = base["market"] or {}
    market_ok = market.get("status") == "OK" and not market.get("blocked_count", 0)
    add("market_data_governance", market_ok, "status={0}; pass={1}; blocked={2}; llm_eligible={3}".format(
        market.get("status", "MISSING"), market.get("pass_count", 0), market.get("blocked_count", 0), market.get("llm_eligible_count", 0)
    ))

    llm = base["llm"] or {}
    llm_summary = llm.get("summary", {}) or {}
    llm_ok = llm.get("status") == "OK" and int(llm_summary.get("pending_exception_count", 0) or 0) == 0
    add("external_llm_governance", llm_ok, "status={0}; pending exceptions={1}".format(
        llm.get("status", "MISSING"), llm_summary.get("pending_exception_count", 0)
    ))

    evidence = base["evidence_vault"] or {}
    evidence_enabled = evidence_vault_config(config)["enabled"]
    evidence_technical_ok = not evidence_enabled or (
        evidence.get("technical_status") == "READY" and evidence.get("integrity_status") == "OK"
    )
    add("official_evidence_vault", evidence_technical_ok, "technical={0}; integrity={1}; documents={2}; events={3}".format(
        evidence.get("technical_status", "DISABLED" if not evidence_enabled else "MISSING"),
        evidence.get("integrity_status", "DISABLED" if not evidence_enabled else "MISSING"),
        evidence.get("document_count", 0),
        evidence.get("event_count", 0),
    ))
    critical_workpapers = [
        item for item in ((base.get("workpaper_projection") or {}).get("workpapers") or [])
        if str((item.get("case", {}) or {}).get("priority") or "") in {"P0", "P1"}
    ]
    critical_missing_retained = [
        item for item in critical_workpapers
        if not any(
            source.get("primary_source_eligible") is True and source.get("original_document_retained") is True
            for source in (item.get("source_records") or [])
            if isinstance(source, dict)
        )
    ]
    critical_coverage_ok = not evidence_enabled or not critical_missing_retained
    add(
        "official_evidence_coverage",
        critical_coverage_ok,
        "P0/P1 workpapers with retained official primary evidence {0}/{1}; missing={2}".format(
            len(critical_workpapers) - len(critical_missing_retained),
            len(critical_workpapers),
            len(critical_missing_retained),
        ),
    )

    workpapers = base["workpapers"] or {}
    workpaper_enabled = research_workpaper_config(config)["enabled"]
    workpaper_technical_ok = not workpaper_enabled or (
        workpapers.get("technical_status") == "READY"
        and workpapers.get("integrity_status") == "OK"
    )
    add("research_workpapers", workpaper_technical_ok, "technical={0}; integrity={1}; workpapers={2}".format(
        workpapers.get("technical_status", "DISABLED" if not workpaper_enabled else "MISSING"),
        workpapers.get("integrity_status", "DISABLED" if not workpaper_enabled else "MISSING"),
        workpapers.get("workpaper_count", 0),
    ))
    workpaper_archive_ok = not workpaper_enabled or workpapers.get("production_archive_ready") is True
    add(
        "research_workpaper_archive",
        workpaper_archive_ok,
        "approved production archive evidence: {0}".format(
            "present" if workpapers.get("production_archive_ready") else "missing"
        ),
    )

    capacity = base["review_capacity"]
    review_ok = (
        capacity["eligible_human_reviewer_count"] > 0
        and capacity["critical_unassigned_count"] == 0
        and capacity["critical_overdue_count"] == 0
        and capacity["overdue_count"] == 0
    )
    add("human_review_capacity", review_ok, "reviewers={0}; outstanding={1}; overdue={2}; critical unassigned={3}".format(
        capacity["eligible_human_reviewer_count"], capacity["outstanding_count"], capacity["overdue_count"],
        capacity["critical_unassigned_count"],
    ))

    scheduled = base["scheduled"] or {}
    sequence = _scheduled_sequence_status(scheduled, config)
    scheduled_ok = bool(scheduled) and int(scheduled.get("exit_code", 1) or 0) == 0 and sequence["valid"]
    add("scheduled_operations", scheduled_ok, "latest daily exit={0}; sequence={1}; path={2}".format(
        scheduled.get("exit_code", "missing"), sequence["status"], base.get("scheduled_path", "")
    ))

    add("artifact_reconciliation", bool(reconciliation.get("reconciled")), "missing={0}; hash errors={1}; stale={2}; scheduled report match={3}; handoff match={4}".format(
        len(reconciliation.get("missing_artifacts", [])), len(reconciliation.get("hash_errors", [])),
        len(reconciliation.get("stale_artifacts", [])), str(bool(reconciliation.get("scheduled_report_match"))).lower(),
        str(bool(reconciliation.get("scheduled_handoff_match"))).lower(),
    ))

    model = base["model"] or {}
    model_ok = _model_operationally_acceptable(model)
    add("model_operations", model_ok, "status={0}; run_id={1}; breach_count={2}; rolling_tokens={3}".format(
        model.get("status", "MISSING"), model.get("run_id", ""), model.get("breach_count", 0),
        (model.get("rolling") or {}).get("total_tokens", 0),
    ))

    incident = base["incidents"]
    incident_ok = incident.get("integrity_status") == "OK" and not incident.get("open_critical_count") and not incident.get("open_high_count")
    add("incident_control", incident_ok, "open critical={0}; high={1}; total={2}".format(
        incident.get("open_critical_count", 0), incident.get("open_high_count", 0), incident.get("open_count", 0)
    ))

    shadow_ok = (
        shadow["qualifying_run_count"] >= cfg["minimum_shadow_runs"]
        and shadow["qualifying_trading_day_count"] >= cfg["minimum_shadow_trading_days"]
        and shadow["unreconciled_run_count"] <= cfg["maximum_unreconciled_runs"]
    )
    add("shadow_run_evidence", shadow_ok, "qualifying runs={0}/{1}; days={2}/{3}; unreconciled={4}/{5}".format(
        shadow["qualifying_run_count"], cfg["minimum_shadow_runs"],
        shadow["qualifying_trading_day_count"], cfg["minimum_shadow_trading_days"],
        shadow["unreconciled_run_count"], cfg["maximum_unreconciled_runs"],
    ))

    approvals = control.get("approvals", {}) or {}
    missing_approvals = [
        gate for gate in cfg["required_approval_gates"]
        if (approvals.get(gate) or {}).get("decision") != "approve" or (approvals.get(gate) or {}).get("expired")
    ]
    approval_actors = [str((approvals.get(gate) or {}).get("actor") or "") for gate in cfg["required_approval_gates"]]
    approvals_distinct = all(approval_actors) and len(set(approval_actors)) == len(approval_actors)
    approval_ok = not missing_approvals and approvals_distinct
    add("human_approvals", approval_ok, "approved={0}/{1}; missing/expired={2}; distinct actors={3}".format(
        len(cfg["required_approval_gates"]) - len(missing_approvals), len(cfg["required_approval_gates"]),
        ", ".join(missing_approvals) or "none", str(approvals_distinct).lower(),
    ))

    activation = control.get("activation", {}) or {}
    activation_ok = bool(activation.get("active"))
    add("production_activation", activation_ok, "manual activation bound to current candidate fingerprint: {0}".format(
        "active" if activation_ok else "not active"
    ))
    return gates


def _build_reconciliation(
    config: Dict[str, Any],
    cfg: Dict[str, Any],
    base: Dict[str, Any],
    *,
    config_path: str,
    scheduled_summary_path: str = "",
) -> Dict[str, Any]:
    explicit_summary_path = str(scheduled_summary_path or "").strip()
    if explicit_summary_path:
        explicit_file = Path(explicit_summary_path)
        if not explicit_file.is_file():
            raise ProductionPilotPolicyError("explicit scheduled summary not found: {0}".format(explicit_summary_path))
        explicit_summary = _read_json(explicit_file)
        if not isinstance(explicit_summary, dict) or not explicit_summary:
            raise ProductionPilotPolicyError("explicit scheduled summary is unreadable: {0}".format(explicit_summary_path))
        explicit_report = str(explicit_summary.get("report_path") or "").strip()
        if not explicit_report:
            # No fallback to the newest report: the named summary decides the
            # identity, and a summary without a report path cannot.
            raise ProductionPilotPolicyError("explicit scheduled summary has no report_path: {0}".format(explicit_summary_path))
    else:
        explicit_summary = {}
        explicit_report = ""
    if explicit_report:
        candidate = Path(explicit_report)
        report_path = candidate if candidate.is_absolute() else project_path(explicit_report)
    else:
        report_path = _latest_file(project_path("reports/json"), "*.json")
    run_ops_path = project_path("logs/run_ops/latest.json")
    report_stem = report_path.stem if report_path else ""
    scheduled = base.get("scheduled") or {}
    uses_pi = bool(
        scheduled.get("agent_runtime") == "pi"
        or "agent_handoff_public_signal_count" in scheduled
    )
    scheduled_commands = scheduled.get("commands", []) if isinstance(scheduled.get("commands"), list) else []
    scheduled_command_names = [
        str(item.get("name") or "") for item in scheduled_commands if isinstance(item, dict)
    ]
    pi_handoff_executed = uses_pi and "pi-handoff-run" in scheduled_command_names
    expected = {
        "config": project_path(config_path) if config_path else None,
        "json_report": report_path,
        "daily_report": project_path("reports/daily/{0}.md".format(report_stem)) if report_stem else None,
        "alert_report": project_path("reports/alerts/{0}.jsonl".format(report_stem)) if report_stem else None,
        "run_ops": run_ops_path,
        "market_governance": project_path("reports/market_data_governance/latest.json"),
        "llm_governance": project_path("reports/llm_governance/latest.json"),
        "model_operations": project_path("reports/model_operations/latest.json"),
        ("agent_handoff" if uses_pi else "openclaw_handoff"): project_path(
            "reports/agent_handoff/latest.json" if uses_pi else "reports/handoff/latest.json"
        ),
        "review_queue": project_path("reports/review_queue/latest.json"),
        "research_runtime": project_path("reports/research_runtime/latest.json"),
        "research_operations": project_path("reports/research_operations/latest.json"),
        "workspace": project_path("reports/workspace/latest.json"),
        "dashboard": project_path("reports/dashboard/latest.html"),
        "notifications": project_path("reports/notifications/latest.md"),
    }
    if research_workpaper_config(config)["enabled"]:
        expected["research_workpapers"] = project_path("reports/research_workpapers/latest.json")
    if evidence_vault_config(config)["enabled"]:
        expected["official_evidence_vault"] = project_path("reports/evidence_vault/latest.json")
    if pi_handoff_executed:
        expected["pi_result"] = project_path("reports/pi_agent/public_handoff/latest.json")
    hashes = {}
    missing = []
    hash_errors = []
    stale_artifacts = []
    for name, path in expected.items():
        if path is None or not path.exists():
            missing.append(name)
            continue
        try:
            hashes[name] = {"path": str(path), "sha256": file_hash(path), "size": path.stat().st_size}
        except OSError as exc:
            hash_errors.append({"artifact": name, "error_type": type(exc).__name__})
    report = _read_json(report_path) if report_path else {}
    handoff_name = "agent_handoff" if uses_pi else "openclaw_handoff"
    handoff = _read_json(expected[handoff_name])
    if handoff:
        validator = validate_agent_handoff if uses_pi else validate_openclaw_handoff
        for error in validator(handoff):
            hash_errors.append({"artifact": handoff_name, "error_type": "validation", "detail": error})
        if report_path and handoff.get("source_report_sha256") != file_hash(report_path):
            hash_errors.append({
                "artifact": handoff_name,
                "error_type": "source_report_hash_mismatch",
            })
    pi_result = _read_json(expected.get("pi_result")) if pi_handoff_executed else {}
    if pi_handoff_executed and pi_result:
        if pi_result.get("handoff_payload_sha256") != handoff.get("payload_sha256"):
            hash_errors.append({"artifact": "pi_result", "error_type": "handoff_hash_mismatch"})
        if bool(pi_result.get("called")) != bool(handoff.get("external_llm_call_required")):
            hash_errors.append({"artifact": "pi_result", "error_type": "conditional_call_mismatch"})
    run_ops = _read_json(run_ops_path)
    report_mtime = report_path.stat().st_mtime if report_path and report_path.exists() else 0.0
    for name, item in hashes.items():
        if name in {"config", "json_report"}:
            continue
        artifact_path = Path(str(item.get("path") or ""))
        try:
            if report_mtime and artifact_path.stat().st_mtime + 10.0 < report_mtime:
                stale_artifacts.append(name)
        except OSError:
            stale_artifacts.append(name)
    scheduled_report_value = str((base.get("scheduled") or {}).get("report_path") or "")
    scheduled_report_match = bool(
        report_path
        and scheduled_report_value
        and Path(scheduled_report_value).stem == report_path.stem
    )
    handoff_count = int(handoff.get("public_signal_count", 0) or 0)
    handoff_call_required = bool(handoff.get("external_llm_call_required"))
    if uses_pi:
        if pi_handoff_executed:
            scheduled_handoff_match = bool(
                handoff
                and pi_result
                and "agent_handoff_public_signal_count" in scheduled
                and int(scheduled.get("agent_handoff_public_signal_count", -1) or 0) == handoff_count
                and bool(scheduled.get("agent_handoff_call_required")) == handoff_call_required
                and bool(scheduled.get("used_pi")) == handoff_call_required
                and int(scheduled.get("pi_model_call_count", -1) or 0) == (1 if handoff_call_required else 0)
            )
        else:
            scheduled_handoff_match = bool(
                handoff
                and "agent-handoff" in scheduled_command_names
                and "agent_handoff_public_signal_count" in scheduled
                and int(scheduled.get("agent_handoff_public_signal_count", -1) or 0) == handoff_count
                and bool(scheduled.get("agent_handoff_call_required")) == handoff_call_required
                and not bool(scheduled.get("used_pi"))
                and int(scheduled.get("pi_model_call_count", 0) or 0) == 0
            )
    else:
        scheduled_handoff_match = bool(
            handoff
            and "openclaw_handoff_public_signal_count" in scheduled
            and int(scheduled.get("openclaw_handoff_public_signal_count", -1) or 0) == handoff_count
            and bool(scheduled.get("openclaw_handoff_call_required")) == handoff_call_required
            and bool(scheduled.get("used_openclaw")) == handoff_call_required
        )
    run_identity = object_hash({
        "report_path": str(report_path or ""),
        "run_id": run_ops.get("run_id", ""),
        "scheduled_path": base.get("scheduled_path", ""),
        "candidate_fingerprint": base["candidate_fingerprint"],
        "agent_runtime": "pi" if uses_pi else "openclaw_legacy",
    }) if report_path else ""
    preliminary = {
        "schema_version": PRODUCTION_PILOT_VERSION,
        "event_type": "shadow_run_reconciled",
        "recorded_at": now_iso(),
        "run_identity": run_identity,
        "candidate_fingerprint": base["candidate_fingerprint"],
        "report_generated_at": str(report.get("generated_at") or ""),
        "run_id": str(run_ops.get("run_id") or report_stem),
        "scheduled_summary_path": base.get("scheduled_path", ""),
        "artifact_hashes": hashes,
        "missing_artifacts": missing,
        "hash_errors": hash_errors,
        "stale_artifacts": sorted(set(stale_artifacts)),
        "scheduled_report_path": scheduled_report_value,
        "scheduled_report_match": scheduled_report_match,
        "scheduled_handoff_match": scheduled_handoff_match,
        "handoff_public_signal_count": handoff_count,
        "handoff_external_call_required": handoff_call_required,
        "reconciled": (
            not missing and not hash_errors and not stale_artifacts
            and scheduled_report_match and scheduled_handoff_match
        ),
    }
    preliminary["qualifying_shadow_run"] = _qualifying_shadow_run(base, preliminary, cfg, config)
    preliminary["qualification_reasons"] = _shadow_qualification_reasons(base, preliminary, cfg, config)
    return preliminary


def _qualifying_shadow_run(
    base: Dict[str, Any],
    reconciliation: Dict[str, Any],
    cfg: Dict[str, Any],
    config: Dict[str, Any],
) -> bool:
    return not _shadow_qualification_reasons(base, reconciliation, cfg, config)


def _shadow_qualification_reasons(
    base: Dict[str, Any],
    reconciliation: Dict[str, Any],
    cfg: Dict[str, Any],
    config: Dict[str, Any],
) -> List[str]:
    reasons = []
    if cfg["mode"] != "shadow":
        reasons.append("configured mode is not shadow")
    if str((base.get("scheduled") or {}).get("trigger_source") or "manual") != "scheduled":
        reasons.append("daily run was not launched by the configured scheduled-task path")
    if not reconciliation.get("reconciled"):
        reasons.append("artifacts are not reconciled")
    if not reconciliation.get("scheduled_report_match"):
        reasons.append("scheduled summary does not match the current report")
    if not base["roster"].get("ready"):
        reasons.append("human roster is not ready")
    onboarding = base.get("business_onboarding") or {}
    if onboarding.get("enabled") and onboarding.get("enforced") and onboarding.get("business_status") != "READY":
        reasons.append("business onboarding preflight is not ready")
    if len(base["provider_registry"].get("active_provider_ids", [])) < cfg["minimum_active_providers"]:
        reasons.append("insufficient active providers")
    if (base["market"] or {}).get("status") != "OK":
        reasons.append("market data governance is not OK")
    llm = base["llm"] or {}
    if llm.get("status") != "OK" or int((llm.get("summary") or {}).get("pending_exception_count", 0) or 0):
        reasons.append("LLM governance is not cleared")
    if not _model_operationally_acceptable(base["model"] or {}):
        reasons.append("model operations is not OK")
    capacity = base["review_capacity"]
    if capacity["overdue_count"] or capacity["critical_unassigned_count"]:
        reasons.append("review backlog is not within operating limits")
    scheduled = base["scheduled"] or {}
    sequence = _scheduled_sequence_status(scheduled, config)
    if int(scheduled.get("exit_code", 1) or 0) != 0 or not sequence["valid"]:
        reasons.append("latest scheduled daily run is not successful")
    if sequence.get("handoff_mode") == "offline_agent_handoff" and bool(
        scheduled.get("agent_handoff_call_required")
    ):
        reasons.append("external Pi call was required but explicitly skipped")
    incidents = base["incidents"]
    if incidents.get("open_critical_count") or incidents.get("open_high_count"):
        reasons.append("high-severity incidents remain open")
    return reasons


def _append_reconciliation_if_new(cfg: Dict[str, Any], reconciliation: Dict[str, Any]) -> None:
    path = project_path(cfg["run_ledger_path"])
    log = load_hash_chained_jsonl(path)
    if log["integrity_status"] != "OK":
        raise AuditLogIntegrityError("production pilot run ledger integrity check failed")
    identity = _reconciliation_key(reconciliation)
    reconciliation["run_identity"] = identity
    matching = [item for item in log["records"] if _reconciliation_key(item) == identity]
    latest = matching[-1] if matching else {}
    state_hash = _reconciliation_state_hash(reconciliation)
    if latest and _reconciliation_state_hash(latest) == state_hash:
        return
    reconciliation["reconciliation_state_sha256"] = state_hash
    reconciliation["reconciliation_revision"] = len(matching) + 1
    reconciliation["supersedes_event_id"] = str(latest.get("event_id") or "")
    reconciliation["supersedes_event_hash"] = str(latest.get("event_hash") or "")
    append_hash_chained_jsonl(path, reconciliation)


def _shadow_evidence(log: Dict[str, Any], cfg: Dict[str, Any], fingerprint: str) -> Dict[str, Any]:
    raw_records = [
        item for item in log.get("records", [])
        if item.get("event_type") == "shadow_run_reconciled"
        and str(item.get("candidate_fingerprint") or "") == fingerprint
    ]
    projected = {}
    for item in raw_records:
        projected[_reconciliation_key(item)] = item
    records = sorted(projected.values(), key=lambda item: int(item.get("sequence", 0) or 0))
    qualifying = [item for item in records if item.get("qualifying_shadow_run")]
    trading_days = sorted({str(item.get("report_generated_at") or item.get("recorded_at") or "")[:10] for item in qualifying if str(item.get("report_generated_at") or item.get("recorded_at") or "")[:10]})
    unreconciled = [item for item in records if not item.get("reconciled")]
    return {
        "integrity_status": log.get("integrity_status", "UNKNOWN"),
        "recorded_run_count": len(records),
        "raw_reconciliation_event_count": len(raw_records),
        "superseded_reconciliation_event_count": len(raw_records) - len(records),
        "historical_unreconciled_event_count": len([item for item in raw_records if not item.get("reconciled")]),
        "qualifying_run_count": len(qualifying),
        "qualifying_trading_day_count": len(trading_days),
        "qualifying_trading_days": trading_days,
        "unreconciled_run_count": len(unreconciled),
        "minimum_runs": cfg["minimum_shadow_runs"],
        "minimum_trading_days": cfg["minimum_shadow_trading_days"],
        "maximum_unreconciled_runs": cfg["maximum_unreconciled_runs"],
        "latest_run": records[-1] if records else {},
    }


def _reconciliation_key(value: Dict[str, Any]) -> str:
    artifact_hashes = value.get("artifact_hashes") or {}
    report_item = artifact_hashes.get("json_report") or {}
    report_path = str(value.get("scheduled_report_path") or report_item.get("path") or "")
    return object_hash({
        "report_path": report_path,
        "run_id": str(value.get("run_id") or ""),
        "scheduled_path": str(value.get("scheduled_summary_path") or ""),
        "candidate_fingerprint": str(value.get("candidate_fingerprint") or ""),
    })


def _reconciliation_state_hash(value: Dict[str, Any]) -> str:
    return object_hash({
        "reconciled": bool(value.get("reconciled")),
        "missing_artifacts": value.get("missing_artifacts", []) or [],
        "hash_errors": value.get("hash_errors", []) or [],
        "stale_artifacts": value.get("stale_artifacts", []) or [],
        "scheduled_report_match": bool(value.get("scheduled_report_match")),
        "scheduled_handoff_match": bool(value.get("scheduled_handoff_match")),
        "qualifying_shadow_run": bool(value.get("qualifying_shadow_run")),
        "qualification_reasons": value.get("qualification_reasons", []) or [],
    })


def _roster_readiness(operations: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    required_roles = set(cfg["required_approval_gates"].values()) | {"research_coordinator"}
    eligible_by_role = {}
    for role in sorted(required_roles):
        eligible_by_role[role] = sorted(
            principal["actor_id"] for principal in operations.get("principals", [])
            if _principal_eligible_for_role(principal, role)
        )
    missing = [role for role, actors in eligible_by_role.items() if not actors]
    separation_possible = _distinct_assignment_possible(eligible_by_role)
    if not separation_possible and not missing:
        missing.append("distinct_actor_separation")
    return {
        "ready": bool(operations.get("roster_valid")) and not missing,
        "roster_valid": bool(operations.get("roster_valid")),
        "roster_errors": operations.get("errors", []),
        "required_roles": sorted(required_roles),
        "required_role_count": len(required_roles),
        "ready_role_count": sum(bool(value) for value in eligible_by_role.values()),
        "missing_roles": missing,
        "eligible_humans_by_role": eligible_by_role,
        "distinct_actor_separation_possible": separation_possible,
        "human_count": sum(item.get("principal_type") == "human" for item in operations.get("principals", [])),
        "service_count": sum(item.get("principal_type") == "service" for item in operations.get("principals", [])),
    }


def _distinct_assignment_possible(eligible_by_role: Dict[str, List[str]]) -> bool:
    roles = sorted(eligible_by_role, key=lambda role: (len(eligible_by_role[role]), role))

    def assign(index: int, used: set) -> bool:
        if index >= len(roles):
            return True
        for actor in eligible_by_role[roles[index]]:
            if actor in used:
                continue
            used.add(actor)
            if assign(index + 1, used):
                return True
            used.remove(actor)
        return False

    return assign(0, set())


def _review_capacity(queue: Dict[str, Any], operations: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    items = [
        item for item in queue.get("items", [])
        if str(item.get("status") or "pending") in OPEN_STATUSES
    ]
    items.sort(key=lambda item: (
        {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(str(item.get("priority") or "P3"), 4),
        -int(item.get("escalation_level", 0) or 0),
        str(item.get("due_at") or ""),
        str(item.get("signal_id") or ""),
    ))
    reviewers = [
        principal for principal in operations.get("principals", [])
        if _principal_eligible_for_role(principal, "lead_analyst")
    ]
    wip = len(reviewers) * cfg["review_wip_limit_per_human"]
    batches = []
    for index in range(0, len(items), cfg["review_batch_size"]):
        batch_items = items[index:index + cfg["review_batch_size"]]
        batches.append({
            "batch_id": "review_batch_{0:03d}".format(len(batches) + 1),
            "signal_ids": [str(item.get("signal_id") or "") for item in batch_items],
            "priorities": dict(Counter(str(item.get("priority") or "P3") for item in batch_items)),
            "assignment_status": "human_assignment_required",
        })
    critical = [item for item in items if str(item.get("priority") or "") in {"P0", "P1"}]
    return {
        "outstanding_count": len(items),
        "overdue_count": sum(str(item.get("sla_status") or "") == "overdue" for item in items),
        "unassigned_count": sum(not str(item.get("assigned_to") or "") for item in items),
        "critical_count": len(critical),
        "critical_overdue_count": sum(str(item.get("sla_status") or "") == "overdue" for item in critical),
        "critical_unassigned_count": sum(not str(item.get("assigned_to") or "") for item in critical),
        "eligible_human_reviewer_count": len(reviewers),
        "eligible_human_reviewers": [item["actor_id"] for item in reviewers],
        "configured_wip_limit_per_human": cfg["review_wip_limit_per_human"],
        "available_wip_capacity": wip,
        "review_batch_size": cfg["review_batch_size"],
        "suggested_batch_count": int(math.ceil(len(items) / float(cfg["review_batch_size"]))) if items else 0,
        "suggested_batches": batches,
        "automatic_assignment_executed": False,
    }


def _load_control_state(cfg: Dict[str, Any], fingerprint: str) -> Dict[str, Any]:
    log = load_hash_chained_jsonl(project_path(cfg["control_event_path"]))
    approvals = {}
    activation = {}
    now = _parse_datetime(now_iso()) or datetime.now(timezone.utc)
    validity_seconds = cfg["approval_validity_days"] * 86400
    for event in log["records"]:
        if str(event.get("candidate_fingerprint") or "") != fingerprint:
            continue
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "pilot_approval_recorded":
            recorded = _parse_datetime(str(event.get("recorded_at") or ""))
            expired = bool(recorded and (now - recorded).total_seconds() > validity_seconds)
            approvals[str(event.get("gate") or "")] = {
                "decision": str(payload.get("decision") or ""),
                "actor": str(event.get("actor") or ""),
                "actor_role": str(event.get("actor_role") or ""),
                "recorded_at": str(event.get("recorded_at") or ""),
                "notes": str(payload.get("notes") or ""),
                "evidence_refs": list(payload.get("evidence_refs") or []),
                "expired": expired,
            }
        elif event_type == "pilot_activation_recorded":
            activation = {
                "active": True,
                "actor": str(event.get("actor") or ""),
                "actor_role": str(event.get("actor_role") or ""),
                "recorded_at": str(event.get("recorded_at") or ""),
                "notes": str(payload.get("notes") or ""),
                "evidence_refs": list(payload.get("evidence_refs") or []),
            }
        elif event_type == "pilot_activation_revoked":
            activation = {
                "active": False,
                "revoked_by": str(event.get("actor") or ""),
                "revoked_at": str(event.get("recorded_at") or ""),
                "notes": str(payload.get("notes") or ""),
            }
    return {
        "integrity_status": log["integrity_status"],
        "invalid_record_count": log["invalid_record_count"],
        "approvals": approvals if log["integrity_status"] == "OK" else {},
        "activation": activation if log["integrity_status"] == "OK" else {},
    }


def _load_incidents(cfg: Dict[str, Any]) -> Dict[str, Any]:
    log = load_hash_chained_jsonl(project_path(cfg["incident_event_path"]))
    incidents: Dict[str, Dict[str, Any]] = {}
    for event in log["records"]:
        incident_id = str(event.get("incident_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not incident_id:
            continue
        if event.get("event_type") == "incident_reported":
            incidents[incident_id] = {
                "incident_id": incident_id,
                "title": str(payload.get("title") or ""),
                "severity": str(payload.get("severity") or "low"),
                "category": str(payload.get("category") or ""),
                "status": "open",
                "reported_at": str(event.get("recorded_at") or ""),
                "reported_by": str(event.get("actor") or ""),
                "actions": [],
            }
            continue
        incident = incidents.get(incident_id)
        if not incident:
            continue
        action = str(payload.get("action") or "")
        action_row = {
            "action": action,
            "actor": str(event.get("actor") or ""),
            "actor_role": str(event.get("actor_role") or ""),
            "recorded_at": str(event.get("recorded_at") or ""),
            "notes": str(payload.get("notes") or ""),
            "evidence_refs": list(payload.get("evidence_refs") or []),
        }
        incident["actions"].append(action_row)
        if action == "acknowledge":
            incident["status"] = "acknowledged"
        elif action == "mitigate":
            incident["status"] = "mitigating"
        elif action == "reject_resolution":
            incident["status"] = "open"
        elif action == "resolve":
            if incident["severity"] in {"critical", "high"}:
                resolving_roles = {
                    row["actor_role"] for row in incident["actions"] if row["action"] == "resolve"
                }
                if CRITICAL_REVIEW_ROLES.issubset(resolving_roles):
                    incident["status"] = "resolved"
                else:
                    incident["status"] = "resolution_pending_dual_review"
            else:
                incident["status"] = "resolved"
    open_items = [item for item in incidents.values() if item.get("status") != "resolved"]
    counts = Counter(item.get("status", "unknown") for item in incidents.values())
    return {
        "integrity_status": log["integrity_status"],
        "invalid_record_count": log["invalid_record_count"],
        "incident_count": len(incidents),
        "open_count": len(open_items),
        "open_critical_count": sum(item.get("severity") == "critical" for item in open_items),
        "open_high_count": sum(item.get("severity") == "high" for item in open_items),
        "status_counts": dict(sorted(counts.items())),
        "incidents": incidents,
    }


def _append_control_event(cfg: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(event)
    if value.get("event_type") not in CONTROL_EVENT_TYPES:
        raise ProductionPilotPolicyError("unsupported pilot control event")
    value["schema_version"] = PRODUCTION_PILOT_VERSION
    value["recorded_at"] = now_iso()
    return append_hash_chained_jsonl(project_path(cfg["control_event_path"]), value)


def _append_incident_event(cfg: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(event)
    if value.get("event_type") not in INCIDENT_EVENT_TYPES:
        raise ProductionPilotPolicyError("unsupported incident event")
    value["schema_version"] = PRODUCTION_PILOT_VERSION
    value["recorded_at"] = now_iso()
    return append_hash_chained_jsonl(project_path(cfg["incident_event_path"]), value)


def _authorize_human(config: Dict[str, Any], actor: str, actor_role: str) -> Dict[str, Any]:
    inputs = load_operations_inputs(config)
    if inputs.get("errors"):
        raise ProductionPilotPolicyError("research roster is invalid: {0}".format("; ".join(inputs["errors"][:5])))
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    principal = (inputs.get("principal_map") or {}).get(actor)
    if not principal:
        raise ProductionPilotPolicyError("actor is not registered in the research operations roster")
    if principal.get("principal_type") != "human" or not principal.get("active"):
        raise ProductionPilotPolicyError("actor must be an active human principal")
    if actor_role not in (principal.get("roles") or []):
        raise ProductionPilotPolicyError("actor is not registered for role {0}".format(actor_role))
    if not _principal_eligible_for_role(principal, actor_role):
        raise ProductionPilotPolicyError("actor is not qualified or authorized for role {0}".format(actor_role))
    return principal


def _require_onboarding_ready_approver(config: Dict[str, Any], actor: str, actor_role: str) -> None:
    onboarding = (config.get("business_onboarding", {}) or {}) if isinstance(config, dict) else {}
    if not (onboarding.get("enabled") and onboarding.get("enforce")):
        return
    preflight = build_business_onboarding_preflight(config)
    if preflight.get("technical_status") != "READY":
        raise ProductionPilotPolicyError("business onboarding technical preflight is not ready")
    eligible = (
        ((preflight.get("roster") or {}).get("eligible_humans_by_role") or {}).get(actor_role, [])
    )
    if str(actor or "").strip() not in eligible:
        raise ProductionPilotPolicyError(
            "actor is not onboarding-ready for protected role {0}".format(actor_role)
        )


def _principal_eligible_for_role(principal: Dict[str, Any], role: str) -> bool:
    if principal.get("principal_type") != "human" or not principal.get("active") or role not in (principal.get("roles") or []):
        return False
    if role in {"lead_analyst", "quality_reviewer"}:
        registration = ((principal.get("qualifications") or {}).get("securities_analyst_registration") or {})
        if registration.get("status") != "verified" or not str(registration.get("registration_id") or "").strip():
            return False
    required = ROLE_AUTHORIZATIONS.get(role)
    if required and required not in (principal.get("authorizations") or []):
        return False
    if role in {"lead_analyst", "quality_reviewer", "research_coordinator"} and principal.get("information_side") != "public":
        return False
    return True


def _critical_input_hashes(
    config: Dict[str, Any],
    config_path: str,
    provider_registry: Dict[str, Any],
    operations: Dict[str, Any],
) -> Dict[str, Any]:
    critical_config = {
        key: config.get(key, {})
        for key in (
            "pi_agent", "agent_handoff", "llm_agent", "multi_role_agent", "llm_governance", "model_operations",
            "market_data_governance", "research_runtime", "official_evidence_vault", "research_workpapers",
            "research_operations", "business_onboarding", "production_pilot",
        )
    }
    hashes: Dict[str, Any] = {"critical_config": object_hash(critical_config)}
    for name, raw_path in (
        ("config_file", config_path),
        ("roster", operations.get("roster_path", "")),
        ("restrictions", operations.get("restrictions_path", "")),
        (
            "control_evidence_ledger",
            (config.get("business_onboarding", {}) or {}).get(
                "control_evidence_event_path", "data/business_onboarding/control_evidence_events.jsonl"
            ),
        ),
    ):
        path = project_path(str(raw_path)) if raw_path else None
        hashes[name] = file_hash(path) if path and path.exists() else "missing"
    pi_cfg = config.get("pi_agent", {}) or {}
    for name, key, default_path in (
        ("pi_skill_registry", "skill_registry", "config/pi_skills.json"),
        ("pi_skill_lock", "skill_lock", "config/pi_skills.lock.json"),
    ):
        if bool(pi_cfg.get("compiled_skills_enabled", False)):
            path = project_path(str(pi_cfg.get(key) or default_path))
            hashes[name] = file_hash(path) if path.exists() else "missing"
    provider_projection = {
        provider_id: {
            "status": item.get("status"),
            "scopes": item.get("service_scopes", []),
            "activated_at": item.get("activated_at", ""),
            "activated_by": item.get("activated_by", ""),
            "reviews": item.get("reviews", {}),
        }
        for provider_id, item in sorted((provider_registry.get("providers") or {}).items())
    }
    hashes["provider_projection"] = object_hash(provider_projection)
    vault_cfg = evidence_vault_config(config)
    if vault_cfg["enabled"]:
        vault = verify_evidence_vault(config)
        hashes["official_evidence_vault"] = object_hash({
            "event_count": vault.get("event_count", 0),
            "integrity_status": vault.get("integrity_status", "UNKNOWN"),
            "document_count": vault.get("document_count", 0),
            "production_archive_ready": vault.get("production_archive_ready", False),
            "archive_policy_fingerprint": (vault.get("archive_control") or {}).get("policy_fingerprint", ""),
        })
    return hashes


def _scheduled_sequence_status(
    summary: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    commands = summary.get("commands", []) if isinstance(summary.get("commands"), list) else []
    names = [str(item.get("name") or "") for item in commands if isinstance(item, dict)]
    uses_pi = bool(summary.get("agent_runtime") == "pi" or "agent_handoff_public_signal_count" in summary)
    onboarding = (config or {}).get("business_onboarding", {}) if isinstance(config, dict) else {}
    onboarding_required = bool(onboarding.get("enabled") and onboarding.get("enforce"))
    if uses_pi:
        offline_handoff = names.count("agent-handoff") == 1 and names.count("pi-handoff-run") == 0
        handoff_step = "agent-handoff" if offline_handoff else "pi-handoff-run"
        required = [
            "market-cache-seed", "agent-run", "evidence-vault-status", handoff_step, "review-queue-sync", "research-ops-plan",
            "workspace-sync", "production-pilot-sync", "dashboard", "alerts",
        ]
    else:
        required = [
            "market-cache-seed", "agent-run", "evidence-vault-status", "openclaw-handoff", "review-queue-sync", "research-ops-plan",
            "workspace-sync", "production-pilot-sync", "dashboard", "alerts",
        ]
    if onboarding_required:
        required.insert(required.index("dashboard"), "business-onboarding-sync")
    positions = []
    missing = []
    for name in required:
        try:
            positions.append(names.index(name))
        except ValueError:
            missing.append(name)
    agent_count = names.count("agent-run")
    if uses_pi:
        used_runtime = bool(summary.get("used_pi"))
        call_required = bool(summary.get("agent_handoff_call_required"))
        public_handoff_count = names.count(handoff_step)
        if offline_handoff:
            conditional_handoff_valid = (
                not used_runtime
                and public_handoff_count == 1
                and names.count("pi-handoff-run") == 0
                and int(summary.get("pi_model_call_count", 0) or 0) == 0
            )
        else:
            conditional_handoff_valid = (
                used_runtime == call_required
                and public_handoff_count == 1
                and names.count("agent-handoff") == 0
                and int(summary.get("pi_model_call_count", -1) or 0) == (1 if call_required else 0)
                and not (int(summary.get("agent_handoff_public_signal_count", 0) or 0) == 0 and used_runtime)
            )
    else:
        used_runtime = bool(summary.get("used_openclaw"))
        call_required = bool(summary.get("openclaw_handoff_call_required"))
        public_handoff_count = names.count("openclaw-public-handoff")
        conditional_handoff_valid = (
            used_runtime == call_required
            and public_handoff_count == (1 if call_required else 0)
            and not (int(summary.get("openclaw_handoff_public_signal_count", 0) or 0) == 0 and used_runtime)
        )
    valid = (
        not missing and positions == sorted(positions) and agent_count == 1
        and conditional_handoff_valid
    )
    return {
        "valid": valid,
        "status": "PASS" if valid else "FAIL",
        "missing": missing,
        "agent_run_count": agent_count,
        "public_handoff_count": public_handoff_count,
        "conditional_handoff_valid": conditional_handoff_valid,
        "agent_runtime": "pi" if uses_pi else "openclaw_legacy",
        "business_onboarding_required": onboarding_required,
        "handoff_mode": ("offline_agent_handoff" if offline_handoff else "pi_handoff") if uses_pi else "openclaw_legacy",
        "command_names": names,
    }


def _model_operationally_acceptable(model: Dict[str, Any]) -> bool:
    status = str(model.get("status") or "").upper()
    if status not in {"OK", "PASS", "IDLE", "ATTENTION"}:
        return False
    if int(model.get("breach_count", 0) or 0) > 0:
        return False
    checks = model.get("checks", []) if isinstance(model.get("checks"), list) else []
    if any(str(item.get("status") or "").upper() in {"FAIL", "BREACH"} for item in checks if isinstance(item, dict)):
        return False
    rolling_tokens = int((model.get("rolling") or {}).get("total_tokens", 0) or 0)
    current_calls = len(model.get("calls", []) or [])
    return rolling_tokens > 0 or current_calls > 0


def _latest_scheduled_summary() -> Optional[Path]:
    directory = project_path("logs/scheduled")
    if not directory.exists():
        return None
    candidates = []
    for path in directory.glob("run_*.summary.json"):
        value = _read_json(path)
        if str(value.get("mode") or "") == "daily":
            candidates.append(path)
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _latest_file(directory: Path, pattern: str) -> Optional[Path]:
    if not directory.exists():
        return None
    files = list(directory.glob(pattern))
    return max(files, key=lambda item: item.stat().st_mtime) if files else None


def _read_json(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_datetime(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_references(
    values: Sequence[str],
    *,
    config: Optional[Dict[str, Any]] = None,
    subject_id: str = "",
    categories: Optional[Sequence[str]] = None,
) -> List[str]:
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        if re.search(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=])", text, re.IGNORECASE):
            raise ProductionPilotPolicyError("evidence references must not contain API keys or secrets")
        if TRADING_PATTERN.search(text):
            raise ProductionPilotPolicyError("pilot evidence must not contain trading or order instructions")
        onboarding = (config or {}).get("business_onboarding", {}) if isinstance(config, dict) else {}
        if onboarding.get("enabled") and onboarding.get("enforce"):
            if text.startswith("control:"):
                ok, reason, _ = resolve_verified_control_evidence(
                    config or {}, text, categories=categories, subject_id=subject_id
                )
                if not ok:
                    raise ProductionPilotPolicyError(
                        "pilot control evidence reference is not ready: {0}".format(reason)
                    )
            else:
                try:
                    validate_managed_archive_reference(config or {}, text)
                except ValueError as exc:
                    raise ProductionPilotPolicyError(str(exc)) from exc
        result.append(text)
        seen.add(text)
    return result


def _compact_stamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "", value.replace("+", "_"))[:32] or "latest"
