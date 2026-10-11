"""Evidence-driven G0.5 value validation for paired research cases.

This module measures whether the system improves a real research workflow. It
does not run a model, publish research, approve production, or execute trades.
Every operational write is an append-only, hash-chained event made by an
authorized human. Blind evaluation uses a commit/reveal mapping so the public
assignment contains only candidate A/B until scoring is complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .audit_log import (
    append_hash_chained_jsonl,
    atomic_write_text,
    canonical_json,
    file_hash,
    load_hash_chained_jsonl,
    object_hash,
)
from .config import project_path
from .models import now_iso
from .research_operations import load_operations_inputs


VALUE_VALIDATION_VERSION = "1.0"
SAFETY_BOUNDARY = (
    "Internal research value measurement only. No automatic approval, publication, "
    "provider activation, trading, order placement, or portfolio action."
)
EVENT_TYPES = {
    "study_opened",
    "case_registered",
    "case_excluded",
    "cohort_frozen",
    "baseline_recorded",
    "treatment_frozen",
    "assisted_recorded",
    "blind_assignment_prepared",
    "blind_evaluation_recorded",
    "blind_mapping_revealed",
    "gate_decision_recorded",
}
SEGMENT_FIELDS = (
    "collection",
    "evidence",
    "analysis",
    "drafting",
    "review",
    "correction",
)
QUALITY_DIMENSIONS = (
    "evidence_quality",
    "analytical_soundness",
    "completeness",
    "actionability",
    "clarity",
    "citation_accuracy",
)
DEFECT_FIELDS = (
    "material_errors",
    "material_omissions",
    "unsupported_claims",
)
GATE_DECISIONS = {"CONTINUE", "NARROW", "STOP", "EXTEND_ONCE"}
COORDINATION_ROLES = {"research_coordinator", "research_supervisor"}
_SAFE_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
_SAFE_REF = re.compile(r"^(?:archive|vault|case|report):[^\s]{2,480}$", re.IGNORECASE)
_SECRET_PATTERN = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
_NON_BLIND_REF_TERMS = (
    "baseline",
    "assisted",
    "manual",
    "human",
    "agent",
    "deepseek",
    "人工",
    "系统",
)


class ValueValidationPolicyError(ValueError):
    pass


def value_validation_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("value_validation", {}) if isinstance(config, dict) else {}
    minimum = max(2, int(raw.get("minimum_cases", 20) or 20))
    maximum = max(minimum, int(raw.get("maximum_cases", 30) or 30))
    return {
        "enabled": bool(raw.get("enabled", False)),
        "study_id": str(raw.get("study_id") or "a_share_research_value_v1").strip(),
        "event_path": str(raw.get("event_path") or "data/value_validation/events.jsonl"),
        "sealed_dir": str(raw.get("sealed_dir") or "data/value_validation/sealed"),
        "report_dir": str(raw.get("report_dir") or "reports/value_validation"),
        "blind_report_dir": str(
            raw.get("blind_report_dir") or "reports/value_validation/blind_assignments"
        ),
        "minimum_cases": minimum,
        "maximum_cases": maximum,
        "minimum_strata": max(1, int(raw.get("minimum_strata", 2) or 2)),
        "target_efficiency_improvement": min(
            0.95, max(0.0, float(raw.get("target_efficiency_improvement", 0.20) or 0.20))
        ),
        "quality_non_inferiority_margin": min(
            20.0, max(0.0, float(raw.get("quality_non_inferiority_margin", 0.0) or 0.0))
        ),
        "minimum_blind_coverage": min(
            1.0, max(0.5, float(raw.get("minimum_blind_coverage", 1.0) or 1.0))
        ),
        "maximum_unauthorized_egress": max(
            0, int(raw.get("maximum_unauthorized_egress", 0) or 0)
        ),
        "require_same_analyst": bool(raw.get("require_same_analyst", True)),
        "allowed_priorities": _clean_list(raw.get("allowed_priorities", ["P0", "P1"])),
        "allowed_data_classifications": _clean_list(
            raw.get("allowed_data_classifications", ["public"])
        ),
        "qualifying_measurement_methods": _clean_list(
            raw.get("qualifying_measurement_methods", ["time_log"])
        ),
        "release_audit_path": str(
            raw.get("release_audit_path") or "reports/release_audit/latest.json"
        ),
    }


def open_value_study(
    config: Dict[str, Any],
    *,
    title: str,
    scenario: str,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, COORDINATION_ROLES)
    status = build_value_validation_status(config)
    if status["study"]:
        raise ValueValidationPolicyError("value study already exists")
    title = _required_text(title, "title", 200)
    scenario = _required_text(scenario, "scenario", 500)
    _reject_secret_text([title, scenario])
    policy = _policy_snapshot(cfg)
    return _append_event(
        cfg,
        "study_opened",
        actor,
        actor_role,
        {
            "title": title,
            "scenario": scenario,
            "policy": policy,
            "policy_fingerprint": object_hash(policy),
        },
    )


def register_value_case(
    config: Dict[str, Any],
    *,
    case_id: str,
    case_ref: str,
    case_sha256: str,
    priority: str,
    stratum: str,
    evidence_cutoff: str,
    data_classification: str,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, COORDINATION_ROLES)
    status = _require_study(config)
    if status["cohort"]:
        raise ValueValidationPolicyError("cohort is frozen; new cases are not allowed")
    case_id = _validate_id(case_id, "case_id")
    if case_id in status["cases"]:
        raise ValueValidationPolicyError("case is already registered: {0}".format(case_id))
    priority = str(priority or "").strip().upper()
    if priority not in cfg["allowed_priorities"]:
        raise ValueValidationPolicyError("priority is outside the frozen value-test scope")
    classification = str(data_classification or "").strip().lower()
    if classification not in {item.lower() for item in cfg["allowed_data_classifications"]}:
        raise ValueValidationPolicyError("data classification is not allowed for this value study")
    case_ref = _validate_ref(case_ref, "case_ref", require_blind=False)
    case_sha256 = _validate_sha256(case_sha256, "case_sha256")
    stratum = _required_text(stratum, "stratum", 80)
    cutoff = _normalize_timestamp(evidence_cutoff, "evidence_cutoff")
    _reject_secret_text([case_id, case_ref, stratum])
    return _append_event(
        cfg,
        "case_registered",
        actor,
        actor_role,
        {
            "case_id": case_id,
            "case_ref": case_ref,
            "case_sha256": case_sha256,
            "priority": priority,
            "stratum": stratum,
            "evidence_cutoff": cutoff,
            "data_classification": classification,
            "outcome_label_recorded": False,
        },
    )


def exclude_value_case(
    config: Dict[str, Any],
    *,
    case_id: str,
    reason: str,
    category: str,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"research_supervisor"})
    status = _require_study(config)
    case = _require_case(status, case_id)
    if case.get("excluded"):
        raise ValueValidationPolicyError("case is already excluded")
    if case.get("baseline") or case.get("assisted"):
        raise ValueValidationPolicyError("case with recorded observations cannot be excluded")
    reason = _required_text(reason, "reason", 500)
    category = _required_text(category, "category", 80)
    _reject_secret_text([reason, category])
    return _append_event(
        cfg,
        "case_excluded",
        actor,
        actor_role,
        {"case_id": case["case_id"], "reason": reason, "category": category},
    )


def freeze_value_cohort(
    config: Dict[str, Any],
    *,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"research_supervisor"})
    status = _require_study(config)
    if status["cohort"]:
        raise ValueValidationPolicyError("cohort is already frozen")
    active = [value for value in status["cases"].values() if not value.get("excluded")]
    if not (cfg["minimum_cases"] <= len(active) <= cfg["maximum_cases"]):
        raise ValueValidationPolicyError(
            "cohort requires {0}-{1} active cases; found {2}".format(
                cfg["minimum_cases"], cfg["maximum_cases"], len(active)
            )
        )
    strata = sorted({str(value.get("stratum") or "") for value in active})
    if len(strata) < cfg["minimum_strata"]:
        raise ValueValidationPolicyError(
            "cohort requires at least {0} strata".format(cfg["minimum_strata"])
        )
    identity = _cohort_identity(active)
    return _append_event(
        cfg,
        "cohort_frozen",
        actor,
        actor_role,
        {
            "case_ids": [item["case_id"] for item in identity],
            "case_set_fingerprint": object_hash(identity),
            "case_count": len(identity),
            "strata": strata,
            "policy_fingerprint": status["study"].get("policy_fingerprint", ""),
        },
    )


def record_value_baseline(
    config: Dict[str, Any],
    *,
    case_id: str,
    output_ref: str,
    output_sha256: str,
    metrics: Dict[str, Any],
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"lead_analyst"})
    status = _require_study(config)
    case = _require_frozen_case(status, case_id)
    if case.get("baseline"):
        raise ValueValidationPolicyError("baseline is already recorded")
    if status["treatment"]:
        raise ValueValidationPolicyError("baseline cannot be added after treatment freeze")
    observation = _normalize_observation(metrics)
    output_ref = _validate_ref(output_ref, "output_ref", require_blind=True)
    output_sha256 = _validate_sha256(output_sha256, "output_sha256")
    return _append_event(
        cfg,
        "baseline_recorded",
        actor,
        actor_role,
        {
            "case_id": case["case_id"],
            "output_ref": output_ref,
            "output_sha256": output_sha256,
            "observation": observation,
            "llm_used": False,
        },
    )


def freeze_value_treatment(
    config: Dict[str, Any],
    *,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"research_supervisor"})
    status = _require_study(config)
    if status["treatment"]:
        raise ValueValidationPolicyError("treatment is already frozen")
    cohort_ids = status.get("cohort", {}).get("case_ids", []) or []
    if not cohort_ids:
        raise ValueValidationPolicyError("cohort must be frozen before treatment")
    missing = [case_id for case_id in cohort_ids if not status["cases"][case_id].get("baseline")]
    if missing:
        raise ValueValidationPolicyError("all cohort baselines are required before treatment freeze")
    current_policy = _policy_snapshot(cfg)
    if object_hash(current_policy) != status["study"].get("policy_fingerprint"):
        raise ValueValidationPolicyError("value validation policy changed after study opening")
    release = _load_release_audit(cfg)
    if not release.get("technical_release_ready") or release.get("status") != "READY":
        raise ValueValidationPolicyError("G0 release audit must be READY before treatment freeze")
    manifest_id = _validate_sha256(str(release.get("manifest_id") or ""), "manifest_id")
    identity = release.get("identity_projection", {}) or {}
    models = identity.get("models", {}) or {}
    pi = identity.get("pi", {}) or {}
    return _append_event(
        cfg,
        "treatment_frozen",
        actor,
        actor_role,
        {
            "manifest_id": manifest_id,
            "release_candidate_fingerprint": str(
                identity.get("release_candidate_fingerprint") or ""
            ),
            "llm_runtime": str(models.get("llm_runtime") or ""),
            "llm_model": str(models.get("llm_model") or ""),
            "pi_provider": str(models.get("pi_provider") or ""),
            "pi_model": str(models.get("pi_model") or ""),
            "pi_skill_bundle_sha256": str(pi.get("skill_bundle_sha256") or ""),
            "policy_fingerprint": status["study"].get("policy_fingerprint", ""),
            "release_ready_at_freeze": True,
        },
    )


def record_value_assisted(
    config: Dict[str, Any],
    *,
    case_id: str,
    output_ref: str,
    output_sha256: str,
    metrics: Dict[str, Any],
    observed_manifest_id: str,
    outcome_hidden: bool,
    evidence_cutoff_respected: bool,
    unauthorized_egress_count: int,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"lead_analyst"})
    status = _require_study(config)
    case = _require_frozen_case(status, case_id)
    if not status["treatment"]:
        raise ValueValidationPolicyError("treatment must be frozen before assisted observation")
    if case.get("assisted"):
        raise ValueValidationPolicyError("assisted observation is already recorded")
    observation = _normalize_observation(metrics, assisted=True)
    output_ref = _validate_ref(output_ref, "output_ref", require_blind=True)
    output_sha256 = _validate_sha256(output_sha256, "output_sha256")
    baseline = case.get("baseline", {}) or {}
    if output_ref == baseline.get("output_ref") or output_sha256 == baseline.get("output_sha256"):
        raise ValueValidationPolicyError(
            "assisted output must be distinct from the frozen baseline output"
        )
    observed_manifest_id = _validate_sha256(observed_manifest_id, "observed_manifest_id")
    unauthorized = max(0, int(unauthorized_egress_count or 0))
    return _append_event(
        cfg,
        "assisted_recorded",
        actor,
        actor_role,
        {
            "case_id": case["case_id"],
            "output_ref": output_ref,
            "output_sha256": output_sha256,
            "observation": observation,
            "observed_manifest_id": observed_manifest_id,
            "attestations": {
                "outcome_hidden": bool(outcome_hidden),
                "evidence_cutoff_respected": bool(evidence_cutoff_respected),
            },
            "unauthorized_egress_count": unauthorized,
        },
    )


def prepare_blind_assignment(
    config: Dict[str, Any],
    *,
    case_id: str,
    actor: str,
    actor_role: str,
    mapping: Optional[Dict[str, str]] = None,
    nonce: str = "",
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, COORDINATION_ROLES)
    status = _require_study(config)
    case = _require_frozen_case(status, case_id)
    if not case.get("baseline") or not case.get("assisted"):
        raise ValueValidationPolicyError("baseline and assisted outputs are required for blinding")
    if case.get("blind_assignment"):
        raise ValueValidationPolicyError("blind assignment already exists")
    chosen = mapping or (
        {"A": "baseline", "B": "assisted"}
        if secrets.choice([True, False])
        else {"A": "assisted", "B": "baseline"}
    )
    _validate_mapping(chosen)
    nonce = nonce or secrets.token_hex(24)
    assignment_id = "blind:{0}:{1}".format(case["case_id"], secrets.token_hex(8))
    commitment = object_hash(
        {
            "assignment_id": assignment_id,
            "case_id": case["case_id"],
            "mapping": chosen,
            "nonce": nonce,
        }
    )
    candidates = {}
    for label in ("A", "B"):
        variant = chosen[label]
        source = case[variant]
        candidates[label] = {
            "output_ref": source["output_ref"],
            "output_sha256": source["output_sha256"],
        }
    package = {
        "schema_version": VALUE_VALIDATION_VERSION,
        "study_id": cfg["study_id"],
        "case_id": case["case_id"],
        "assignment_id": assignment_id,
        "candidates": candidates,
        "rubric": {
            "quality_dimensions": list(QUALITY_DIMENSIONS),
            "defect_fields": list(DEFECT_FIELDS),
            "score_range": [0, 100],
        },
        "blinding_rules": [
            "Reviewer receives this package only, not the value-validation event ledger or sealed mapping.",
            "Score candidate A and B independently before requesting reveal.",
            "Do not infer authorship, model, or workflow variant.",
        ],
        "mapping_included": False,
    }
    package_text = json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    package_path = project_path(cfg["blind_report_dir"]) / "{0}.json".format(_path_token(assignment_id))
    sealed = {
        "schema_version": VALUE_VALIDATION_VERSION,
        "study_id": cfg["study_id"],
        "case_id": case["case_id"],
        "assignment_id": assignment_id,
        "mapping": chosen,
        "nonce": nonce,
        "commitment": commitment,
    }
    sealed_path = project_path(cfg["sealed_dir"]) / "{0}.json".format(_path_token(assignment_id))
    atomic_write_text(package_path, package_text)
    atomic_write_text(
        sealed_path,
        json.dumps(sealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    try:
        os.chmod(str(sealed_path), 0o600)
    except OSError:
        pass
    event = _append_event(
        cfg,
        "blind_assignment_prepared",
        actor,
        actor_role,
        {
            "case_id": case["case_id"],
            "assignment_id": assignment_id,
            "mapping_commitment": commitment,
            "package_ref": _relative_path(package_path),
            "package_sha256": hashlib.sha256(package_text.encode("utf-8")).hexdigest(),
            "mapping_recorded": False,
        },
    )
    return {
        "event": event,
        "assignment_id": assignment_id,
        "package_path": str(package_path),
        "sealed_mapping_path": str(sealed_path),
    }


def record_blind_evaluation(
    config: Dict[str, Any],
    *,
    assignment_id: str,
    scores_a: Dict[str, Any],
    scores_b: Dict[str, Any],
    blinding_attested: bool,
    no_ledger_access_attested: bool,
    notes: str,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"quality_reviewer"})
    status = _require_study(config)
    case, assignment = _find_assignment(status, assignment_id)
    if assignment.get("evaluation"):
        raise ValueValidationPolicyError("blind evaluation is already recorded")
    analysts = {
        str((case.get("baseline") or {}).get("actor") or ""),
        str((case.get("assisted") or {}).get("actor") or ""),
        str(assignment.get("actor") or ""),
    }
    if actor in analysts:
        raise ValueValidationPolicyError("blind reviewer must be independent from analysts and preparer")
    package_path = project_path(str(assignment.get("package_ref") or ""))
    if not package_path.is_file() or file_hash(package_path) != assignment.get("package_sha256"):
        raise ValueValidationPolicyError("blind package is missing or its hash does not match")
    notes = str(notes or "").strip()[:1000]
    _reject_secret_text([notes])
    return _append_event(
        cfg,
        "blind_evaluation_recorded",
        actor,
        actor_role,
        {
            "case_id": case["case_id"],
            "assignment_id": assignment_id,
            "scores": {
                "A": _normalize_scores(scores_a),
                "B": _normalize_scores(scores_b),
            },
            "attestations": {
                "blinding_attested": bool(blinding_attested),
                "no_ledger_access_attested": bool(no_ledger_access_attested),
            },
            "notes": notes,
        },
    )


def reveal_blind_assignment(
    config: Dict[str, Any],
    *,
    assignment_id: str,
    actor: str,
    actor_role: str,
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, COORDINATION_ROLES)
    status = _require_study(config)
    case, assignment = _find_assignment(status, assignment_id)
    if not assignment.get("evaluation"):
        raise ValueValidationPolicyError("blind evaluation must be recorded before reveal")
    if assignment.get("reveal"):
        raise ValueValidationPolicyError("blind mapping is already revealed")
    sealed_path = project_path(cfg["sealed_dir"]) / "{0}.json".format(_path_token(assignment_id))
    try:
        sealed = json.loads(sealed_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueValidationPolicyError("sealed mapping is missing or invalid") from exc
    mapping = sealed.get("mapping", {}) if isinstance(sealed, dict) else {}
    nonce = str(sealed.get("nonce") or "") if isinstance(sealed, dict) else ""
    _validate_mapping(mapping)
    expected = object_hash(
        {
            "assignment_id": assignment_id,
            "case_id": case["case_id"],
            "mapping": mapping,
            "nonce": nonce,
        }
    )
    if expected != assignment.get("mapping_commitment") or expected != sealed.get("commitment"):
        raise ValueValidationPolicyError("sealed mapping commitment does not match")
    return _append_event(
        cfg,
        "blind_mapping_revealed",
        actor,
        actor_role,
        {
            "case_id": case["case_id"],
            "assignment_id": assignment_id,
            "mapping": mapping,
            "nonce": nonce,
            "mapping_commitment": expected,
            "revealed_after_evaluation": True,
        },
    )


def record_value_gate_decision(
    config: Dict[str, Any],
    *,
    decision: str,
    notes: str,
    evidence_ref: str,
    actor: str,
    actor_role: str,
    decision_deadline: str = "",
) -> Dict[str, Any]:
    cfg = _require_enabled(config)
    _authorize_human(config, actor, actor_role, {"research_supervisor"})
    status = _require_study(config)
    if status.get("decision"):
        raise ValueValidationPolicyError("value gate decision is already recorded")
    decision = str(decision or "").strip().upper()
    if decision not in GATE_DECISIONS:
        raise ValueValidationPolicyError("invalid value gate decision")
    if decision == "CONTINUE" and status.get("recommended_decision") != "CONTINUE":
        raise ValueValidationPolicyError("CONTINUE is blocked until every value gate passes")
    if decision in {"CONTINUE", "EXTEND_ONCE"} and status["operating_status"] not in {
        "READY_FOR_DECISION",
        "VALUE_GATE_FAILED",
        "BLIND_EVALUATION",
        "ASSISTED_RUNNING",
    }:
        raise ValueValidationPolicyError("study has insufficient evidence for this decision")
    deadline = ""
    if decision == "EXTEND_ONCE":
        deadline = _normalize_timestamp(decision_deadline, "decision_deadline")
    notes = _required_text(notes, "notes", 1000)
    evidence_ref = _validate_ref(evidence_ref, "evidence_ref", require_blind=False)
    _reject_secret_text([notes, evidence_ref])
    return _append_event(
        cfg,
        "gate_decision_recorded",
        actor,
        actor_role,
        {
            "decision": decision,
            "notes": notes,
            "evidence_ref": evidence_ref,
            "decision_deadline": deadline,
            "system_recommendation": status.get("recommended_decision", "INSUFFICIENT_EVIDENCE"),
            "automatic_activation": False,
        },
    )


def build_value_validation_status(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = value_validation_config(config)
    log = load_hash_chained_jsonl(project_path(cfg["event_path"]))
    state = _project_events(log.get("records", []), cfg)
    case_metrics = [
        _case_metrics(case, state.get("treatment", {}), cfg)
        for case in state["cases"].values()
        if case.get("in_frozen_cohort")
    ]
    aggregate = _aggregate_metrics(case_metrics)
    release = _load_release_audit(cfg)
    gates = _build_value_gates(cfg, log, state, case_metrics, aggregate, release)
    gate_pass = bool(gates) and all(item["status"] == "PASS" for item in gates)
    recommendation = _recommend_decision(gates, aggregate)
    operating = _operating_status(cfg, log, state, aggregate, gate_pass)
    next_actions = _next_actions(cfg, state, gates, aggregate, release)
    roster = load_operations_inputs(config)
    return {
        "schema_version": VALUE_VALIDATION_VERSION,
        "generated_at": now_iso(),
        "status": "PASS" if gate_pass else ("DISABLED" if not cfg["enabled"] else "BLOCKED"),
        "operating_status": operating,
        "study_id": cfg["study_id"],
        "integrity_status": log.get("integrity_status", "FAIL"),
        "event_count": log.get("record_count", 0),
        "invalid_event_count": log.get("invalid_record_count", 0),
        "semantic_errors": state["semantic_errors"],
        "study": state["study"],
        "cohort": state["cohort"],
        "treatment": state["treatment"],
        "cases": state["cases"],
        "case_metrics": case_metrics,
        "aggregate": aggregate,
        "gates": gates,
        "value_gate_ready": gate_pass,
        "recommended_decision": recommendation,
        "decision": state["decision"],
        "release_audit": {
            "status": str(release.get("status") or "MISSING"),
            "technical_release_ready": bool(release.get("technical_release_ready", False)),
            "manifest_id": str(release.get("manifest_id") or ""),
        },
        "roster": {
            "valid": bool(roster.get("roster_valid", False)),
            "active_human_count": sum(
                item.get("principal_type") == "human" and item.get("active")
                for item in roster.get("principals", [])
            ),
            "missing_human_capabilities": list(
                roster.get("missing_mandatory_human_capabilities", []) or []
            ),
        },
        "next_actions": next_actions,
        "policy": _policy_snapshot(cfg),
        "safety_boundary": SAFETY_BOUNDARY,
        "operation_contract": {
            "market_data_collection": False,
            "llm_call": False,
            "business_state_write": False,
            "automatic_case_selection": False,
            "automatic_human_identity": False,
            "automatic_gate_decision": False,
            "approval_or_activation": False,
            "scheduled_task_change": False,
            "publication": False,
            "trading_or_order_action": False,
        },
    }


def write_value_validation_status(config: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = build_value_validation_status(config)
    cfg = value_validation_config(config)
    generated = datetime.fromisoformat(snapshot["generated_at"])
    stamp = generated.strftime("%Y%m%dT%H%M%S%z") or generated.strftime("%Y%m%dT%H%M%S")
    directory = project_path(cfg["report_dir"])
    json_path = directory / "value_validation_{0}.json".format(stamp)
    md_path = directory / "value_validation_{0}.md".format(stamp)
    latest_json = directory / "latest.json"
    latest_md = directory / "latest.md"
    json_text = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = render_value_validation_markdown(snapshot)
    atomic_write_text(json_path, json_text)
    atomic_write_text(md_path, markdown, encoding="utf-8-sig")
    atomic_write_text(latest_json, json_text)
    atomic_write_text(latest_md, markdown, encoding="utf-8-sig")
    return {
        "snapshot": snapshot,
        "json_path": str(json_path),
        "report_path": str(md_path),
        "latest_json_path": str(latest_json),
        "latest_path": str(latest_md),
    }


def load_latest_value_validation(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = value_validation_config(config)
    path = project_path(cfg["report_dir"]) / "latest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def render_value_validation_markdown(snapshot: Dict[str, Any]) -> str:
    aggregate = snapshot.get("aggregate", {}) or {}
    cohort = snapshot.get("cohort", {}) or {}
    lines = [
        "# G0.5 Value Validation",
        "",
        "- Generated at: `{0}`".format(snapshot.get("generated_at", "")),
        "- Study: `{0}`".format(snapshot.get("study_id", "")),
        "- Operating status: `{0}`".format(snapshot.get("operating_status", "")),
        "- Event integrity: `{0}`".format(snapshot.get("integrity_status", "")),
        "- Frozen cases: `{0}`".format(cohort.get("case_count", 0)),
        "- Qualifying paired cases: `{0}`".format(aggregate.get("qualifying_case_count", 0)),
        "- System recommendation: `{0}`".format(snapshot.get("recommended_decision", "")),
        "- Human decision: `{0}`".format((snapshot.get("decision") or {}).get("decision", "NOT_RECORDED")),
        "",
        "The recommendation is not an approval and never changes production or trading state.",
        "",
        "## Value Gates",
        "",
        "| Gate | Status | Detail |",
        "|---|---|---|",
    ]
    for gate in snapshot.get("gates", []) or []:
        lines.append("| {0} | `{1}` | {2} |".format(
            _md(gate.get("gate", "")),
            _md(gate.get("status", "FAIL")),
            _md(gate.get("detail", "")),
        ))
    lines.extend([
        "",
        "## Paired Metrics",
        "",
        "- Baseline human minutes: `{0:.2f}`".format(float(aggregate.get("baseline_minutes", 0) or 0)),
        "- Assisted human minutes: `{0:.2f}`".format(float(aggregate.get("assisted_minutes", 0) or 0)),
        "- Weighted human-time improvement: `{0:.1%}`".format(float(aggregate.get("weighted_efficiency_improvement", 0) or 0)),
        "- Median paired improvement: `{0:.1%}`".format(float(aggregate.get("median_efficiency_improvement", 0) or 0)),
        "- Baseline quality: `{0:.2f}`".format(float(aggregate.get("baseline_quality_mean", 0) or 0)),
        "- Assisted quality: `{0:.2f}`".format(float(aggregate.get("assisted_quality_mean", 0) or 0)),
        "- Quality delta: `{0:.2f}`".format(float(aggregate.get("quality_delta", 0) or 0)),
        "- Estimated model cost CNY: `{0:.4f}`".format(float(aggregate.get("estimated_cost_cny", 0) or 0)),
        "- Unauthorized egress count: `{0}`".format(aggregate.get("unauthorized_egress_count", 0)),
        "",
        "## Case Progress",
        "",
        "| Case | Priority | Stratum | Baseline | Assisted | Blind review | Qualifying | Deviations |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ])
    for item in snapshot.get("case_metrics", []) or []:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} |".format(
            _md(item.get("case_id", "")),
            _md(item.get("priority", "")),
            _md(item.get("stratum", "")),
            "yes" if item.get("baseline_complete") else "no",
            "yes" if item.get("assisted_complete") else "no",
            "yes" if item.get("blind_complete") else "no",
            "yes" if item.get("qualifying") else "no",
            _md("; ".join(item.get("protocol_deviations", []) or []) or "none"),
        ))
    lines.extend(["", "## Next Actions", ""])
    lines.extend(
        ["- {0}".format(_md(item)) for item in snapshot.get("next_actions", []) or []]
        or ["- none"]
    )
    lines.extend(["", "## Safety Boundary", "", snapshot.get("safety_boundary", SAFETY_BOUNDARY), ""])
    return "\n".join(lines)


def _project_events(records: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "study": {},
        "cases": {},
        "cohort": {},
        "treatment": {},
        "assignments": {},
        "decision": {},
        "semantic_errors": [],
    }
    for event in records:
        if str(event.get("study_id") or "") != cfg["study_id"]:
            continue
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        if event_type not in EVENT_TYPES:
            state["semantic_errors"].append("unknown event type: {0}".format(event_type))
            continue
        if event_type != "study_opened" and not state["study"]:
            state["semantic_errors"].append("event before study_opened: {0}".format(event_type))
            continue
        if event_type == "study_opened":
            if state["study"]:
                state["semantic_errors"].append("duplicate study_opened")
                continue
            state["study"] = dict(payload)
            state["study"].update(_event_actor(event))
        elif event_type == "case_registered":
            case_id = str(payload.get("case_id") or "")
            if state["cohort"] or not case_id or case_id in state["cases"]:
                state["semantic_errors"].append("invalid case_registered: {0}".format(case_id))
                continue
            state["cases"][case_id] = dict(payload)
            state["cases"][case_id].update({"excluded": False, "in_frozen_cohort": False})
        elif event_type == "case_excluded":
            case_id = str(payload.get("case_id") or "")
            case = state["cases"].get(case_id)
            if not case or case.get("excluded") or case.get("baseline") or case.get("assisted"):
                state["semantic_errors"].append("invalid case_excluded: {0}".format(case_id))
                continue
            case["excluded"] = True
            case["exclusion"] = dict(payload)
            case["exclusion"].update(_event_actor(event))
        elif event_type == "cohort_frozen":
            if state["cohort"]:
                state["semantic_errors"].append("duplicate cohort_frozen")
                continue
            active = [item for item in state["cases"].values() if not item.get("excluded")]
            identity = _cohort_identity(active)
            expected_ids = [item["case_id"] for item in identity]
            if payload.get("case_ids") != expected_ids or payload.get("case_set_fingerprint") != object_hash(identity):
                state["semantic_errors"].append("cohort fingerprint mismatch")
                continue
            state["cohort"] = dict(payload)
            state["cohort"].update(_event_actor(event))
            for case_id in expected_ids:
                state["cases"][case_id]["in_frozen_cohort"] = True
        elif event_type == "baseline_recorded":
            case = state["cases"].get(str(payload.get("case_id") or ""))
            if not case or not case.get("in_frozen_cohort") or case.get("baseline") or state["treatment"]:
                state["semantic_errors"].append("invalid baseline_recorded")
                continue
            case["baseline"] = dict(payload)
            case["baseline"].update(_event_actor(event))
        elif event_type == "treatment_frozen":
            cohort_ids = state.get("cohort", {}).get("case_ids", []) or []
            if state["treatment"] or not cohort_ids or any(
                not state["cases"].get(case_id, {}).get("baseline") for case_id in cohort_ids
            ):
                state["semantic_errors"].append("invalid treatment_frozen")
                continue
            state["treatment"] = dict(payload)
            state["treatment"].update(_event_actor(event))
        elif event_type == "assisted_recorded":
            case = state["cases"].get(str(payload.get("case_id") or ""))
            if not case or not case.get("in_frozen_cohort") or not state["treatment"] or case.get("assisted"):
                state["semantic_errors"].append("invalid assisted_recorded")
                continue
            case["assisted"] = dict(payload)
            case["assisted"].update(_event_actor(event))
        elif event_type == "blind_assignment_prepared":
            case = state["cases"].get(str(payload.get("case_id") or ""))
            assignment_id = str(payload.get("assignment_id") or "")
            if not case or not case.get("baseline") or not case.get("assisted") or not assignment_id:
                state["semantic_errors"].append("invalid blind_assignment_prepared")
                continue
            if assignment_id in state["assignments"] or case.get("blind_assignment"):
                state["semantic_errors"].append("duplicate blind assignment")
                continue
            assignment = dict(payload)
            assignment.update(_event_actor(event))
            state["assignments"][assignment_id] = assignment
            case["blind_assignment"] = assignment
        elif event_type == "blind_evaluation_recorded":
            assignment = state["assignments"].get(str(payload.get("assignment_id") or ""))
            if not assignment or assignment.get("evaluation"):
                state["semantic_errors"].append("invalid blind_evaluation_recorded")
                continue
            assignment["evaluation"] = dict(payload)
            assignment["evaluation"].update(_event_actor(event))
        elif event_type == "blind_mapping_revealed":
            assignment = state["assignments"].get(str(payload.get("assignment_id") or ""))
            if not assignment or not assignment.get("evaluation") or assignment.get("reveal"):
                state["semantic_errors"].append("invalid blind_mapping_revealed")
                continue
            commitment = object_hash({
                "assignment_id": payload.get("assignment_id"),
                "case_id": payload.get("case_id"),
                "mapping": payload.get("mapping"),
                "nonce": payload.get("nonce"),
            })
            if commitment != assignment.get("mapping_commitment"):
                state["semantic_errors"].append("blind reveal commitment mismatch")
                continue
            assignment["reveal"] = dict(payload)
            assignment["reveal"].update(_event_actor(event))
        elif event_type == "gate_decision_recorded":
            if state["decision"]:
                state["semantic_errors"].append("duplicate gate_decision_recorded")
                continue
            state["decision"] = dict(payload)
            state["decision"].update(_event_actor(event))
    return state


def _case_metrics(case: Dict[str, Any], treatment: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    baseline = case.get("baseline", {}) or {}
    assisted = case.get("assisted", {}) or {}
    assignment = case.get("blind_assignment", {}) or {}
    evaluation = assignment.get("evaluation", {}) or {}
    reveal = assignment.get("reveal", {}) or {}
    deviations: List[str] = []
    baseline_obs = baseline.get("observation", {}) or {}
    assisted_obs = assisted.get("observation", {}) or {}
    if baseline and baseline_obs.get("measurement_method") not in cfg["qualifying_measurement_methods"]:
        deviations.append("baseline measurement method is not qualifying")
    if assisted and assisted_obs.get("measurement_method") not in cfg["qualifying_measurement_methods"]:
        deviations.append("assisted measurement method is not qualifying")
    if cfg["require_same_analyst"] and baseline and assisted and baseline.get("actor") != assisted.get("actor"):
        deviations.append("baseline and assisted analysts differ")
    if assisted and assisted.get("observed_manifest_id") != treatment.get("manifest_id"):
        deviations.append("assisted release manifest differs from frozen treatment")
    if baseline and assisted and (
        baseline.get("output_ref") == assisted.get("output_ref")
        or baseline.get("output_sha256") == assisted.get("output_sha256")
    ):
        deviations.append("baseline and assisted outputs are not distinct")
    attestations = assisted.get("attestations", {}) or {}
    if assisted and not attestations.get("outcome_hidden"):
        deviations.append("outcome label was not attested hidden")
    if assisted and not attestations.get("evidence_cutoff_respected"):
        deviations.append("evidence cutoff was not attested respected")
    unauthorized = int(assisted.get("unauthorized_egress_count", 0) or 0)
    if unauthorized > cfg["maximum_unauthorized_egress"]:
        deviations.append("unauthorized egress exceeds policy")
    eval_attest = evaluation.get("attestations", {}) or {}
    if evaluation and not eval_attest.get("blinding_attested"):
        deviations.append("reviewer did not attest blinding")
    if evaluation and not eval_attest.get("no_ledger_access_attested"):
        deviations.append("reviewer did not attest ledger isolation")
    package_ref = str(assignment.get("package_ref") or "")
    if assignment:
        package_path = project_path(package_ref)
        if not package_path.is_file() or file_hash(package_path) != assignment.get("package_sha256"):
            deviations.append("blind package integrity mismatch")
    baseline_scores: Dict[str, Any] = {}
    assisted_scores: Dict[str, Any] = {}
    if evaluation and reveal:
        mapping = reveal.get("mapping", {}) or {}
        scores = evaluation.get("scores", {}) or {}
        for label in ("A", "B"):
            if mapping.get(label) == "baseline":
                baseline_scores = scores.get(label, {}) or {}
            elif mapping.get(label) == "assisted":
                assisted_scores = scores.get(label, {}) or {}
    baseline_minutes = float(baseline_obs.get("active_minutes", 0) or 0)
    assisted_minutes = float(assisted_obs.get("active_minutes", 0) or 0)
    improvement = (
        (baseline_minutes - assisted_minutes) / baseline_minutes if baseline_minutes > 0 and assisted else 0.0
    )
    complete = bool(baseline and assisted and assignment and evaluation and reveal)
    return {
        "case_id": case.get("case_id", ""),
        "priority": case.get("priority", ""),
        "stratum": case.get("stratum", ""),
        "baseline_complete": bool(baseline),
        "assisted_complete": bool(assisted),
        "blind_complete": bool(assignment and evaluation and reveal),
        "complete": complete,
        "qualifying": bool(complete and not deviations),
        "baseline_minutes": baseline_minutes,
        "assisted_minutes": assisted_minutes,
        "efficiency_improvement": improvement,
        "baseline_quality": _quality_composite(baseline_scores),
        "assisted_quality": _quality_composite(assisted_scores),
        "baseline_defects": {field: int(baseline_scores.get(field, 0) or 0) for field in DEFECT_FIELDS},
        "assisted_defects": {field: int(assisted_scores.get(field, 0) or 0) for field in DEFECT_FIELDS},
        "estimated_cost_cny": float(assisted_obs.get("estimated_cost_cny", 0) or 0),
        "unauthorized_egress_count": unauthorized,
        "protocol_deviations": deviations,
    }


def _aggregate_metrics(case_metrics: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    qualifying = [item for item in case_metrics if item.get("qualifying")]
    baseline_minutes = sum(float(item.get("baseline_minutes", 0) or 0) for item in qualifying)
    assisted_minutes = sum(float(item.get("assisted_minutes", 0) or 0) for item in qualifying)
    improvements = [float(item.get("efficiency_improvement", 0) or 0) for item in qualifying]
    baseline_quality = [float(item.get("baseline_quality", 0) or 0) for item in qualifying]
    assisted_quality = [float(item.get("assisted_quality", 0) or 0) for item in qualifying]
    baseline_defects = {
        field: sum(int((item.get("baseline_defects") or {}).get(field, 0) or 0) for item in qualifying)
        for field in DEFECT_FIELDS
    }
    assisted_defects = {
        field: sum(int((item.get("assisted_defects") or {}).get(field, 0) or 0) for item in qualifying)
        for field in DEFECT_FIELDS
    }
    return {
        "cohort_case_count": len(case_metrics),
        "baseline_complete_count": sum(bool(item.get("baseline_complete")) for item in case_metrics),
        "assisted_complete_count": sum(bool(item.get("assisted_complete")) for item in case_metrics),
        "blind_complete_count": sum(bool(item.get("blind_complete")) for item in case_metrics),
        "qualifying_case_count": len(qualifying),
        "protocol_deviation_case_count": sum(bool(item.get("protocol_deviations")) for item in case_metrics),
        "baseline_minutes": baseline_minutes,
        "assisted_minutes": assisted_minutes,
        "weighted_efficiency_improvement": (
            (baseline_minutes - assisted_minutes) / baseline_minutes if baseline_minutes > 0 else 0.0
        ),
        "median_efficiency_improvement": median(improvements) if improvements else 0.0,
        "baseline_quality_mean": mean(baseline_quality) if baseline_quality else 0.0,
        "assisted_quality_mean": mean(assisted_quality) if assisted_quality else 0.0,
        "quality_delta": (
            mean(assisted_quality) - mean(baseline_quality)
            if baseline_quality and assisted_quality
            else 0.0
        ),
        "baseline_defects": baseline_defects,
        "assisted_defects": assisted_defects,
        "estimated_cost_cny": sum(float(item.get("estimated_cost_cny", 0) or 0) for item in qualifying),
        "unauthorized_egress_count": sum(
            int(item.get("unauthorized_egress_count", 0) or 0) for item in case_metrics
        ),
    }


def _build_value_gates(
    cfg: Dict[str, Any],
    log: Dict[str, Any],
    state: Dict[str, Any],
    case_metrics: Sequence[Dict[str, Any]],
    aggregate: Dict[str, Any],
    release: Dict[str, Any],
) -> List[Dict[str, str]]:
    gates: List[Dict[str, str]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        gates.append({"gate": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    add(
        "event_integrity",
        log.get("integrity_status") == "OK" and not state["semantic_errors"],
        "hash={0}; semantic_errors={1}".format(
            log.get("integrity_status", "FAIL"), len(state["semantic_errors"])
        ),
    )
    add("study_opened", bool(state["study"]), "study_id={0}".format(cfg["study_id"]))
    cohort_count = int((state.get("cohort") or {}).get("case_count", 0) or 0)
    add(
        "frozen_case_set",
        bool(state["cohort"]) and cfg["minimum_cases"] <= cohort_count <= cfg["maximum_cases"],
        "cases={0}; required={1}-{2}".format(
            cohort_count, cfg["minimum_cases"], cfg["maximum_cases"]
        ),
    )
    policy_match = bool(
        state["study"]
        and state["study"].get("policy_fingerprint") == object_hash(_policy_snapshot(cfg))
    )
    add("policy_frozen", policy_match, "current policy matches opened study={0}".format(policy_match))
    release_ready = bool(
        state["treatment"]
        and state["treatment"].get("release_ready_at_freeze")
        and _SAFE_SHA256.fullmatch(str(state["treatment"].get("manifest_id") or ""))
    )
    release_detail = "treatment not frozen"
    if state["treatment"]:
        release_detail = "manifest={0}".format(state["treatment"].get("manifest_id", ""))
    elif release:
        release_detail = "current G0={0}".format(release.get("status", "BLOCKED"))
    add("g0_treatment_freeze", release_ready, release_detail)
    qualifying = int(aggregate.get("qualifying_case_count", 0) or 0)
    complete = int(aggregate.get("blind_complete_count", 0) or 0)
    add(
        "paired_case_completeness",
        bool(cohort_count and complete == cohort_count and qualifying == cohort_count),
        "blind_complete={0}; qualifying={1}; cohort={2}".format(complete, qualifying, cohort_count),
    )
    blind_coverage = float(complete) / cohort_count if cohort_count else 0.0
    add(
        "blind_review_coverage",
        bool(cohort_count and blind_coverage >= cfg["minimum_blind_coverage"]),
        "coverage={0:.1%}; required={1:.1%}".format(blind_coverage, cfg["minimum_blind_coverage"]),
    )
    deviations = int(aggregate.get("protocol_deviation_case_count", 0) or 0)
    add("protocol_integrity", deviations == 0, "cases_with_deviation={0}".format(deviations))
    median_improvement = float(aggregate.get("median_efficiency_improvement", 0) or 0)
    add(
        "human_time_improvement",
        qualifying >= cfg["minimum_cases"]
        and median_improvement >= cfg["target_efficiency_improvement"],
        "median={0:.1%}; required={1:.1%}".format(
            median_improvement, cfg["target_efficiency_improvement"]
        ),
    )
    quality_delta = float(aggregate.get("quality_delta", 0) or 0)
    add(
        "quality_non_inferiority",
        qualifying >= cfg["minimum_cases"]
        and quality_delta >= -cfg["quality_non_inferiority_margin"],
        "delta={0:.2f}; lower_bound=-{1:.2f}".format(
            quality_delta, cfg["quality_non_inferiority_margin"]
        ),
    )
    baseline_defects = aggregate.get("baseline_defects", {}) or {}
    assisted_defects = aggregate.get("assisted_defects", {}) or {}
    worse = [
        field
        for field in DEFECT_FIELDS
        if int(assisted_defects.get(field, 0) or 0) > int(baseline_defects.get(field, 0) or 0)
    ]
    add(
        "material_defect_non_increase",
        qualifying >= cfg["minimum_cases"] and not worse,
        "worse={0}".format(",".join(worse) or "none"),
    )
    unauthorized = int(aggregate.get("unauthorized_egress_count", 0) or 0)
    add(
        "unauthorized_egress",
        unauthorized <= cfg["maximum_unauthorized_egress"],
        "observed={0}; maximum={1}".format(unauthorized, cfg["maximum_unauthorized_egress"]),
    )
    return gates


def _recommend_decision(gates: Sequence[Dict[str, str]], aggregate: Dict[str, Any]) -> str:
    if gates and all(item.get("status") == "PASS" for item in gates):
        return "CONTINUE"
    gate_map = {item.get("gate"): item.get("status") for item in gates}
    qualifying = int(aggregate.get("qualifying_case_count", 0) or 0)
    if int(aggregate.get("unauthorized_egress_count", 0) or 0) > 0:
        return "STOP"
    if qualifying > 0 and gate_map.get("material_defect_non_increase") == "FAIL":
        return "STOP"
    if qualifying > 0 and (
        gate_map.get("human_time_improvement") == "FAIL"
        or gate_map.get("quality_non_inferiority") == "FAIL"
    ):
        return "NARROW_OR_STOP"
    return "INSUFFICIENT_EVIDENCE"


def _operating_status(
    cfg: Dict[str, Any],
    log: Dict[str, Any],
    state: Dict[str, Any],
    aggregate: Dict[str, Any],
    gate_pass: bool,
) -> str:
    if not cfg["enabled"]:
        return "DISABLED"
    if log.get("integrity_status") != "OK" or state["semantic_errors"]:
        return "INVALID"
    if not state["study"]:
        return "NOT_STARTED"
    if state["decision"]:
        return "DECIDED_{0}".format(state["decision"].get("decision", "UNKNOWN"))
    if not state["cohort"]:
        return "COHORT_BUILDING"
    if not state["treatment"]:
        return "BASELINE_COLLECTION"
    cohort_count = int((state["cohort"] or {}).get("case_count", 0) or 0)
    if int(aggregate.get("assisted_complete_count", 0) or 0) < cohort_count:
        return "ASSISTED_RUNNING"
    if int(aggregate.get("blind_complete_count", 0) or 0) < cohort_count:
        return "BLIND_EVALUATION"
    return "READY_FOR_DECISION" if gate_pass else "VALUE_GATE_FAILED"


def _next_actions(
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    gates: Sequence[Dict[str, str]],
    aggregate: Dict[str, Any],
    release: Dict[str, Any],
) -> List[str]:
    if not cfg["enabled"]:
        return ["Enable value_validation only after scope and real human owners are approved."]
    if not state["study"]:
        return [
            "Register real active human coordinator, supervisor, analyst, and independent quality reviewer.",
            "Open the study with one named scenario; do not use synthetic cases as business evidence.",
        ]
    if not state["cohort"]:
        active = sum(not item.get("excluded") for item in state["cases"].values())
        return [
            "Register and review {0} more real cases before freezing the cohort.".format(
                max(0, cfg["minimum_cases"] - active)
            )
        ]
    cohort_count = int(state["cohort"].get("case_count", 0) or 0)
    baseline_count = int(aggregate.get("baseline_complete_count", 0) or 0)
    if baseline_count < cohort_count:
        return ["Record time-log baselines for {0} frozen cases.".format(cohort_count - baseline_count)]
    if not state["treatment"]:
        if not release.get("technical_release_ready"):
            return ["Resolve G0 release blockers before freezing the assisted treatment."]
        return ["Freeze the exact release manifest, Pi method bundle, model, prompt, and policy."]
    assisted = int(aggregate.get("assisted_complete_count", 0) or 0)
    if assisted < cohort_count:
        return ["Record governed assisted observations for {0} cases.".format(cohort_count - assisted)]
    blind = int(aggregate.get("blind_complete_count", 0) or 0)
    if blind < cohort_count:
        return ["Complete independent blinded scoring and reveal for {0} cases.".format(cohort_count - blind)]
    if state["decision"]:
        return ["Preserve the decision pack; this module does not activate production."]
    failed = [item["gate"] for item in gates if item.get("status") != "PASS"]
    if failed:
        return ["Human steering decision required; failed gates: {0}.".format(", ".join(failed))]
    return ["Authorized research supervisor may record CONTINUE, NARROW, or STOP with evidence."]


def _normalize_observation(raw: Dict[str, Any], assisted: bool = False) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueValidationPolicyError("metrics must be a JSON object")
    if raw.get("template_only") is True:
        raise ValueValidationPolicyError(
            "metrics template cannot be submitted; copy it and remove template_only after recording real evidence"
        )
    started = _parse_timestamp(raw.get("started_at"), "started_at")
    completed = _parse_timestamp(raw.get("completed_at"), "completed_at")
    if completed <= started:
        raise ValueValidationPolicyError("completed_at must be after started_at")
    elapsed = (completed - started).total_seconds() / 60.0
    active = _positive_float(raw.get("active_minutes"), "active_minutes")
    if active > elapsed + 1.0:
        raise ValueValidationPolicyError("active_minutes cannot exceed elapsed time")
    segments_raw = raw.get("segment_minutes", {})
    if not isinstance(segments_raw, dict):
        raise ValueValidationPolicyError("segment_minutes must be an object")
    segments = {
        field: _nonnegative_float(segments_raw.get(field, 0), "segment_minutes.{0}".format(field))
        for field in SEGMENT_FIELDS
    }
    segment_total = sum(segments.values())
    tolerance = max(1.0, active * 0.05)
    if abs(segment_total - active) > tolerance:
        raise ValueValidationPolicyError("segment minutes must reconcile to active_minutes")
    method = str(raw.get("measurement_method") or "").strip().lower()
    if method not in {"time_log", "calendar_reconstruction"}:
        raise ValueValidationPolicyError(
            "measurement_method must be time_log or calendar_reconstruction"
        )
    value = {
        "started_at": started.isoformat(timespec="seconds"),
        "completed_at": completed.isoformat(timespec="seconds"),
        "elapsed_minutes": round(elapsed, 4),
        "active_minutes": round(active, 4),
        "segment_minutes": {key: round(number, 4) for key, number in segments.items()},
        "measurement_method": method,
        "tool_switch_count": _nonnegative_int(raw.get("tool_switch_count", 0), "tool_switch_count"),
        "source_count": _nonnegative_int(raw.get("source_count", 0), "source_count"),
        "citation_count": _nonnegative_int(raw.get("citation_count", 0), "citation_count"),
        "measurement_evidence_ref": _validate_ref(
            raw.get("measurement_evidence_ref", ""),
            "measurement_evidence_ref",
            require_blind=False,
        ),
        "measurement_evidence_sha256": _validate_sha256(
            raw.get("measurement_evidence_sha256", ""),
            "measurement_evidence_sha256",
        ),
    }
    if assisted:
        value.update({
            "system_duration_seconds": _nonnegative_float(
                raw.get("system_duration_seconds", 0), "system_duration_seconds"
            ),
            "prompt_tokens": _nonnegative_int(raw.get("prompt_tokens", 0), "prompt_tokens"),
            "completion_tokens": _nonnegative_int(
                raw.get("completion_tokens", 0), "completion_tokens"
            ),
            "reasoning_tokens": _nonnegative_int(
                raw.get("reasoning_tokens", 0), "reasoning_tokens"
            ),
            "estimated_cost_cny": _nonnegative_float(
                raw.get("estimated_cost_cny", 0), "estimated_cost_cny"
            ),
            "system_run_ref": _validate_ref(
                raw.get("system_run_ref", ""), "system_run_ref", require_blind=False
            ),
            "system_run_sha256": _validate_sha256(
                raw.get("system_run_sha256", ""), "system_run_sha256"
            ),
        })
    return value


def _normalize_scores(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueValidationPolicyError("blind scores must be a JSON object")
    if raw.get("template_only") is True:
        raise ValueValidationPolicyError(
            "score template cannot be submitted; copy it and remove template_only after independent review"
        )
    value: Dict[str, Any] = {}
    for field in QUALITY_DIMENSIONS:
        try:
            number = float(raw.get(field, -1))
        except (TypeError, ValueError) as exc:
            raise ValueValidationPolicyError(
                "{0} must be numeric between 0 and 100".format(field)
            ) from exc
        if number < 0 or number > 100:
            raise ValueValidationPolicyError("{0} must be between 0 and 100".format(field))
        value[field] = round(number, 4)
    for field in DEFECT_FIELDS:
        value[field] = _nonnegative_int(raw.get(field, 0), field)
    value["composite_quality"] = _quality_composite(value)
    return value


def _quality_composite(scores: Dict[str, Any]) -> float:
    if not scores or any(field not in scores for field in QUALITY_DIMENSIONS):
        return 0.0
    return round(mean(float(scores[field]) for field in QUALITY_DIMENSIONS), 4)


def _require_enabled(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = value_validation_config(config)
    if not cfg["enabled"]:
        raise ValueValidationPolicyError("value_validation is disabled")
    if not _SAFE_ID.fullmatch(cfg["study_id"]):
        raise ValueValidationPolicyError("value_validation.study_id is invalid")
    return cfg


def _require_study(config: Dict[str, Any]) -> Dict[str, Any]:
    status = build_value_validation_status(config)
    if status["integrity_status"] != "OK" or status["semantic_errors"]:
        raise ValueValidationPolicyError("value validation ledger is invalid")
    if not status["study"]:
        raise ValueValidationPolicyError("value study is not opened")
    return status


def _require_case(status: Dict[str, Any], case_id: str) -> Dict[str, Any]:
    value = status["cases"].get(str(case_id or "").strip())
    if not value:
        raise ValueValidationPolicyError("unknown value case: {0}".format(case_id))
    return value


def _require_frozen_case(status: Dict[str, Any], case_id: str) -> Dict[str, Any]:
    case = _require_case(status, case_id)
    if not case.get("in_frozen_cohort"):
        raise ValueValidationPolicyError("case is not in the frozen cohort")
    return case


def _find_assignment(
    status: Dict[str, Any], assignment_id: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    assignment_id = str(assignment_id or "").strip()
    for case in status["cases"].values():
        assignment = case.get("blind_assignment", {}) or {}
        if assignment.get("assignment_id") == assignment_id:
            return case, assignment
    raise ValueValidationPolicyError("unknown blind assignment")


def _append_event(
    cfg: Dict[str, Any],
    event_type: str,
    actor: str,
    actor_role: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueValidationPolicyError("unsupported value validation event")
    log_path = project_path(cfg["event_path"])
    current = load_hash_chained_jsonl(log_path)
    if current["integrity_status"] != "OK":
        raise ValueValidationPolicyError("value validation event ledger integrity failed")
    return append_hash_chained_jsonl(
        log_path,
        {
            "schema_version": VALUE_VALIDATION_VERSION,
            "study_id": cfg["study_id"],
            "event_type": event_type,
            "recorded_at": now_iso(),
            "actor": str(actor or "").strip(),
            "actor_role": str(actor_role or "").strip(),
            "payload": payload,
            "automatic_approval": False,
            "automatic_activation": False,
        },
    )


def _authorize_human(
    config: Dict[str, Any], actor: str, actor_role: str, allowed_roles: Iterable[str]
) -> Dict[str, Any]:
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    if actor_role not in set(allowed_roles):
        raise ValueValidationPolicyError("actor_role is not allowed for this value event")
    inputs = load_operations_inputs(config)
    if inputs.get("errors"):
        raise ValueValidationPolicyError(
            "research roster is invalid: {0}".format("; ".join(inputs["errors"][:5]))
        )
    principal = inputs.get("principal_map", {}).get(actor)
    if not principal:
        raise ValueValidationPolicyError("actor is not registered in the research roster")
    if principal.get("principal_type") != "human" or not principal.get("active"):
        raise ValueValidationPolicyError("actor must be an active human principal")
    if actor_role not in (principal.get("roles") or []):
        raise ValueValidationPolicyError("actor is not registered for role {0}".format(actor_role))
    if actor_role in {"lead_analyst", "quality_reviewer"}:
        registration = (principal.get("qualifications") or {}).get(
            "securities_analyst_registration", {}
        )
        if not (
            isinstance(registration, dict)
            and str(registration.get("status") or "").lower() == "verified"
            and str(registration.get("registration_id") or "").strip()
        ):
            raise ValueValidationPolicyError("analyst registration is not verified")
    if actor_role == "research_supervisor" and "publication_authorized" not in (
        principal.get("authorizations") or []
    ):
        raise ValueValidationPolicyError("research supervisor authorization is missing")
    if actor_role in {
        "research_coordinator",
        "research_supervisor",
        "lead_analyst",
        "quality_reviewer",
    } and principal.get("information_side") != "public":
        raise ValueValidationPolicyError("value validation actor must be on the public information side")
    return principal


def _policy_snapshot(cfg: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "minimum_cases",
        "maximum_cases",
        "minimum_strata",
        "target_efficiency_improvement",
        "quality_non_inferiority_margin",
        "minimum_blind_coverage",
        "maximum_unauthorized_egress",
        "require_same_analyst",
        "allowed_priorities",
        "allowed_data_classifications",
        "qualifying_measurement_methods",
    )
    return {key: cfg[key] for key in keys}


def _cohort_identity(cases: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        [
            {
                "case_id": item.get("case_id", ""),
                "case_ref": item.get("case_ref", ""),
                "case_sha256": item.get("case_sha256", ""),
                "priority": item.get("priority", ""),
                "stratum": item.get("stratum", ""),
                "evidence_cutoff": item.get("evidence_cutoff", ""),
                "data_classification": item.get("data_classification", ""),
            }
            for item in cases
        ],
        key=lambda item: str(item["case_id"]),
    )


def _load_release_audit(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = project_path(cfg["release_audit_path"])
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_mapping(mapping: Dict[str, str]) -> None:
    if not isinstance(mapping, dict) or set(mapping.keys()) != {"A", "B"}:
        raise ValueValidationPolicyError("blind mapping must contain A and B")
    if {str(mapping.get("A")), str(mapping.get("B"))} != {"baseline", "assisted"}:
        raise ValueValidationPolicyError("blind mapping must map A/B to baseline/assisted")


def _validate_ref(value: str, field: str, *, require_blind: bool) -> str:
    text = str(value or "").strip()
    if not _SAFE_REF.fullmatch(text):
        raise ValueValidationPolicyError(
            "{0} must be an opaque archive:, vault:, case:, or report: reference".format(field)
        )
    if require_blind and any(term in text.lower() for term in _NON_BLIND_REF_TERMS):
        raise ValueValidationPolicyError("{0} reveals the workflow variant".format(field))
    _reject_secret_text([text])
    return text


def _validate_sha256(value: str, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SAFE_SHA256.fullmatch(text):
        raise ValueValidationPolicyError("{0} must be a SHA-256 hex digest".format(field))
    return text


def _validate_id(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueValidationPolicyError("{0} is invalid".format(field))
    return text


def _required_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueValidationPolicyError("{0} is required".format(field))
    if len(text) > limit:
        raise ValueValidationPolicyError("{0} exceeds {1} characters".format(field, limit))
    return text


def _reject_secret_text(values: Iterable[Any]) -> None:
    if any(_SECRET_PATTERN.search(str(value or "")) for value in values):
        raise ValueValidationPolicyError("secret-like value is not allowed in value validation records")


def _parse_timestamp(value: Any, field: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueValidationPolicyError("{0} must be an ISO-8601 timestamp".format(field)) from exc
    if parsed.tzinfo is None:
        raise ValueValidationPolicyError("{0} must include a timezone".format(field))
    return parsed


def _normalize_timestamp(value: Any, field: str) -> str:
    return _parse_timestamp(value, field).isoformat(timespec="seconds")


def _positive_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueValidationPolicyError("{0} must be numeric".format(field)) from exc
    if number <= 0:
        raise ValueValidationPolicyError("{0} must be positive".format(field))
    return number


def _nonnegative_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueValidationPolicyError("{0} must be numeric".format(field)) from exc
    if number < 0:
        raise ValueValidationPolicyError("{0} must be non-negative".format(field))
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueValidationPolicyError("{0} must be an integer".format(field)) from exc
    if number < 0:
        raise ValueValidationPolicyError("{0} must be non-negative".format(field))
    return number


def _event_actor(event: Dict[str, Any]) -> Dict[str, str]:
    return {
        "actor": str(event.get("actor") or ""),
        "actor_role": str(event.get("actor_role") or ""),
        "recorded_at": str(event.get("recorded_at") or ""),
        "event_id": str(event.get("event_id") or ""),
    }


def _relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(project_path(".").resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _path_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:180]


def _clean_list(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
