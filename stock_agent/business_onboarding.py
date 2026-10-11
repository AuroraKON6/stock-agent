from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .audit_log import atomic_write_text, object_hash
from .config import project_path
from .control_evidence import (
    load_control_evidence_registry,
    resolve_verified_control_evidence,
    write_control_evidence_snapshot,
)
from .evidence_vault import verify_evidence_vault
from .llm_governance import load_latest_governance_summary
from .market_data_governance import load_latest_market_data_governance
from .models import now_iso
from .provider_registry import build_provider_registry, provider_registry_config
from .research_operations import load_operations_inputs
from .research_workpapers import load_research_workpapers, verify_research_workpapers


BUSINESS_ONBOARDING_VERSION = "29.0"
SAFETY_BOUNDARY = (
    "Internal research onboarding only. No automatic roster activation, vendor approval, protected-gate approval, "
    "external publication, trading, order placement, or position change."
)
PHASE_ORDER = [
    "governance_scope",
    "human_roster",
    "provider_authorization",
    "official_evidence_backfill",
    "governance_remediation",
    "production_archive",
    "shadow_readiness",
    "go_no_go",
]
PHASE_OWNERS = {
    "governance_scope": "compliance_officer",
    "human_roster": "research_coordinator",
    "provider_authorization": "data_steward",
    "official_evidence_backfill": "information_analyst",
    "governance_remediation": "compliance_officer",
    "production_archive": "information_security_officer",
    "shadow_readiness": "research_coordinator",
    "go_no_go": "publication_approver",
}
DEFAULT_GOVERNANCE_EVIDENCE = [
    "governance_mandate",
    "internal_research_scope",
    "prohibited_actions_policy",
]
DEFAULT_HUMAN_EVIDENCE = [
    "human_employment",
    "human_confidentiality",
    "human_training",
    "role_appointment",
]
DEFAULT_ARCHIVE_EVIDENCE = [
    "production_archive_contract",
    "production_archive_security",
    "production_archive_retention",
]
ROLE_SPECIFIC_HUMAN_EVIDENCE = {
    "lead_analyst": ["human_qualification"],
    "quality_reviewer": ["human_qualification"],
}
ROLE_AUTHORIZATIONS = {
    "compliance_officer": "compliance_authorized",
    "information_security_officer": "information_security_authorized",
    "data_steward": "vendor_data_steward_authorized",
    "publication_approver": "publication_authorized",
    "research_coordinator": "research_coordination_authorized",
}


def business_onboarding_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("business_onboarding", {}) if isinstance(config, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce": bool(raw.get("enforce", False)),
        "organization_id": str(raw.get("organization_id") or "").strip(),
        "report_dir": str(raw.get("report_dir") or "reports/business_onboarding"),
        "intake_pack_dir": str(raw.get("intake_pack_dir") or "reports/business_onboarding/intake_pack"),
        "required_governance_evidence": _clean_list(
            raw.get("required_governance_evidence", DEFAULT_GOVERNANCE_EVIDENCE)
        ) or list(DEFAULT_GOVERNANCE_EVIDENCE),
        "required_human_evidence": _clean_list(raw.get("required_human_evidence", DEFAULT_HUMAN_EVIDENCE))
        or list(DEFAULT_HUMAN_EVIDENCE),
        "required_archive_evidence": _clean_list(raw.get("required_archive_evidence", DEFAULT_ARCHIVE_EVIDENCE))
        or list(DEFAULT_ARCHIVE_EVIDENCE),
        "require_verified_control_evidence": bool(raw.get("require_verified_control_evidence", True)),
        "require_distinct_humans": bool(raw.get("require_distinct_humans", True)),
    }


def build_business_onboarding_preflight(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = business_onboarding_config(config)
    control = load_control_evidence_registry(config)
    operations = load_operations_inputs(config)
    provider = build_provider_registry(config)
    governance = _governance_scope_stage(config, cfg, control)
    roster = _human_roster_stage(config, cfg, operations)
    providers = _provider_stage(config, provider)
    stages = [governance, roster, providers]
    archive_control = _archive_control_evidence_status(config, cfg, control)
    technical_errors = []
    if control.get("integrity_status") != "OK":
        technical_errors.append("internal control evidence registry integrity failed")
    if operations.get("errors"):
        technical_errors.append("research roster or restrictions input is invalid")
    if provider.get("integrity_status") != "OK":
        technical_errors.append("provider registry integrity failed")
    blockers = [reason for stage in stages for reason in stage.get("blockers", [])]
    blockers.extend(archive_control["blockers"])
    return {
        "version": BUSINESS_ONBOARDING_VERSION,
        "generated_at": now_iso(),
        "enabled": cfg["enabled"],
        "enforced": cfg["enforce"],
        "technical_status": "READY" if not technical_errors else "FAIL",
        "business_status": "READY" if cfg["enabled"] and not blockers and not technical_errors else "BLOCKED",
        "technical_errors": technical_errors,
        "business_blockers": blockers,
        "stages": stages,
        "control_evidence": control,
        "roster": roster,
        "provider": providers,
        "archive_control_evidence": archive_control,
        "safety_boundary": SAFETY_BOUNDARY,
        "automatic_assignment_executed": False,
        "automatic_approval_executed": False,
        "automatic_activation_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }


def build_business_onboarding(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = business_onboarding_config(config)
    preflight = build_business_onboarding_preflight(config)
    workpapers = load_research_workpapers(config)
    workpaper_verification = verify_research_workpapers(config)
    vault = verify_evidence_vault(config)
    market = load_latest_market_data_governance(config)
    llm = load_latest_governance_summary(config)
    pilot = _load_latest_pilot(config)
    stages = list(preflight["stages"])
    stages.extend([
        _official_evidence_stage(workpapers),
        _governance_remediation_stage(market, llm),
        _production_archive_stage(config, cfg, preflight.get("control_evidence", {}), vault, workpaper_verification),
        _shadow_readiness_stage(pilot),
        _go_no_go_stage(pilot),
    ])
    by_name = {stage["phase"]: stage for stage in stages}
    stages = [by_name[name] for name in PHASE_ORDER]
    current_phase = next((stage["phase"] for stage in stages if stage["status"] != "PASS"), "completed")
    actions = _build_action_queue(stages)
    technical_errors = list(preflight.get("technical_errors", []))
    if workpaper_verification.get("technical_status") == "FAIL":
        technical_errors.append("research workpaper technical verification failed")
    if vault.get("technical_status") == "FAIL":
        technical_errors.append("official evidence vault technical verification failed")
    if pilot and pilot.get("technical_readiness") == "FAIL":
        technical_errors.append("production pilot technical verification failed")
    business_blockers = [reason for stage in stages for reason in stage.get("blockers", [])]
    snapshot = {
        "version": BUSINESS_ONBOARDING_VERSION,
        "generated_at": now_iso(),
        "enabled": cfg["enabled"],
        "enforced": cfg["enforce"],
        "organization_id": cfg["organization_id"],
        "technical_status": "READY" if not technical_errors else "FAIL",
        "business_status": "READY" if cfg["enabled"] and not business_blockers and not technical_errors else "BLOCKED",
        "current_phase": current_phase,
        "completed_phase_count": sum(stage["status"] == "PASS" for stage in stages),
        "phase_count": len(stages),
        "stages": stages,
        "action_queue": actions,
        "action_count": len(actions),
        "technical_errors": sorted(set(technical_errors)),
        "business_blockers": business_blockers,
        "control_evidence": _control_summary(preflight.get("control_evidence", {})),
        "production_candidate_fingerprint": str(pilot.get("candidate_fingerprint") or "") if pilot else "",
        "input_fingerprint": object_hash({
            "control_event_hash": (preflight.get("control_evidence") or {}).get("last_event_hash", ""),
            "roster": preflight.get("roster", {}),
            "provider": preflight.get("provider", {}),
            "vault_event_hash": vault.get("last_event_hash", ""),
            "workpaper_event_hash": workpapers.get("last_event_hash", ""),
            "pilot_candidate_fingerprint": str(pilot.get("candidate_fingerprint") or "") if pilot else "",
        }),
        "safety_boundary": SAFETY_BOUNDARY,
        "raw_control_files_stored": False,
        "automatic_assignment_executed": False,
        "automatic_approval_executed": False,
        "automatic_activation_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
    }
    return snapshot


def sync_business_onboarding(config: Dict[str, Any]) -> Dict[str, Any]:
    control = load_control_evidence_registry(config)
    write_control_evidence_snapshot(control)
    snapshot = build_business_onboarding(config)
    snapshot.update(write_business_onboarding_snapshot(snapshot, config))
    return snapshot


def load_latest_business_onboarding(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = business_onboarding_config(config)
    return _read_json(project_path(str(Path(cfg["report_dir"]) / "latest.json")))


def write_business_onboarding_snapshot(snapshot: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    cfg = business_onboarding_config(config)
    report_dir = project_path(cfg["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "latest.json"
    md_path = report_dir / "latest.md"
    atomic_write_text(json_path, json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    atomic_write_text(md_path, render_business_onboarding_markdown(snapshot), encoding="utf-8-sig")
    return {"business_onboarding_json_path": str(json_path), "business_onboarding_path": str(md_path)}


def generate_business_onboarding_pack(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = business_onboarding_config(config)
    target = project_path(cfg["intake_pack_dir"])
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "00_README.json": {
            "version": BUSINESS_ONBOARDING_VERSION,
            "purpose": "Human-completed onboarding intake. Templates do not grant approval or alter production state.",
            "operating_order": PHASE_ORDER,
            "safety_boundary": SAFETY_BOUNDARY,
            "rules": [
                "Store raw confidential documents only in the approved external archive.",
                "Register only the external archive URI and locally computed SHA-256 metadata in Stock Agent.",
                "Use real active human actors for review and approval; service accounts are prohibited.",
                "Do not install scheduled tasks until provider, archive, roster, and governance gates are ready.",
            ],
        },
        "01_governance_scope.example.json": {
            "organization_id": "organization:replace_with_legal_entity_id",
            "required_control_evidence": [
                {"category": category, "subject_id": "organization:replace_with_legal_entity_id", "evidence_id": "control:replace_me"}
                for category in cfg["required_governance_evidence"] + cfg["required_archive_evidence"]
            ],
            "human_confirmation_required": True,
        },
        "02_human_roster.example.json": {
            "source_of_truth": "config/research_roster.local.json",
            "minimum_distinct_humans": 7,
            "required_roles": _required_roles(config),
            "principal_template": {
                "actor_id": "human:replace_me",
                "display_name": "Replace Me",
                "principal_type": "human",
                "active": False,
                "roles": ["lead_analyst"],
                "information_side": "public",
                "qualifications": {
                    "securities_analyst_registration": {
                        "status": "unverified",
                        "registration_id": "",
                        "verified_at": "",
                        "evidence_ref": "control:replace_me",
                    }
                },
                "authorizations": [],
                "onboarding_evidence_refs": {
                    "human_employment": "control:replace_me",
                    "human_confidentiality": "control:replace_me",
                    "human_training": "control:replace_me",
                    "role_appointment": "control:replace_me",
                },
                "coverage_entities": ["*"],
                "restricted_entities": [],
                "max_active_cases": 10,
            },
            "activation_is_manual": True,
        },
        "03_provider_due_diligence.example.json": {
            "source_of_truth": "provider-init/provider-due-diligence/provider-review/provider-activate commands",
            "required_scopes": provider_registry_config(config)["required_scopes"],
            "required_evidence_fields": provider_registry_config(config)["required_evidence"],
            "evidence_values_must_be_verified_control_ids": True,
            "minimum_independent_active_providers": int(
                (config.get("production_pilot", {}) or {}).get("minimum_active_providers", 2) or 2
            ),
            "activation_is_manual": True,
        },
        "04_production_approval.example.json": {
            "candidate_fingerprint": "copy_from_reports/production_pilot/latest.json",
            "gates": (config.get("production_pilot", {}) or {}).get("required_approval_gates", {}),
            "each_gate_requires_distinct_human": True,
            "approval_evidence_ref": "control:verified_approval_support_or_managed_archive_uri",
            "automatic_approval": False,
            "automatic_activation": False,
        },
    }
    paths = []
    for name, payload in files.items():
        path = target / name
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        paths.append(str(path))
    return {
        "generated_at": now_iso(),
        "pack_dir": str(target),
        "files": paths,
        "template_only": True,
        "production_state_changed": False,
        "automatic_approval_executed": False,
    }


def render_business_onboarding_markdown(snapshot: Dict[str, Any]) -> str:
    lines = [
        "# Business Onboarding v29",
        "",
        "- generated_at: {0}".format(snapshot.get("generated_at", "")),
        "- technical: {0}".format(snapshot.get("technical_status", "UNKNOWN")),
        "- business: {0}".format(snapshot.get("business_status", "BLOCKED")),
        "- current phase: {0}".format(snapshot.get("current_phase", "")),
        "- completed phases: {0}/{1}".format(snapshot.get("completed_phase_count", 0), snapshot.get("phase_count", 0)),
        "- production candidate fingerprint: {0}".format(snapshot.get("production_candidate_fingerprint", "") or "none"),
        "- raw control files stored: false",
        "- automatic assignment / approval / activation / publication / trading: false / false / false / false / false",
        "",
        "## Ordered Operating Stages",
        "",
        "| # | Phase | Owner | Status | Evidence | Blockers |",
        "|---:|---|---|---|---:|---|",
    ]
    for index, stage in enumerate(snapshot.get("stages", []), 1):
        lines.append("| {0} | {1} | {2} | {3} | {4}/{5} | {6} |".format(
            index,
            stage.get("phase", ""),
            stage.get("owner_role", ""),
            stage.get("status", ""),
            stage.get("ready_count", 0),
            stage.get("required_count", 0),
            "<br>".join(stage.get("blockers", [])[:5]) or "none",
        ))
    lines.extend(["", "## Ordered Human Action Queue", ""])
    if not snapshot.get("action_queue"):
        lines.append("- none; all business gates are satisfied for the current candidate fingerprint")
    for action in snapshot.get("action_queue", []):
        lines.extend([
            "### {0}. {1}".format(action.get("sequence", ""), action.get("phase", "")),
            "",
            "- owner role: {0}".format(action.get("owner_role", "")),
            "- action: {0}".format(action.get("action", "")),
            "- evidence required: {0}".format(str(bool(action.get("evidence_required"))).lower()),
            "- automatic execution: false",
            "",
        ])
    if snapshot.get("technical_errors"):
        lines.extend(["## Technical Errors", ""])
        lines.extend("- {0}".format(item) for item in snapshot["technical_errors"])
    lines.extend(["## Safety Boundary", "", snapshot.get("safety_boundary", SAFETY_BOUNDARY), ""])
    return "\n".join(lines).rstrip() + "\n"


def _governance_scope_stage(
    config: Dict[str, Any], cfg: Dict[str, Any], registry: Dict[str, Any]
) -> Dict[str, Any]:
    organization_id = cfg["organization_id"]
    blockers = []
    ready = 0
    if not organization_id or "replace" in organization_id.lower() or "pending" in organization_id.lower():
        blockers.append("real legal-entity organization_id is not configured")
    for category in cfg["required_governance_evidence"]:
        matches = [
            item for item in (registry.get("evidence") or {}).values()
            if item.get("category") == category
            and item.get("subject_id") == organization_id
            and item.get("status") == "verified"
        ]
        if matches:
            ready += 1
        else:
            blockers.append("missing verified organization control evidence: {0}".format(category))
    return _stage(
        "governance_scope", ready, len(cfg["required_governance_evidence"]), blockers,
        "Configure the real legal entity and independently verify mandate, scope, and prohibited-action controls.",
    )


def _human_roster_stage(
    config: Dict[str, Any], cfg: Dict[str, Any], operations: Dict[str, Any]
) -> Dict[str, Any]:
    required_roles = _required_roles(config)
    eligible_by_role: Dict[str, List[str]] = {}
    ineligible_by_role: Dict[str, List[str]] = {}
    for role in required_roles:
        eligible = []
        ineligible = []
        for principal in operations.get("principals", []):
            if not _baseline_role_eligible(principal, role):
                continue
            actor = str(principal.get("actor_id") or "")
            required_categories = list(cfg["required_human_evidence"])
            required_categories.extend(ROLE_SPECIFIC_HUMAN_EVIDENCE.get(role, []))
            refs = principal.get("onboarding_evidence_refs") or {}
            missing = []
            for category in required_categories:
                evidence_id = str(refs.get(category) or "")
                if category == "human_qualification":
                    registration = (
                        (principal.get("qualifications") or {}).get("securities_analyst_registration") or {}
                    )
                    if str(registration.get("evidence_ref") or "").strip() != evidence_id:
                        missing.append("human_qualification(roster evidence_ref mismatch)")
                        continue
                ok, _, _ = resolve_verified_control_evidence(
                    config, evidence_id, categories=[category], subject_id=actor
                ) if evidence_id else (False, "missing", {})
                if not ok:
                    missing.append(category)
            if missing:
                ineligible.append("{0} missing verified evidence: {1}".format(
                    actor, ", ".join(sorted(set(missing)))
                ))
                continue
            eligible.append(actor)
        eligible_by_role[role] = sorted(set(eligible))
        ineligible_by_role[role] = sorted(set(ineligible))
    missing_roles = [role for role, actors in eligible_by_role.items() if not actors]
    distinct = _distinct_assignment_possible(eligible_by_role)
    blockers = []
    if operations.get("errors"):
        blockers.extend("roster validation: {0}".format(item) for item in operations["errors"][:5])
    blockers.extend("no onboarding-ready human for role: {0}".format(role) for role in missing_roles)
    for role in missing_roles:
        blockers.extend("{0}/{1}".format(role, item) for item in ineligible_by_role.get(role, [])[:5])
    if cfg["require_distinct_humans"] and not missing_roles and not distinct:
        blockers.append("required roles cannot be assigned to distinct human actors")
    return {
        **_stage(
            "human_roster", len(required_roles) - len(missing_roles), len(required_roles), blockers,
            "Enter real people in the local roster, attach verified control evidence, and keep protected roles distinct.",
        ),
        "eligible_humans_by_role": eligible_by_role,
        "ineligible_humans_by_role": ineligible_by_role,
        "distinct_actor_separation_possible": distinct,
        "human_count": sum(item.get("principal_type") == "human" for item in operations.get("principals", [])),
    }


def _provider_stage(config: Dict[str, Any], registry: Dict[str, Any]) -> Dict[str, Any]:
    minimum = max(1, int((config.get("production_pilot", {}) or {}).get("minimum_active_providers", 2) or 2))
    active = list(registry.get("active_provider_ids") or [])
    blockers = []
    if registry.get("integrity_status") != "OK":
        blockers.append("provider registry integrity failed")
    if len(active) < minimum:
        blockers.append("active independently reviewed providers {0}/{1}".format(len(active), minimum))
    return {
        **_stage(
            "provider_authorization", min(len(active), minimum), minimum, blockers,
            "Complete legal, data-rights, security, SLA, incident, and exit reviews for at least two independent providers.",
        ),
        "active_provider_ids": active,
        "provider_count": registry.get("provider_count", 0),
    }


def _official_evidence_stage(workpapers: Dict[str, Any]) -> Dict[str, Any]:
    critical = []
    missing = []
    for item in workpapers.get("workpapers", []) or []:
        priority = str((item.get("case") or {}).get("priority") or "")
        if priority not in {"P0", "P1"}:
            continue
        critical.append(item)
        eligible = any(
            source.get("primary_source_eligible") is True
            and source.get("original_document_retained") is True
            and str(source.get("vault_document_id") or "").strip()
            for source in (item.get("source_records") or [])
        )
        if not eligible:
            missing.append({
                "workpaper_id": item.get("workpaper_id", ""),
                "case_id": item.get("case_id", ""),
                "priority": priority,
                "entity": (item.get("case") or {}).get("entity", ""),
                "signal_ids": list((item.get("case") or {}).get("signal_ids", []) or []),
                "candidate_source_urls": sorted(set(
                    str(source.get("origin_signal_url") or source.get("source_url") or "")
                    for source in (item.get("source_records") or [])
                    if str(source.get("origin_signal_url") or source.get("source_url") or "").strip()
                )),
            })
    blockers = ["P0/P1 workpaper lacks retained official primary evidence: {0}".format(item["case_id"]) for item in missing]
    return {
        **_stage(
            "official_evidence_backfill", len(critical) - len(missing), len(critical), blockers,
            "Locate the exact exchange/CNInfo/issuer original, import it under human control, then link it to every affected signal.",
        ),
        "missing_workpapers": missing,
    }


def _governance_remediation_stage(market: Dict[str, Any], llm: Dict[str, Any]) -> Dict[str, Any]:
    pending = int(((llm.get("summary") or {}).get("pending_exception_count", 0)) or 0)
    checks = [
        (str(market.get("status") or "UNKNOWN") in {"OK", "PASS", "READY"}, "market data governance is not OK/PASS"),
        (str(llm.get("status") or "UNKNOWN") in {"OK", "PASS", "READY"}, "LLM governance is not OK/PASS"),
        (pending == 0, "pending LLM governance exceptions: {0}".format(pending)),
    ]
    blockers = [reason for ok, reason in checks if not ok]
    return {
        **_stage(
            "governance_remediation", len(checks) - len(blockers), len(checks), blockers,
            "Resolve source authorization, freshness, external-data classification, citation, and exception records without relaxing controls.",
        ),
        "market_status": market.get("status", "UNKNOWN"),
        "llm_status": llm.get("status", "UNKNOWN"),
        "pending_llm_exceptions": pending,
    }


def _production_archive_stage(
    config: Dict[str, Any],
    cfg: Dict[str, Any],
    control: Dict[str, Any],
    vault: Dict[str, Any],
    workpapers: Dict[str, Any],
) -> Dict[str, Any]:
    archive_control = _archive_control_evidence_status(config, cfg, control)
    checks = [
        (vault.get("technical_status") == "READY", "official evidence vault technical status is not READY"),
        (vault.get("production_archive_ready") is True, "official evidence production archive is not approved and active"),
        (workpapers.get("technical_status") == "READY", "research workpaper technical status is not READY"),
        (workpapers.get("production_archive_ready") is True, "research workpaper production archive is not approved"),
    ]
    blockers = [reason for ok, reason in checks if not ok]
    blockers.extend(archive_control["blockers"])
    return _stage(
        "production_archive",
        (len(checks) - sum(not ok for ok, _ in checks)) + archive_control["ready_count"],
        len(checks) + archive_control["required_count"],
        blockers,
        "Complete external immutable archive contracts, retention controls, independent compliance/security reviews, and manual activation.",
    )


def _archive_control_evidence_status(
    config: Dict[str, Any], cfg: Dict[str, Any], control: Dict[str, Any]
) -> Dict[str, Any]:
    organization_id = cfg["organization_id"]
    ready = 0
    blockers = []
    for category in cfg["required_archive_evidence"]:
        match = any(
            item.get("category") == category
            and item.get("subject_id") == organization_id
            and item.get("status") == "verified"
            for item in (control.get("evidence") or {}).values()
        )
        if match:
            ready += 1
        else:
            blockers.append("missing verified production archive control evidence: {0}".format(category))
    return {"ready_count": ready, "required_count": len(cfg["required_archive_evidence"]), "blockers": blockers}


def _shadow_readiness_stage(pilot: Dict[str, Any]) -> Dict[str, Any]:
    shadow = pilot.get("shadow_evidence") or {}
    minimum_runs = int(shadow.get("minimum_runs", 5) or 5)
    minimum_days = int(shadow.get("minimum_trading_days", 3) or 3)
    runs = int(shadow.get("qualifying_run_count", 0) or 0)
    days = int(shadow.get("qualifying_trading_day_count", 0) or 0)
    unreconciled = int(shadow.get("unreconciled_run_count", 0) or 0)
    blockers = []
    if runs < minimum_runs:
        blockers.append("qualifying scheduled shadow runs {0}/{1}".format(runs, minimum_runs))
    if days < minimum_days:
        blockers.append("qualifying trading days {0}/{1}".format(days, minimum_days))
    if unreconciled:
        blockers.append("unreconciled shadow runs: {0}".format(unreconciled))
    if not pilot:
        blockers.append("production pilot snapshot is missing")
    return {
        **_stage(
            "shadow_readiness", int(runs >= minimum_runs) + int(days >= minimum_days) + int(unreconciled == 0 and bool(pilot)), 3,
            blockers,
            "After all preflight gates pass, collect genuine Windows Task Scheduler runs across trading days; manual reruns never count.",
        ),
        "qualifying_runs": runs,
        "minimum_runs": minimum_runs,
        "qualifying_days": days,
        "minimum_days": minimum_days,
        "unreconciled_runs": unreconciled,
    }


def _go_no_go_stage(pilot: Dict[str, Any]) -> Dict[str, Any]:
    overall = str(
        pilot.get("overall_status") or pilot.get("go_live_status") or pilot.get("go_live_decision") or "NO_GO"
    )
    approvals = pilot.get("approvals") or {}
    required = list(((pilot.get("policy") or {}).get("required_approval_gates") or {}).keys())
    if not required:
        required = list((pilot.get("required_approval_gates") or {}).keys())
    if not required:
        required = list(((pilot.get("requirements") or {}).get("required_approval_gates") or {}).keys())
    approved = sum((approvals.get(gate) or {}).get("decision") == "approve" for gate in required)
    activation = pilot.get("activation") or {}
    blockers = []
    if overall != "GO":
        blockers.append("production pilot remains {0}".format(overall or "NO_GO"))
    if required and approved < len(required):
        blockers.append("fingerprint-bound human approvals {0}/{1}".format(approved, len(required)))
    if activation.get("active") is not True:
        blockers.append("current candidate fingerprint is not manually activated")
    return {
        **_stage(
            "go_no_go", int(overall == "GO") + int(bool(required) and approved == len(required)) + int(activation.get("active") is True), 3,
            blockers,
            "Six distinct authorized humans decide on the current fingerprint, then the publication approver performs manual internal-research activation.",
        ),
        "pilot_status": overall,
        "human_approvals": approved,
        "required_human_approvals": len(required),
        "activation_active": activation.get("active") is True,
    }


def _stage(phase: str, ready: int, required: int, blockers: Sequence[str], action: str) -> Dict[str, Any]:
    unique_blockers = list(dict.fromkeys(str(item) for item in blockers if str(item).strip()))
    return {
        "phase": phase,
        "owner_role": PHASE_OWNERS[phase],
        "status": "PASS" if not unique_blockers else "BLOCKED",
        "ready_count": max(0, int(ready)),
        "required_count": max(0, int(required)),
        "blockers": unique_blockers,
        "required_action": action,
        "automatic_execution": False,
    }


def _build_action_queue(stages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actions = []
    for stage in stages:
        if stage.get("status") == "PASS":
            continue
        actions.append({
            "sequence": len(actions) + 1,
            "phase": stage.get("phase", ""),
            "owner_role": stage.get("owner_role", ""),
            "action": stage.get("required_action", ""),
            "blockers": list(stage.get("blockers", []) or []),
            "evidence_required": stage.get("phase") not in {"shadow_readiness"},
            "automatic_execution": False,
        })
    return actions


def _required_roles(config: Dict[str, Any]) -> List[str]:
    gates = (config.get("production_pilot", {}) or {}).get("required_approval_gates", {})
    roles = set(str(role) for role in gates.values() if str(role).strip()) if isinstance(gates, dict) else set()
    roles.add("research_coordinator")
    return sorted(roles)


def _baseline_role_eligible(principal: Dict[str, Any], role: str) -> bool:
    if principal.get("principal_type") != "human" or not principal.get("active"):
        return False
    if role not in (principal.get("roles") or []):
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

    return bool(roles) and all(eligible_by_role.values()) and assign(0, set())


def _control_summary(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "technical_status": value.get("technical_status", "UNKNOWN"),
        "integrity_status": value.get("integrity_status", "UNKNOWN"),
        "event_count": value.get("event_count", 0),
        "evidence_count": value.get("evidence_count", 0),
        "verified_count": len(value.get("verified_evidence_ids", []) or []),
        "raw_files_stored": False,
    }


def _load_latest_pilot(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("production_pilot", {}) if isinstance(config, dict) else {}
    report_dir = str(raw.get("report_dir") or "reports/production_pilot")
    return _read_json(project_path(str(Path(report_dir) / "latest.json")))


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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
