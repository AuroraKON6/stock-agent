from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import project_path
from .models import now_iso


POLICY_VERSION = "20.0"
SAFETY_BOUNDARY = "Internal research only. No automatic trading, external publication, or protected-gate approval."

SERVICE_ALLOWED_ROLES = {
    "information_analyst",
    "fundamental_analyst",
    "market_analyst",
    "risk_reviewer",
}
HUMAN_ONLY_ROLES = {
    "research_coordinator",
    "lead_analyst",
    "quality_reviewer",
    "compliance_officer",
    "publication_approver",
    "research_supervisor",
}
PUBLIC_RESEARCH_ROLES = {
    "research_coordinator",
    "lead_analyst",
    "quality_reviewer",
    "information_analyst",
    "fundamental_analyst",
    "market_analyst",
    "risk_reviewer",
}
PROTECTED_ROLES = {
    "quality_reviewer",
    "compliance_officer",
    "publication_approver",
    "research_supervisor",
}
MANDATORY_HUMAN_CAPABILITIES = {
    "research_coordinator",
    "lead_analyst",
    "quality_reviewer",
    "compliance_officer",
    "publication_approver",
}

ASSIGNMENT_ROLE_BY_ACTION = {
    "assign_lead": "lead_analyst",
    "assign_quality_reviewer": "quality_reviewer",
    "assign_compliance_officer": "compliance_officer",
    "assign_approver": "publication_approver",
}
PROTECTED_ROLE_BY_ACTION = {
    "quality_approve": "quality_reviewer",
    "quality_reject": "quality_reviewer",
    "compliance_clear": "compliance_officer",
    "compliance_block": "compliance_officer",
    "publication_approve": "research_supervisor",
    "publication_reject": "research_supervisor",
    "start_monitoring": "research_supervisor",
    "close_case": "research_supervisor",
    "reopen_case": "research_supervisor",
}

DEFAULT_STAGE_SLA_HOURS = {
    "P0": {
        "intake": 0.5,
        "triage": 0.5,
        "evidence_collection": 2,
        "analysis": 4,
        "challenge": 2,
        "quality_review": 2,
        "compliance_review": 2,
        "human_approval": 2,
    },
    "P1": {
        "intake": 2,
        "triage": 2,
        "evidence_collection": 8,
        "analysis": 16,
        "challenge": 8,
        "quality_review": 8,
        "compliance_review": 8,
        "human_approval": 8,
    },
    "P2": {
        "intake": 8,
        "triage": 8,
        "evidence_collection": 24,
        "analysis": 32,
        "challenge": 16,
        "quality_review": 16,
        "compliance_review": 16,
        "human_approval": 16,
    },
    "P3": {
        "intake": 16,
        "triage": 16,
        "evidence_collection": 40,
        "analysis": 56,
        "challenge": 24,
        "quality_review": 24,
        "compliance_review": 24,
        "human_approval": 24,
    },
}


class ResearchOperationsPolicyError(ValueError):
    pass


def operations_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("research_operations", {}) if isinstance(config, dict) else {}
    calendar = raw.get("business_calendar", {}) if isinstance(raw.get("business_calendar"), dict) else {}
    sla = raw.get("stage_sla_hours", {}) if isinstance(raw.get("stage_sla_hours"), dict) else {}
    merged_sla: Dict[str, Dict[str, float]] = {}
    for priority, defaults in DEFAULT_STAGE_SLA_HOURS.items():
        supplied = sla.get(priority, {}) if isinstance(sla.get(priority), dict) else {}
        merged_sla[priority] = {
            stage: max(0.25, float(supplied.get(stage, value) or value))
            for stage, value in defaults.items()
        }
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforce_roster": bool(raw.get("enforce_roster", False)),
        "assignment_mode": "suggest_only",
        "roster_path": str(raw.get("roster_path") or "config/research_roster.example.json"),
        "restrictions_path": str(raw.get("restrictions_path") or "config/research_restrictions.example.json"),
        "require_recorded_challenge": bool(raw.get("require_recorded_challenge", False)),
        "timezone": str(raw.get("timezone") or "Asia/Shanghai"),
        "weekdays": _int_list(calendar.get("weekdays", [0, 1, 2, 3, 4]), minimum=0, maximum=6),
        "workday_start": str(calendar.get("workday_start") or "08:30"),
        "workday_end": str(calendar.get("workday_end") or "18:00"),
        "holidays": _clean_list(calendar.get("holidays", [])),
        "at_risk_ratio": min(0.95, max(0.5, float(raw.get("at_risk_ratio", 0.8) or 0.8))),
        "stage_sla_hours": merged_sla,
    }


def load_operations_inputs(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = operations_config(config)
    roster_raw, roster_errors = _load_json_document(cfg["roster_path"], "principals")
    restriction_raw, restriction_errors = _load_json_document(cfg["restrictions_path"], "entries")
    principals, principal_errors = _normalize_principals(roster_raw.get("principals", []))
    restrictions, restriction_validation_errors = _normalize_restrictions(restriction_raw.get("entries", []))
    errors = roster_errors + restriction_errors + principal_errors + restriction_validation_errors
    capabilities = _capability_status(principals)
    missing = sorted(role for role, available in capabilities.items() if not available)
    return {
        "config": cfg,
        "principals": principals,
        "principal_map": {item["actor_id"]: item for item in principals},
        "restrictions": restrictions,
        "errors": errors,
        "capabilities": capabilities,
        "missing_mandatory_human_capabilities": missing,
        "roster_valid": not errors,
        "roster_path": str(project_path(cfg["roster_path"])),
        "restrictions_path": str(project_path(cfg["restrictions_path"])),
    }


def authorize_runtime_action(
    config: Dict[str, Any],
    case: Dict[str, Any],
    action: str,
    actor: str,
    actor_role: str,
    *,
    assignee: str = "",
    task_id: str = "",
    runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    inputs = load_operations_inputs(config)
    cfg = inputs["config"]
    decision = {
        "policy_version": POLICY_VERSION,
        "enforced": bool(cfg["enabled"] and cfg["enforce_roster"]),
        "allowed": True,
        "actor_id": str(actor or "").strip(),
        "actor_role": str(actor_role or "").strip(),
        "principal_type": "legacy",
        "reasons": [],
        "evaluated_at": now_iso(),
    }
    if not decision["enforced"]:
        return decision
    if inputs["errors"]:
        _deny(decision, "research operations roster is invalid: {0}".format("; ".join(inputs["errors"][:5])))
        return decision
    principal = inputs["principal_map"].get(decision["actor_id"])
    if principal is None:
        _deny(decision, "actor is not registered in the research operations roster")
        return decision
    decision["principal_type"] = principal["principal_type"]

    entity = str(case.get("entity") or "unknown")
    restriction = active_entity_restriction(entity, inputs["restrictions"])
    if restriction.get("list_type") == "restricted" and action not in {"compliance_block", "close_case"}:
        _deny(decision, "entity is on the active restricted list")

    expected_actor_roles = _expected_actor_roles(action, case, task_id)
    if actor_role not in expected_actor_roles:
        _deny(decision, "action {0} requires actor role {1}".format(action, " or ".join(sorted(expected_actor_roles))))
    loads = _active_loads(runtime or {"cases": [case]})
    actor_reasons = _principal_ineligibility(
        principal,
        actor_role,
        case,
        restriction,
        loads,
        enforce_capacity=False,
    )
    if actor_reasons:
        _deny(decision, "actor is not eligible: {0}".format("; ".join(actor_reasons)))

    if action in ASSIGNMENT_ROLE_BY_ACTION or action == "assign_workstream":
        if principal["principal_type"] != "human" or actor_role not in {"research_coordinator", "research_supervisor"}:
            _deny(decision, "only an authorized human coordinator or supervisor may assign work")
        target_role = ASSIGNMENT_ROLE_BY_ACTION.get(action) or _task_owner_role(case, task_id)
        target = inputs["principal_map"].get(str(assignee or "").strip())
        if target is None:
            _deny(decision, "assignee is not registered in the research operations roster")
            return decision
        target_reasons = _principal_ineligibility(target, target_role, case, restriction, loads, enforce_capacity=True)
        if target_reasons:
            _deny(decision, "assignee is not eligible: {0}".format("; ".join(target_reasons)))

    if action == "complete_task":
        expected_assignee = _task_assignee(case, task_id)
        if not expected_assignee:
            _deny(decision, "workstream must be assigned before completion")
        if expected_assignee != actor:
            _deny(decision, "only the assigned workstream principal may complete the task")
    elif action in {"attach_workpaper", "request_evidence", "resolve_evidence_request"}:
        if not _actor_assigned_to_case(case, actor, actor_role):
            _deny(decision, "actor must be assigned to this case or one of its workstreams")

    if action in PROTECTED_ROLE_BY_ACTION and principal["principal_type"] != "human":
        _deny(decision, "service principals cannot pass protected human gates")
    return decision


def validate_runtime_action(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    decision = authorize_runtime_action(*args, **kwargs)
    if not decision["allowed"]:
        raise ResearchOperationsPolicyError("Research operations policy denied action: {0}".format("; ".join(decision["reasons"])))
    return decision


def build_research_operations_snapshot(
    config: Dict[str, Any],
    runtime: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    write: bool = True,
) -> Dict[str, Any]:
    if runtime is None:
        from .research_runtime import load_research_runtime

        runtime = load_research_runtime()
    inputs = load_operations_inputs(config)
    cfg = inputs["config"]
    current = _coerce_now(now, cfg["timezone"])
    active_loads = _active_loads(runtime)
    rows = []
    for case in runtime.get("cases", []) or []:
        if not isinstance(case, dict):
            continue
        rows.append(_case_operations(case, inputs, active_loads, current))
    rows.sort(key=_operations_sort_key)
    escalation_counts = Counter(item["escalation_level"] for item in rows)
    summary = {
        "case_count": len(rows),
        "unstaffed_count": sum(bool(item["staffing_gaps"]) for item in rows),
        "unstaffed_high_priority_count": sum(
            bool(item["staffing_gaps"]) and item["priority"] in {"P0", "P1"} for item in rows
        ),
        "at_risk_count": sum(item["sla_status"] == "at_risk" for item in rows),
        "overdue_count": sum(item["sla_status"] == "overdue" for item in rows),
        "restricted_count": sum(item["access_classification"] == "restricted" for item in rows),
        "observation_count": sum(item["access_classification"] == "observation" for item in rows),
        "escalation_counts": dict(sorted(escalation_counts.items())),
        "missing_mandatory_human_capabilities": inputs["missing_mandatory_human_capabilities"],
        "roster_error_count": len(inputs["errors"]),
    }
    snapshot = {
        "policy_version": POLICY_VERSION,
        "generated_at": current.isoformat(),
        "enabled": cfg["enabled"],
        "enforce_roster": cfg["enforce_roster"],
        "assignment_mode": "suggest_only",
        "sla_policy_type": "internal_operating_policy_not_regulatory_deadline",
        "roster": {
            "path": inputs["roster_path"],
            "principal_count": len(inputs["principals"]),
            "human_count": sum(item["principal_type"] == "human" for item in inputs["principals"]),
            "service_count": sum(item["principal_type"] == "service" for item in inputs["principals"]),
            "valid": inputs["roster_valid"],
            "errors": inputs["errors"],
            "capabilities": inputs["capabilities"],
            "missing_mandatory_human_capabilities": inputs["missing_mandatory_human_capabilities"],
        },
        "restrictions_path": inputs["restrictions_path"],
        "active_loads": active_loads,
        "summary": summary,
        "cases": rows,
        "safety_boundary": SAFETY_BOUNDARY,
    }
    if write:
        snapshot.update(write_research_operations_snapshot(snapshot))
    return snapshot


def write_research_operations_snapshot(snapshot: Dict[str, Any]) -> Dict[str, str]:
    data_path = project_path("data/research_operations/latest.json")
    report_json = project_path("reports/research_operations/latest.json")
    report_md = project_path("reports/research_operations/latest.md")
    stamp = str(snapshot.get("generated_at") or now_iso()).replace("-", "").replace(":", "")
    stamp = stamp.replace("+", "_").replace(".", "")
    history_json = project_path("reports/research_operations/operations_{0}.json".format(stamp))
    history_md = project_path("reports/research_operations/operations_{0}.md".format(stamp))
    for path in (data_path, report_json, report_md, history_json, history_md):
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    markdown = _render_markdown(snapshot)
    _atomic_write(data_path, payload)
    _atomic_write(report_json, payload)
    _atomic_write(report_md, markdown)
    _atomic_write(history_json, payload)
    _atomic_write(history_md, markdown)
    return {
        "research_operations_data_path": str(data_path),
        "research_operations_json_path": str(report_json),
        "research_operations_report_path": str(report_md),
        "research_operations_history_json_path": str(history_json),
        "research_operations_history_report_path": str(history_md),
    }


def active_entity_restriction(entity: str, restrictions: Sequence[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    target = str(entity or "").strip().lower()
    current = now or datetime.now().astimezone()
    active = []
    for item in restrictions:
        if str(item.get("entity") or "").strip().lower() != target:
            continue
        if str(item.get("status") or "active") != "active":
            continue
        start = _parse_datetime(item.get("effective_from"))
        end = _parse_datetime(item.get("effective_to"))
        if start and _compare_aware(current, start) < 0:
            continue
        if end and _compare_aware(current, end) > 0:
            continue
        active.append(item)
    active.sort(key=lambda item: 0 if item.get("list_type") == "restricted" else 1)
    return active[0] if active else {}


def add_business_hours(start: datetime, hours: float, cfg: Dict[str, Any]) -> datetime:
    remaining = max(0.0, float(hours)) * 3600.0
    current = _next_business_instant(start, cfg)
    day_start, day_end = _business_times(cfg)
    while remaining > 0:
        end = current.replace(hour=day_end.hour, minute=day_end.minute, second=0, microsecond=0)
        available = max(0.0, (end - current).total_seconds())
        if remaining <= available:
            return current + timedelta(seconds=remaining)
        remaining -= available
        current = _next_business_instant(
            (current + timedelta(days=1)).replace(hour=day_start.hour, minute=day_start.minute, second=0, microsecond=0),
            cfg,
        )
    return current


def business_hours_elapsed(start: datetime, end: datetime, cfg: Dict[str, Any]) -> float:
    if end <= start:
        return 0.0
    cursor = _next_business_instant(start, cfg)
    if cursor >= end:
        return 0.0
    total = 0.0
    _, day_end = _business_times(cfg)
    while cursor < end:
        close = cursor.replace(hour=day_end.hour, minute=day_end.minute, second=0, microsecond=0)
        segment_end = min(end, close)
        if segment_end > cursor:
            total += (segment_end - cursor).total_seconds()
        cursor = _next_business_instant(
            (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), cfg
        )
    return total / 3600.0


def _case_operations(
    case: Dict[str, Any],
    inputs: Dict[str, Any],
    active_loads: Dict[str, int],
    current: datetime,
) -> Dict[str, Any]:
    cfg = inputs["config"]
    priority = str(case.get("top_priority") or "P3")
    stage = str(case.get("stage") or "unknown")
    entity = str(case.get("entity") or "unknown")
    restriction = active_entity_restriction(entity, inputs["restrictions"], current)
    access = str(restriction.get("list_type") or "public")
    next_task = next(
        (item for item in case.get("workstreams", []) or [] if item.get("status") == "ready"),
        None,
    )
    required_role = str((next_task or {}).get("owner_role") or _stage_owner_role(stage))
    next_task_id = str((next_task or {}).get("task_id") or "")
    eligible_by_role = {}
    candidate_roles = {
        "research_coordinator",
        "research_supervisor",
        "lead_analyst",
        "information_analyst",
        "fundamental_analyst",
        "market_analyst",
        "risk_reviewer",
        "quality_reviewer",
        "compliance_officer",
        "publication_approver",
    }
    if required_role:
        candidate_roles.add(required_role)
    for role in sorted(candidate_roles):
        role_eligible = []
        for principal in inputs["principals"]:
            reasons = _principal_ineligibility(
                principal, role, case, restriction, active_loads, enforce_capacity=True
            )
            if not reasons:
                role_eligible.append(principal["actor_id"])
        role_eligible.sort(key=lambda actor_id: (active_loads.get(actor_id, 0), actor_id))
        eligible_by_role[role] = role_eligible
    eligible = eligible_by_role.get(required_role, [])

    assignments = case.get("assignments", {}) or {}
    task_assignments = case.get("task_assignments", {}) or {}
    staffing_gaps = []
    if stage not in {"closed", "monitoring"} and not assignments.get("lead_analyst"):
        staffing_gaps.append("lead_analyst")
    if next_task_id and not task_assignments.get(next_task_id) and not (next_task or {}).get("assignee"):
        staffing_gaps.append(required_role or next_task_id)

    stage_started = _parse_datetime(case.get("stage_entered_at") or case.get("updated_at") or case.get("created_at"))
    if stage_started is None:
        stage_started = current
    stage_started = _align_timezone(stage_started, current)
    target_hours = float((cfg["stage_sla_hours"].get(priority, {}) or {}).get(stage, 0) or 0)
    due_at = add_business_hours(stage_started, target_hours, cfg) if target_hours else None
    elapsed = business_hours_elapsed(stage_started, current, cfg) if target_hours else 0.0
    ratio = elapsed / target_hours if target_hours else 0.0
    if stage in {"published", "monitoring", "closed"} or not target_hours:
        sla_status = "not_applicable"
    elif due_at and current > due_at:
        sla_status = "overdue"
    elif ratio >= cfg["at_risk_ratio"]:
        sla_status = "at_risk"
    else:
        sla_status = "on_track"

    escalation_level = "L0"
    escalation_reasons = []
    if sla_status == "at_risk":
        escalation_level = "L1"
        escalation_reasons.append("stage SLA is approaching its internal deadline")
    if sla_status == "overdue":
        escalation_level = "L2"
        escalation_reasons.append("stage SLA is overdue")
    if priority in {"P0", "P1"} and staffing_gaps:
        escalation_level = "L3"
        escalation_reasons.append("high-priority case is not fully staffed")
    if access == "restricted":
        escalation_level = "L3"
        escalation_reasons.append("entity is on the restricted list; public-side work must stop")
    elif access == "observation" and escalation_level == "L0":
        escalation_level = "L1"
        escalation_reasons.append("entity is on the observation list")

    return {
        "case_id": str(case.get("case_id") or ""),
        "title": str(case.get("title") or ""),
        "entity": entity,
        "priority": priority,
        "stage": stage,
        "stage_entered_at": stage_started.isoformat(),
        "access_classification": access,
        "restriction": restriction,
        "target_business_hours": target_hours,
        "elapsed_business_hours": round(elapsed, 2),
        "due_at": due_at.isoformat() if due_at else "",
        "sla_status": sla_status,
        "escalation_level": escalation_level,
        "escalation_reasons": escalation_reasons,
        "staffing_gaps": sorted(set(staffing_gaps)),
        "next_task_id": next_task_id,
        "required_role": required_role,
        "eligible_assignees": eligible,
        "eligible_assignees_by_role": eligible_by_role,
        "assignment_recommendation": eligible[0] if eligible else "",
        "recommendation_only": True,
        "automatic_assignment_executed": False,
    }


def _expected_actor_roles(action: str, case: Dict[str, Any], task_id: str) -> set:
    if action in ASSIGNMENT_ROLE_BY_ACTION or action == "assign_workstream":
        return {"research_coordinator", "research_supervisor"}
    if action in PROTECTED_ROLE_BY_ACTION:
        return {PROTECTED_ROLE_BY_ACTION[action]}
    if action == "complete_task":
        role = _task_owner_role(case, task_id)
        return {role} if role else set()
    if action in {"attach_workpaper", "request_evidence", "resolve_evidence_request"}:
        return {
            "research_coordinator",
            "lead_analyst",
            "information_analyst",
            "fundamental_analyst",
            "market_analyst",
            "risk_reviewer",
        }
    return set()


def _principal_ineligibility(
    principal: Dict[str, Any],
    role: str,
    case: Dict[str, Any],
    restriction: Dict[str, Any],
    active_loads: Dict[str, int],
    *,
    enforce_capacity: bool,
) -> List[str]:
    reasons = []
    if not principal.get("active"):
        reasons.append("principal is inactive")
    if role not in principal.get("roles", []):
        reasons.append("role is not authorized")
    principal_type = principal.get("principal_type")
    if principal_type == "service" and role not in SERVICE_ALLOWED_ROLES:
        reasons.append("service principal cannot hold this role")
    if role in HUMAN_ONLY_ROLES and principal_type != "human":
        reasons.append("role is human-only")
    if role in {"lead_analyst", "quality_reviewer"} and not _verified_analyst_registration(principal):
        reasons.append("verified securities analyst registration is required")
    if role == "compliance_officer" and "compliance_authorized" not in principal.get("authorizations", []):
        reasons.append("compliance authorization is required")
    if role in {"publication_approver", "research_supervisor"} and "publication_authorized" not in principal.get("authorizations", []):
        reasons.append("publication authorization is required")
    side = str(principal.get("information_side") or "unknown")
    if role in PUBLIC_RESEARCH_ROLES and side != "public":
        reasons.append("public-side research role requires information_side=public")
    if role == "compliance_officer" and side not in {"public", "wall"}:
        reasons.append("compliance role requires public or wall information side")
    entity = str(case.get("entity") or "unknown")
    coverage = principal.get("coverage_entities", [])
    if coverage and "*" not in coverage and entity not in coverage:
        reasons.append("entity is outside principal coverage")
    if entity in principal.get("restricted_entities", []):
        reasons.append("entity is restricted for this principal")
    if restriction.get("list_type") == "restricted" and role in PUBLIC_RESEARCH_ROLES:
        reasons.append("entity is on the restricted list")
    if enforce_capacity:
        maximum = int(principal.get("max_active_cases", 0) or 0)
        if maximum <= 0:
            reasons.append("principal has no configured case capacity")
        elif active_loads.get(principal["actor_id"], 0) >= maximum:
            reasons.append("principal is at configured capacity")
    return sorted(set(reasons))


def _normalize_principals(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    principals = []
    errors = []
    seen = set()
    if not isinstance(raw, list):
        return [], ["principals must be a list"]
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            errors.append("principal[{0}] must be an object".format(index))
            continue
        actor_id = str(value.get("actor_id") or "").strip()
        principal_type = str(value.get("principal_type") or "").strip().lower()
        if not actor_id:
            errors.append("principal[{0}] actor_id is required".format(index))
            continue
        if actor_id in seen:
            errors.append("duplicate actor_id: {0}".format(actor_id))
            continue
        if principal_type not in {"human", "service"}:
            errors.append("principal {0} has invalid principal_type".format(actor_id))
            continue
        seen.add(actor_id)
        roles = _clean_list(value.get("roles", []))
        if principal_type == "service":
            disallowed = sorted(set(roles).difference(SERVICE_ALLOWED_ROLES))
            if disallowed:
                errors.append("service principal {0} has disallowed roles: {1}".format(actor_id, ", ".join(disallowed)))
        principals.append({
            "actor_id": actor_id,
            "display_name": str(value.get("display_name") or actor_id),
            "principal_type": principal_type,
            "active": bool(value.get("active", False)),
            "roles": roles,
            "information_side": str(value.get("information_side") or "unknown").strip().lower(),
            "qualifications": value.get("qualifications", {}) if isinstance(value.get("qualifications"), dict) else {},
            "authorizations": _clean_list(value.get("authorizations", [])),
            "onboarding_evidence_refs": _normalize_evidence_refs(
                value.get("onboarding_evidence_refs", {}), actor_id, errors
            ),
            "coverage_entities": _clean_list(value.get("coverage_entities", [])),
            "restricted_entities": _clean_list(value.get("restricted_entities", [])),
            "max_active_cases": max(0, int(value.get("max_active_cases", 0) or 0)),
        })
    return principals, errors


def _normalize_restrictions(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    restrictions = []
    errors = []
    if not isinstance(raw, list):
        return [], ["restriction entries must be a list"]
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            errors.append("restriction[{0}] must be an object".format(index))
            continue
        entity = str(value.get("entity") or "").strip()
        list_type = str(value.get("list_type") or "").strip().lower()
        if not entity or list_type not in {"observation", "restricted"}:
            errors.append("restriction[{0}] requires entity and list_type=observation|restricted".format(index))
            continue
        status = str(value.get("status") or "active").strip().lower()
        reason = str(value.get("reason") or "").strip()
        authorized_by = str(value.get("authorized_by") or "").strip()
        if status == "active" and (not reason or not authorized_by):
            errors.append("active restriction[{0}] requires reason and authorized_by".format(index))
            continue
        restrictions.append({
            "entity": entity,
            "list_type": list_type,
            "status": status,
            "effective_from": str(value.get("effective_from") or ""),
            "effective_to": str(value.get("effective_to") or ""),
            "reason": reason,
            "authorized_by": authorized_by,
        })
    return restrictions, errors


def _capability_status(principals: Sequence[Dict[str, Any]]) -> Dict[str, bool]:
    status = {}
    for role in sorted(MANDATORY_HUMAN_CAPABILITIES):
        status[role] = any(
            item.get("principal_type") == "human"
            and _principal_has_capability(item, role)
            for item in principals
        )
    return status


def _principal_has_capability(principal: Dict[str, Any], role: str) -> bool:
    if not principal.get("active") or role not in principal.get("roles", []):
        return False
    if role in HUMAN_ONLY_ROLES and principal.get("principal_type") != "human":
        return False
    if role in {"lead_analyst", "quality_reviewer"} and not _verified_analyst_registration(principal):
        return False
    if role == "compliance_officer" and "compliance_authorized" not in principal.get("authorizations", []):
        return False
    if role in {"publication_approver", "research_supervisor"} and "publication_authorized" not in principal.get("authorizations", []):
        return False
    side = str(principal.get("information_side") or "unknown")
    if role in PUBLIC_RESEARCH_ROLES and side != "public":
        return False
    return role != "compliance_officer" or side in {"public", "wall"}


def _verified_analyst_registration(principal: Dict[str, Any]) -> bool:
    item = principal.get("qualifications", {}).get("securities_analyst_registration", {})
    return (
        isinstance(item, dict)
        and str(item.get("status") or "").lower() == "verified"
        and bool(str(item.get("registration_id") or "").strip())
    )


def _active_loads(runtime: Dict[str, Any]) -> Dict[str, int]:
    actor_cases: Dict[str, set] = {}
    for case in runtime.get("cases", []) or []:
        if not isinstance(case, dict) or case.get("stage") == "closed":
            continue
        case_id = str(case.get("case_id") or "")
        actors = list((case.get("assignments", {}) or {}).values())
        actors.extend((case.get("task_assignments", {}) or {}).values())
        for actor in actors:
            actor_id = str(actor or "").strip()
            if actor_id:
                actor_cases.setdefault(actor_id, set()).add(case_id)
    return {actor: len(case_ids) for actor, case_ids in actor_cases.items()}


def _task_owner_role(case: Dict[str, Any], task_id: str) -> str:
    task = next((item for item in case.get("workstreams", []) or [] if item.get("task_id") == task_id), None)
    return str((task or {}).get("owner_role") or "")


def _task_assignee(case: Dict[str, Any], task_id: str) -> str:
    task_assignments = case.get("task_assignments", {}) or {}
    if task_assignments.get(task_id):
        return str(task_assignments[task_id])
    task = next((item for item in case.get("workstreams", []) or [] if item.get("task_id") == task_id), None)
    return str((task or {}).get("assignee") or "")


def _actor_assigned_to_case(case: Dict[str, Any], actor: str, actor_role: str) -> bool:
    assignments = case.get("assignments", {}) or {}
    task_assignments = case.get("task_assignments", {}) or {}
    if actor in assignments.values() or actor in task_assignments.values():
        return True
    return actor_role == "research_coordinator"


def _stage_owner_role(stage: str) -> str:
    return {
        "intake": "research_coordinator",
        "triage": "research_coordinator",
        "evidence_collection": "information_analyst",
        "analysis": "lead_analyst",
        "challenge": "risk_reviewer",
        "quality_review": "quality_reviewer",
        "compliance_review": "compliance_officer",
        "human_approval": "publication_approver",
        "published": "lead_analyst",
        "monitoring": "lead_analyst",
    }.get(stage, "")


def _load_json_document(path_value: str, collection_key: str) -> Tuple[Dict[str, Any], List[str]]:
    path = project_path(path_value)
    if not path.exists():
        return {collection_key: []}, ["missing operations file: {0}".format(path)]
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {collection_key: []}, ["invalid operations file {0} ({1})".format(path, type(exc).__name__)]
    if not isinstance(value, dict):
        return {collection_key: []}, ["operations file must contain a JSON object: {0}".format(path)]
    return value, []


def _business_times(cfg: Dict[str, Any]) -> Tuple[time, time]:
    start = _parse_time(cfg.get("workday_start"), time(8, 30))
    end = _parse_time(cfg.get("workday_end"), time(18, 0))
    if end <= start:
        raise ValueError("research_operations workday_end must be after workday_start")
    return start, end


def _next_business_instant(value: datetime, cfg: Dict[str, Any]) -> datetime:
    current = value
    start, end = _business_times(cfg)
    holidays = set(cfg.get("holidays", []))
    weekdays = set(cfg.get("weekdays", [0, 1, 2, 3, 4]))
    while True:
        if current.weekday() not in weekdays or current.date().isoformat() in holidays:
            current = (current + timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            continue
        open_at = current.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        close_at = current.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if current < open_at:
            return open_at
        if current >= close_at:
            current = (current + timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            continue
        return current


def _coerce_now(value: Optional[datetime], timezone_name: str) -> datetime:
    if value is not None:
        if value.tzinfo is None:
            return value.astimezone()
        return value
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(timezone_name))
    except Exception:
        return datetime.now().astimezone()


def _align_timezone(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def _compare_aware(left: datetime, right: datetime) -> int:
    aligned = _align_timezone(right, left)
    return -1 if left < aligned else 1 if left > aligned else 0


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_time(value: Any, default: time) -> time:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return default


def _deny(decision: Dict[str, Any], reason: str) -> None:
    decision["allowed"] = False
    decision["reasons"].append(reason)


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(str(temp), str(path))


def _render_markdown(snapshot: Dict[str, Any]) -> str:
    summary = snapshot.get("summary", {}) or {}
    roster = snapshot.get("roster", {}) or {}
    lines = [
        "# Research Operations Control",
        "",
        "- Generated: {0}".format(snapshot.get("generated_at", "")),
        "- Policy: {0}".format(snapshot.get("policy_version", POLICY_VERSION)),
        "- Assignment mode: suggest_only (no automatic assignment)",
        "- SLA type: internal operating policy, not a regulatory deadline",
        "- Safety: {0}".format(SAFETY_BOUNDARY),
        "",
        "## Roster readiness",
        "",
        "- Principals: {0} (human {1}, service {2})".format(
            roster.get("principal_count", 0), roster.get("human_count", 0), roster.get("service_count", 0)
        ),
        "- Missing mandatory human capabilities: {0}".format(
            ", ".join(roster.get("missing_mandatory_human_capabilities", [])) or "none"
        ),
        "- Validation errors: {0}".format("; ".join(roster.get("errors", [])) or "none"),
        "",
        "## Queue",
        "",
        "- Cases: {0}".format(summary.get("case_count", 0)),
        "- Unstaffed high priority: {0}".format(summary.get("unstaffed_high_priority_count", 0)),
        "- At risk / overdue: {0} / {1}".format(summary.get("at_risk_count", 0), summary.get("overdue_count", 0)),
        "- Observation / restricted: {0} / {1}".format(summary.get("observation_count", 0), summary.get("restricted_count", 0)),
        "",
        "| Esc | Priority | Stage | Case | SLA | Due | Staffing | Next owner | Recommendation |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in snapshot.get("cases", []) or []:
        lines.append("| {escalation_level} | {priority} | {stage} | {case_id} | {sla_status} | {due_at} | {staffing} | {role} | {recommendation} |".format(
            staffing=", ".join(item.get("staffing_gaps", [])),
            role=item.get("required_role", ""),
            recommendation=item.get("assignment_recommendation", ""),
            **item,
        ))
    return "\n".join(lines) + "\n"


def _operations_sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
    escalation = {"L3": 0, "L2": 1, "L1": 2, "L0": 3}.get(item.get("escalation_level"), 9)
    priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(item.get("priority"), 9)
    return escalation, priority, str(item.get("case_id") or "")


def _clean_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values if isinstance(values, Iterable) else []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalize_evidence_refs(raw: Any, actor_id: str, errors: List[str]) -> Dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        errors.append("principal {0} onboarding_evidence_refs must be an object".format(actor_id))
        return {}
    result = {}
    for category, evidence_id in raw.items():
        category_text = str(category or "").strip()
        evidence_text = str(evidence_id or "").strip()
        if not category_text or not evidence_text.startswith("control:"):
            errors.append(
                "principal {0} onboarding evidence references must use category -> control:<id>".format(actor_id)
            )
            continue
        result[category_text] = evidence_text
    return result


def _int_list(values: Any, minimum: int, maximum: int) -> List[int]:
    result = []
    for value in values if isinstance(values, list) else []:
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if minimum <= integer <= maximum and integer not in result:
            result.append(integer)
    return result or [0, 1, 2, 3, 4]
