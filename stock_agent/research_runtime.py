from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import project_path, project_path_override
from .models import now_iso
from .model_operations import load_latest_model_operations
from .market_data_governance import load_latest_market_data_governance
from .review_queue import load_latest_review_queue


RUNTIME_VERSION = "1.0"
SAFETY_BOUNDARY = "Internal research only. No automatic trading, order placement, or position changes."

STAGE_ORDER = [
    "intake",
    "triage",
    "evidence_collection",
    "analysis",
    "challenge",
    "quality_review",
    "compliance_review",
    "human_approval",
    "published",
    "monitoring",
    "closed",
]

PROTECTED_ACTION_ROLES = {
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

ASSIGNMENT_ACTIONS = {
    "assign_lead": "lead_analyst",
    "assign_quality_reviewer": "quality_reviewer",
    "assign_compliance_officer": "compliance_officer",
    "assign_approver": "publication_approver",
}

VALID_ACTIONS = {
    *ASSIGNMENT_ACTIONS,
    "assign_workstream",
    "attach_workpaper",
    "complete_task",
    "request_evidence",
    "resolve_evidence_request",
    *PROTECTED_ACTION_ROLES,
}

FORBIDDEN_ACTION_TERMS = {
    "auto_trade",
    "place_order",
    "order_placement",
    "rebalance_position",
    "position_rebalance",
    "buy",
    "sell",
}

DEFAULT_COMPLIANCE_CHECKS = [
    "public_information_only",
    "inside_information_check",
    "conflict_of_interest_check",
    "quiet_period_check",
    "disclosures_complete",
]

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


@contextmanager
def _active_project_path(resolver: Optional[Any]):
    """Temporarily install a path resolver for a single sync call.

    Delegates to the central context-scoped override in ``config`` so the
    resolver is active for every module this sync touches (not just this one)
    and is always restored, even on error, without any module-global mutation.
    The resolver is therefore safe across threads and asyncio tasks.
    """
    with project_path_override(resolver):
        yield


def sync_research_runtime(
    config: Dict[str, Any],
    cases: Optional[List[Dict[str, Any]]] = None,
    signals: Optional[Sequence[Any]] = None,
    report_path: str = "",
    project_path_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Project research cases into an auditable institution-style runtime.

    ``project_path_fn`` lets a caller (e.g. the pipeline) forward its active path
    resolver so an isolated test root is used by every file this sync touches.
    The resolver is installed only for the duration of this call and always
    restored in a ``finally`` block, so no global path change leaks after errors.

    Fail-closed rules:
    * Empty upstream case input must never erase a populated runtime; the
      existing cases, workpapers, actions, approvals, and stages are preserved.
    * Unavailable or severely incomplete historical signal backfill must never
      regress a case that already has verified evidence; the last verified
      runtime state is preserved and the degradation is reported in structured
      diagnostics.
    """
    runtime_cfg = _runtime_config(config)
    if not runtime_cfg["enabled"]:
        return _disabled_result()

    with _active_project_path(project_path_fn):
        if cases is None:
            from .research_cases import list_research_cases

            cases = list_research_cases()
        if signals is None:
            report = _load_report(report_path)
            signals = report.get("signals", []) if isinstance(report, dict) else []

        previous = load_research_runtime()
        previous_cases = {
            str(item.get("case_id")): item
            for item in previous.get("cases", [])
            if isinstance(item, dict) and item.get("case_id")
        }

        diagnostics = _new_diagnostics()
        incoming_cases = [
            item for item in (cases or [])
            if isinstance(item, dict) and str(item.get("case_id") or "").strip()
        ]
        diagnostics["requested_cases"] = len(cases or [])
        diagnostics["loaded_cases"] = len(incoming_cases)

        # Fail closed: empty upstream input must never erase a populated runtime.
        if not incoming_cases and previous_cases:
            diagnostics = _empty_input_preserved_diagnostics(previous_cases)
            runtime = {
                "runtime_version": RUNTIME_VERSION,
                "generated_at": now_iso(),
                "policy": _runtime_policy(runtime_cfg),
                "model_operations": _runtime_model_operations(config),
                "review_queue": _runtime_review_queue(config),
                "market_data_governance": load_latest_market_data_governance(config),
                "cases": list(previous_cases.values()),
                "safety_boundary": SAFETY_BOUNDARY,
                "degradation": diagnostics,
            }
            _save_runtime(runtime)
            summary = summarize_research_runtime(runtime)
            paths = write_research_runtime_report(runtime, summary)
            return _runtime_result(runtime, summary, paths, diagnostics)

        signal_map = _signal_map(signals or [])
        all_signal_ids = _clean_list(
            signal_id
            for source_case in incoming_cases
            for signal_id in source_case.get("signal_ids", [])
        )
        missing_signal_ids = [signal_id for signal_id in all_signal_ids if signal_id not in signal_map]
        backfill_attempted = False
        if missing_signal_ids:
            backfill_attempted = True
            signal_map.update(_signal_map(_load_persisted_signals(config, missing_signal_ids)))
        still_missing_signal_ids = [signal_id for signal_id in all_signal_ids if signal_id not in signal_map]
        diagnostics["requested_signal_count"] = len(all_signal_ids)
        diagnostics["loaded_signal_count"] = len(all_signal_ids) - len(still_missing_signal_ids)
        diagnostics["missing_signal_ids"] = still_missing_signal_ids
        diagnostics["backfill_unavailable"] = bool(backfill_attempted and still_missing_signal_ids)

        projected: List[Dict[str, Any]] = []
        preserved_case_ids: List[str] = []
        degradation_reasons: List[str] = []

        for source_case in incoming_cases:
            case_id = str(source_case.get("case_id") or "").strip()
            if not case_id:
                continue
            old = previous_cases.get(case_id, {})
            case_missing = _case_missing_signal_ids(source_case, signal_map)
            # Preserve the last verified state when a case's signals cannot be
            # recovered instead of re-projecting from missing evidence.
            if case_missing and old:
                preserved_case_ids.append(case_id)
                degradation_reasons.append(
                    "case {0}: {1} referenced signal(s) unavailable ({2}); preserving prior runtime state".format(
                        case_id, len(case_missing), ", ".join(case_missing[:3])
                    )
                )
                projected.append(old)
                continue
            matched_signals = [signal_map[item] for item in source_case.get("signal_ids", []) if item in signal_map]
            runtime_case = _project_runtime_case(source_case, matched_signals, old, runtime_cfg)
            projected.append(runtime_case)
            if not old:
                _append_runtime_event("research_case_runtime_created", runtime_case, actor="system")
            elif old.get("stage") != runtime_case.get("stage"):
                _append_runtime_event(
                    "research_case_stage_changed",
                    runtime_case,
                    actor="system",
                    payload={"from_stage": old.get("stage", ""), "to_stage": runtime_case.get("stage", "")},
                )

        diagnostics["preserved_cases"] = preserved_case_ids
        diagnostics["degraded"] = bool(diagnostics["backfill_unavailable"] or preserved_case_ids)
        diagnostics["degradation_reasons"] = _unique(degradation_reasons)

        projected.sort(key=_runtime_case_sort_key)
        runtime = {
            "runtime_version": RUNTIME_VERSION,
            "generated_at": now_iso(),
            "policy": _runtime_policy(runtime_cfg),
            "model_operations": _runtime_model_operations(config),
            "review_queue": _runtime_review_queue(config),
            "market_data_governance": load_latest_market_data_governance(config),
            "cases": projected,
            "safety_boundary": SAFETY_BOUNDARY,
        }
        if diagnostics["degraded"]:
            runtime["degradation"] = diagnostics
        _save_runtime(runtime)
        summary = summarize_research_runtime(runtime)
        paths = write_research_runtime_report(runtime, summary)
        return _runtime_result(runtime, summary, paths, diagnostics)


def load_research_runtime() -> Dict[str, Any]:
    path = project_path("data/research_runtime/runtime.json")
    if not path.exists():
        return {
            "runtime_version": RUNTIME_VERSION,
            "generated_at": "",
            "policy": {},
            "cases": [],
            "safety_boundary": SAFETY_BOUNDARY,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {"cases": []}
    except (OSError, json.JSONDecodeError):
        return {"runtime_version": RUNTIME_VERSION, "cases": [], "safety_boundary": SAFETY_BOUNDARY}


def get_runtime_case(case_id: str) -> Dict[str, Any]:
    target = str(case_id or "").strip()
    for case in load_research_runtime().get("cases", []):
        if str(case.get("case_id")) == target:
            return case
    return {}


def record_runtime_action(
    case_id: str,
    action: str,
    actor: str,
    actor_role: str,
    *,
    notes: str = "",
    task_id: str = "",
    assignee: str = "",
    references: Optional[Sequence[str]] = None,
    checks: Optional[Sequence[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    action = str(action or "").strip()
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    case_id = str(case_id or "").strip()
    if not case_id:
        raise ValueError("case_id is required")
    if action not in VALID_ACTIONS or action in FORBIDDEN_ACTION_TERMS:
        raise ValueError("Invalid or unsafe research runtime action: {0}".format(action))
    if not actor or not actor_role:
        raise ValueError("actor and actor_role are required")

    runtime = load_research_runtime()
    cases = runtime.get("cases", [])
    runtime_case = next((item for item in cases if str(item.get("case_id")) == case_id), None)
    if runtime_case is None:
        raise ValueError("Unknown research runtime case: {0}".format(case_id))

    cfg = _runtime_config(config or {})
    before_stage = str(runtime_case.get("stage") or "")
    reference_list = _clean_list(references or [])
    check_list = _clean_list(checks or [])

    from .research_workpapers import validate_workpaper_gate

    workpaper_authorization = validate_workpaper_gate(case_id, action, config or {})

    from .research_operations import validate_runtime_action

    authorization = validate_runtime_action(
        config or {},
        runtime_case,
        action,
        actor,
        actor_role,
        assignee=assignee,
        task_id=task_id,
        runtime=runtime,
    )

    if action in ASSIGNMENT_ACTIONS:
        _apply_assignment(runtime_case, action, assignee, actor, actor_role)
    elif action == "assign_workstream":
        _apply_workstream_assignment(runtime_case, task_id, assignee, actor, actor_role)
    elif action == "attach_workpaper":
        _attach_workpapers(runtime_case, reference_list, actor, actor_role, notes)
    elif action == "complete_task":
        _complete_task(runtime_case, task_id, actor, actor_role, notes, reference_list)
    elif action == "request_evidence":
        _request_evidence(runtime_case, notes, actor, actor_role)
    elif action == "resolve_evidence_request":
        _resolve_evidence_request(runtime_case, task_id, reference_list, actor, actor_role, notes)
    elif action == "quality_approve":
        _quality_approve(runtime_case, actor, actor_role, notes)
    elif action == "quality_reject":
        _quality_reject(runtime_case, actor, actor_role, notes)
    elif action == "compliance_clear":
        _compliance_clear(runtime_case, actor, actor_role, notes, check_list, cfg)
    elif action == "compliance_block":
        _compliance_block(runtime_case, actor, actor_role, notes, check_list)
    elif action == "publication_approve":
        _publication_approve(runtime_case, actor, actor_role, notes)
    elif action == "publication_reject":
        _publication_reject(runtime_case, actor, actor_role, notes)
    elif action == "start_monitoring":
        _start_monitoring(runtime_case, actor, actor_role, notes)
    elif action == "close_case":
        _close_case(runtime_case, actor, actor_role, notes)
    elif action == "reopen_case":
        _reopen_case(runtime_case, actor, actor_role, notes)

    action_record = {
        "action_id": _action_id(case_id, action),
        "case_id": case_id,
        "action": action,
        "actor": actor,
        "actor_role": actor_role,
        "notes": str(notes or ""),
        "task_id": str(task_id or ""),
        "assignee": str(assignee or ""),
        "references": reference_list,
        "checks": check_list,
        "recorded_at": now_iso(),
        "from_stage": before_stage,
        "authorization": authorization,
        "workpaper_authorization": workpaper_authorization,
        "principal_type": authorization.get("principal_type", "legacy"),
        "operations_policy_version": authorization.get("policy_version", ""),
        "safety_boundary": SAFETY_BOUNDARY,
    }
    history = list(runtime_case.get("action_history", []))
    history.append(action_record)
    runtime_case["action_history"] = history[-100:]
    runtime_case["updated_at"] = action_record["recorded_at"]
    _refresh_runtime_case(runtime_case, cfg)
    if before_stage != runtime_case.get("stage"):
        runtime_case["stage_entered_at"] = action_record["recorded_at"]
    action_record["to_stage"] = runtime_case.get("stage", "")

    runtime["generated_at"] = now_iso()
    runtime["policy"] = _runtime_policy(cfg)
    runtime["safety_boundary"] = SAFETY_BOUNDARY
    _save_runtime(runtime)
    _append_runtime_event(
        "research_case_action_recorded",
        runtime_case,
        actor=actor,
        payload={
            "action": action,
            "actor_role": actor_role,
            "task_id": str(task_id or ""),
            "from_stage": before_stage,
            "to_stage": runtime_case.get("stage", ""),
        },
    )
    if before_stage != runtime_case.get("stage"):
        _append_runtime_event(
            "research_case_stage_changed",
            runtime_case,
            actor=actor,
            payload={"from_stage": before_stage, "to_stage": runtime_case.get("stage", ""), "action": action},
        )

    from .research_workpapers import refresh_workpaper_from_runtime_action

    workpaper_sync = refresh_workpaper_from_runtime_action(runtime_case, action_record, config or {})

    summary = summarize_research_runtime(runtime)
    paths = write_research_runtime_report(runtime, summary)
    return {
        "record": action_record,
        "case": runtime_case,
        "summary": summary,
        "workpaper_sync": workpaper_sync,
        **paths,
    }


def summarize_research_runtime(runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    runtime = runtime or load_research_runtime()
    cases = [item for item in runtime.get("cases", []) if isinstance(item, dict)]
    stages = Counter(str(item.get("stage") or "unknown") for item in cases)
    blocked = [item for item in cases if item.get("blockers")]
    pending_human = [
        item
        for item in cases
        if item.get("stage") in {"quality_review", "compliance_review", "human_approval"}
    ]
    monitoring = [item for item in cases if item.get("stage") == "monitoring"]
    return {
        "generated_at": now_iso(),
        "runtime_version": runtime.get("runtime_version", RUNTIME_VERSION),
        "case_count": len(cases),
        "stage_counts": dict(sorted(stages.items(), key=lambda item: _stage_rank(item[0]))),
        "blocked_count": len(blocked),
        "pending_human_count": len(pending_human),
        "monitoring_count": len(monitoring),
        "blocked_cases": [_compact_case(item) for item in blocked[:50]],
        "pending_human_cases": [_compact_case(item) for item in pending_human[:50]],
        "next_action_queue": [_queue_item(item) for item in cases if item.get("stage") != "closed"][:100],
        "policy": runtime.get("policy", {}),
        "model_operations": runtime.get("model_operations", {}),
        "review_queue": (runtime.get("review_queue", {}) or {}).get("summary", {}),
        "market_data_governance": runtime.get("market_data_governance", {}),
        "degradation": runtime.get("degradation", _new_diagnostics()),
        "safety_boundary": SAFETY_BOUNDARY,
    }


def write_research_runtime_report(
    runtime: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    runtime = runtime or load_research_runtime()
    summary = summary or summarize_research_runtime(runtime)
    out_dir = project_path("reports/research_runtime")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_time(summary.get("generated_at") or now_iso())
    json_path = out_dir / "runtime_{0}.json".format(stamp)
    md_path = out_dir / "runtime_{0}.md".format(stamp)
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    payload = {"summary": summary, "runtime": runtime}
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    markdown = _render_runtime_markdown(runtime, summary)
    json_path.write_text(json_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        "research_runtime_path": str(md_path),
        "research_runtime_json_path": str(json_path),
        "research_runtime_latest_path": str(latest_md),
        "research_runtime_latest_json_path": str(latest_json),
    }


def _runtime_review_queue(config: Dict[str, Any]) -> Dict[str, Any]:
    queue = load_latest_review_queue(config)
    if not queue:
        return {}
    return {
        "version": queue.get("version", ""),
        "generated_at": queue.get("generated_at", ""),
        "summary": queue.get("summary", {}),
        "assignment_mode": queue.get("assignment_mode", "suggest_only"),
        "automatic_assignment_executed": False,
        "safety_boundary": queue.get("safety_boundary", ""),
    }


def _project_runtime_case(
    source_case: Dict[str, Any],
    signals: Sequence[Dict[str, Any]],
    previous: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    previous_stage = str(previous.get("stage") or "")
    case = {
        "runtime_version": RUNTIME_VERSION,
        "case_id": str(source_case.get("case_id") or ""),
        "title": str(source_case.get("title") or source_case.get("case_id") or "Research Case"),
        "entity": str(source_case.get("entity") or "unknown"),
        "topic": str(source_case.get("topic") or "unknown"),
        "catalyst_path": str(source_case.get("catalyst_path") or "unknown"),
        "top_priority": str(source_case.get("top_priority") or "P3"),
        "source_case_status": str(source_case.get("status") or "active"),
        "signal_ids": _clean_list(source_case.get("signal_ids", [])),
        "source_types": _clean_list(source_case.get("source_types", [])),
        "created_at": previous.get("created_at") or source_case.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "published_at": previous.get("published_at", ""),
        "monitoring_started_at": previous.get("monitoring_started_at", ""),
        "closed_at": previous.get("closed_at", ""),
        "close_reason": previous.get("close_reason", ""),
        "assignments": dict(previous.get("assignments", {})),
        "task_assignments": dict(previous.get("task_assignments", {})),
        "stage_entered_at": previous.get("stage_entered_at") or previous.get("created_at") or source_case.get("created_at") or now_iso(),
        "approvals": _normalized_approvals(previous.get("approvals", {})),
        "manual_workpapers": list(previous.get("manual_workpapers", [])),
        "manual_evidence_requests": list(previous.get("manual_evidence_requests", [])),
        "task_overrides": dict(previous.get("task_overrides", {})),
        "action_history": list(previous.get("action_history", []))[-100:],
        "source_case": _source_case_snapshot(source_case),
        "evidence_facts": _evidence_facts(source_case, signals, previous.get("evidence_facts", {})),
        "safety_boundary": SAFETY_BOUNDARY,
    }
    _refresh_runtime_case(case, cfg)
    if previous_stage and previous_stage != case.get("stage"):
        case["stage_entered_at"] = now_iso()
    return case


def _refresh_runtime_case(case: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    gates = _evaluate_gates(case, cfg)
    case["gates"] = gates
    case["workpaper_manifest"] = _workpaper_manifest(case)
    case["workstreams"] = _build_workstreams(case, gates)
    case["stage"] = _derive_stage(case, gates)
    case["status"] = "closed" if case["stage"] == "closed" else "active"
    case["blockers"] = _case_blockers(case, gates)
    case["next_actions"] = _case_next_actions(case)
    case["human_gate_required"] = case["stage"] in {"quality_review", "compliance_review", "human_approval"}
    case["external_publication_allowed"] = bool(cfg.get("external_publication_enabled", False))
    case["auto_publish"] = False
    case["updated_at"] = now_iso()


def _evaluate_gates(case: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    facts = case.get("evidence_facts", {}) or {}
    source_case = case.get("source_case", {}) or {}
    requests = [item for item in case.get("manual_evidence_requests", []) if item.get("status") == "open"]
    overrides = case.get("task_overrides", {}) or {}
    workpapers = _workpaper_manifest(case)

    entity_ok = str(case.get("entity") or "unknown") != "unknown"
    topic_ok = str(case.get("topic") or "unknown") != "unknown"
    intake_missing = []
    if not entity_ok:
        intake_missing.append("covered entity is not identified")
    if not topic_ok:
        intake_missing.append("research topic is not identified")
    intake = _gate("PASS" if not intake_missing else "BLOCKED", intake_missing)

    priority_ok = str(case.get("top_priority") or "") in {"P0", "P1", "P2", "P3"}
    triage = _gate("PASS" if priority_ok and intake["status"] == "PASS" else "BLOCKED", [] if priority_ok else ["priority is not classified"])

    high_priority = str(case.get("top_priority")) in set(cfg.get("require_primary_source_for", []))
    traceable_count = int(facts.get("traceable_source_count", 0)) + _workpaper_count(workpapers, {"official_filing", "source_document", "meeting_note"})
    primary_count = int(facts.get("primary_source_count", 0)) + _workpaper_count(workpapers, {"official_filing"})
    evidence_reasons = []
    if traceable_count <= 0:
        evidence_reasons.append("no traceable source or workpaper")
    if high_priority and primary_count <= 0:
        evidence_reasons.append("P0/P1 case requires an official primary source")
    if facts.get("source_error_count", 0):
        evidence_reasons.append("source errors remain unresolved")
    evidence_reasons.extend(str(item) for item in facts.get("evidence_gaps", []) if str(item).strip())
    evidence_reasons.extend("open evidence request: {0}".format(item.get("request", "")) for item in requests)
    if _task_completed(overrides, "source_verification") and not requests:
        evidence_reasons = []
        if high_priority and primary_count <= 0:
            evidence_reasons.append("P0/P1 case requires an official primary source")
    evidence = _gate("PASS" if not evidence_reasons else "BLOCKED", _unique(evidence_reasons))

    has_thesis = bool(facts.get("has_hypothesis")) or _task_completed(overrides, "hypothesis_analysis") or _workpaper_count(workpapers, {"analysis_model", "research_note"}) > 0
    has_risk = bool(facts.get("has_risk_challenge")) or _task_completed(overrides, "independent_challenge") or _workpaper_count(workpapers, {"risk_note"}) > 0
    recorded_challenge = _recorded_independent_challenge(case)
    source_types = set(case.get("source_types", []))
    fundamental_required = bool(source_types.intersection({"filing", "news", "manual"}))
    market_required = bool("price" in source_types or case.get("top_priority") in {"P0", "P1"})
    has_fundamental = bool(facts.get("has_fundamental_analysis")) or _task_completed(overrides, "fundamental_analysis") or _workpaper_count(workpapers, {"analysis_model"}) > 0
    has_market = bool(facts.get("has_market_context")) or _task_completed(overrides, "market_context") or _workpaper_count(workpapers, {"market_data"}) > 0
    analysis_reasons = []
    if evidence["status"] != "PASS":
        analysis_reasons.append("evidence gate has not passed")
    if not has_thesis:
        analysis_reasons.append("base thesis or hypothesis is missing")
    if fundamental_required and not has_fundamental:
        analysis_reasons.append("fundamental or event-impact workstream is incomplete")
    if market_required and not has_market:
        analysis_reasons.append("market-context workstream is incomplete")
    if not has_risk:
        analysis_reasons.append("risk or falsification analysis is missing")
    analysis = _gate("PASS" if not analysis_reasons else "BLOCKED", analysis_reasons)

    challenge_reasons = []
    if analysis["status"] != "PASS":
        challenge_reasons.append("analysis gate has not passed")
    challenge_complete = recorded_challenge if cfg.get("require_recorded_challenge") else has_risk
    if not challenge_complete:
        challenge_reasons.append("independent challenge is incomplete")
    challenge = _gate("PASS" if not challenge_reasons else "BLOCKED", challenge_reasons)

    approvals = case.get("approvals", {}) or {}
    quality = _approval_gate(approvals.get("quality_review", {}), "independent quality review is pending")
    compliance = _approval_gate(approvals.get("compliance_review", {}), "compliance review is pending")
    publication = _approval_gate(approvals.get("publication", {}), "authorized human publication approval is pending")

    return {
        "intake": intake,
        "triage": triage,
        "evidence": evidence,
        "analysis": analysis,
        "challenge": challenge,
        "quality_review": quality,
        "compliance_review": compliance,
        "publication": publication,
    }


def _derive_stage(case: Dict[str, Any], gates: Dict[str, Dict[str, Any]]) -> str:
    if case.get("closed_at") or case.get("source_case_status") in {"noise", "archived"}:
        return "closed"
    if gates["intake"]["status"] != "PASS":
        return "intake"
    if gates["triage"]["status"] != "PASS":
        return "triage"
    if gates["evidence"]["status"] != "PASS":
        return "evidence_collection"
    if gates["analysis"]["status"] != "PASS":
        return "analysis"
    if gates["challenge"]["status"] != "PASS":
        return "challenge"
    quality_status = str((case.get("approvals", {}).get("quality_review", {}) or {}).get("status") or "pending")
    if quality_status == "rejected":
        return "analysis"
    if gates["quality_review"]["status"] != "PASS":
        return "quality_review"
    compliance_status = str((case.get("approvals", {}).get("compliance_review", {}) or {}).get("status") or "pending")
    if compliance_status == "blocked":
        return "compliance_review"
    if gates["compliance_review"]["status"] != "PASS":
        return "compliance_review"
    publication_status = str((case.get("approvals", {}).get("publication", {}) or {}).get("status") or "pending")
    if publication_status == "rejected":
        return "quality_review"
    if gates["publication"]["status"] != "PASS":
        return "human_approval"
    if case.get("monitoring_started_at"):
        return "monitoring"
    return "published"


def _build_workstreams(case: Dict[str, Any], gates: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    source_types = set(case.get("source_types", []))
    high_priority = case.get("top_priority") in {"P0", "P1"}
    definitions = [
        ("mandate_intake", "intake", "research_coordinator", True, [], gates["intake"]),
        ("scope_triage", "triage", "research_coordinator", True, ["mandate_intake"], gates["triage"]),
        ("source_verification", "evidence_collection", "information_analyst", True, ["scope_triage"], gates["evidence"]),
        ("fundamental_analysis", "analysis", "fundamental_analyst", bool(source_types.intersection({"filing", "news", "manual"})), ["source_verification"], gates["analysis"]),
        ("market_context", "analysis", "market_analyst", bool("price" in source_types or high_priority), ["source_verification"], gates["analysis"]),
        ("hypothesis_analysis", "analysis", "lead_analyst", True, ["source_verification"], gates["analysis"]),
        ("independent_challenge", "challenge", "risk_reviewer", True, ["hypothesis_analysis"], gates["challenge"]),
        ("quality_review", "quality_review", "quality_reviewer", True, ["independent_challenge"], gates["quality_review"]),
        ("compliance_review", "compliance_review", "compliance_officer", True, ["quality_review"], gates["compliance_review"]),
        ("publication_approval", "human_approval", "research_supervisor", True, ["compliance_review"], gates["publication"]),
        ("post_publication_monitoring", "monitoring", "lead_analyst", True, ["publication_approval"], _gate("PASS" if case.get("monitoring_started_at") else "PENDING", [])),
    ]
    overrides = case.get("task_overrides", {}) or {}
    task_assignments = case.get("task_assignments", {}) or {}
    rows = []
    for task_id, stage, owner_role, required, dependencies, gate in definitions:
        if not required:
            status = "not_required"
        elif _task_completed(overrides, task_id):
            status = "completed"
        elif gate.get("status") == "PASS":
            status = "completed"
        elif any(_dependency_incomplete(rows, dependency) for dependency in dependencies):
            status = "blocked"
        else:
            status = "ready"
        override = overrides.get(task_id, {}) if isinstance(overrides.get(task_id), dict) else {}
        rows.append({
            "task_id": task_id,
            "stage": stage,
            "owner_role": owner_role,
            "assignee": task_assignments.get(task_id) or (case.get("assignments", {}) or {}).get(_assignment_key_for_owner(owner_role), ""),
            "required": required,
            "dependencies": dependencies,
            "status": status,
            "completed_by": override.get("actor", ""),
            "completed_at": override.get("completed_at", ""),
            "evidence_refs": override.get("references", []),
            "blockers": gate.get("reasons", []) if status in {"blocked", "ready"} else [],
        })
    return rows


def _evidence_facts(
    source_case: Dict[str, Any],
    signals: Sequence[Dict[str, Any]],
    previous: Dict[str, Any],
) -> Dict[str, Any]:
    traceable = set(str(item) for item in previous.get("traceable_sources", []) if str(item))
    primary = set(str(item) for item in previous.get("primary_sources", []) if str(item))
    # Rebuild inferred gaps from current persisted signals on every sync. Manual
    # requests have their own durable collection; carrying old inferred gaps
    # forever turns resolved research questions into permanent blockers.
    gaps: set = set()
    analysis_questions = set(
        str(item) for item in previous.get("analysis_questions", []) if str(item)
    )
    source_errors = int(previous.get("source_error_count", 0) or 0)
    has_hypothesis = bool(previous.get("has_hypothesis"))
    has_risk = bool(previous.get("has_risk_challenge"))
    has_fundamental = bool(previous.get("has_fundamental_analysis"))
    has_market = bool(previous.get("has_market_context"))
    analysis_refs = set(str(item) for item in previous.get("analysis_refs", []) if str(item))
    llm_governance_denied = 0
    llm_governance_output_blocked = 0
    llm_governance_restricted = 0
    llm_governance_injection = 0

    case_analysis = source_case.get("case_analysis", {}) or {}
    hypotheses = case_analysis.get("hypotheses", []) if isinstance(case_analysis, dict) else []
    counter_questions = case_analysis.get("counter_questions", []) if isinstance(case_analysis, dict) else []
    case_gaps = case_analysis.get("evidence_gaps", []) if isinstance(case_analysis, dict) else []
    if hypotheses:
        has_hypothesis = True
        analysis_refs.add("case_analysis:hypotheses")
    if counter_questions or any(item.get("evidence_against") for item in hypotheses if isinstance(item, dict)):
        has_risk = True
        analysis_refs.add("case_analysis:counter_questions")
    for item in case_gaps:
        if _is_hard_evidence_gap(item):
            gaps.add(str(item))
        elif str(item).strip():
            analysis_questions.add(str(item).strip())

    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        source_type = str(signal.get("source_type") or "")
        source = str(signal.get("source") or "")
        url = str(signal.get("url") or "").strip()
        payload = signal.get("payload", {}) if isinstance(signal.get("payload"), dict) else {}
        if source_type == "source_error":
            source_errors += 1
        if url:
            traceable.add("signal:{0}:url".format(signal_id))
        if source_type == "price" and _has_market_snapshot(payload):
            traceable.add("signal:{0}:market_snapshot".format(signal_id))
            if source not in {"market_cache", "cache", "manual"}:
                primary.add("signal:{0}:market_data:{1}".format(signal_id, source or "provider"))
        filing = payload.get("filing_evidence", {}) if isinstance(payload.get("filing_evidence"), dict) else {}
        filing_ok = str(filing.get("status") or "").lower() in {"ok", "verified", "pass"}
        if source_type == "filing" and url and (filing_ok or "filing" in source or "cninfo" in source):
            primary.add("signal:{0}:official_filing".format(signal_id))
        chain = signal.get("evidence_chain", {}) if isinstance(signal.get("evidence_chain"), dict) else {}
        for item in chain.get("unresolved_refs", []) or []:
            if _is_hard_evidence_gap(item):
                gaps.add(str(item))
            elif str(item).strip():
                analysis_questions.add(str(item).strip())
        for item in chain.get("evidence_gap_tasks", []) or []:
            if isinstance(item, dict) and item.get("status", "open") == "open":
                gap = str(item.get("gap") or item.get("task_id") or "evidence gap")
                if _is_hard_evidence_gap(gap):
                    gaps.add(gap)
                elif gap.strip():
                    analysis_questions.add(gap.strip())
        governance = signal.get("llm_governance", {}) if isinstance(signal.get("llm_governance"), dict) else {}
        for decision in governance.values():
            if not isinstance(decision, dict):
                continue
            if decision.get("decision") == "denied":
                llm_governance_denied += 1
                analysis_questions.add("LLM egress was denied; human data-classification review is required")
            if decision.get("classification") == "restricted":
                llm_governance_restricted += 1
                analysis_questions.add("restricted or nonpublic information must remain outside external LLM processing")
            if decision.get("prompt_injection_detected"):
                llm_governance_injection += 1
                analysis_questions.add("prompt-injection content requires source and security review")
            if str(((decision.get("postflight", {}) or {}).get("status") or "")) == "blocked":
                llm_governance_output_blocked += 1
                analysis_questions.add("LLM output lacks approved evidence references and cannot support a conclusion")
        llm_analysis = signal.get("llm_analysis", {}) if isinstance(signal.get("llm_analysis"), dict) else {}
        if llm_analysis and str(llm_analysis.get("grounding_status") or "") != "blocked":
            has_hypothesis = True
            analysis_refs.add("signal:{0}:llm_analysis".format(signal_id))
            risks = llm_analysis.get("risk_points", [])
            has_risk = has_risk or bool(risks)
        role_analyses = signal.get("role_analyses", []) or []
        grounded_roles = [
            item for item in role_analyses
            if isinstance(item, dict) and str(item.get("grounding_status") or "") != "blocked"
        ]
        if grounded_roles:
            has_hypothesis = True
            analysis_refs.add("signal:{0}:role_analyses".format(signal_id))
        for role in grounded_roles:
            role_id = str(role.get("role_id") or "").lower()
            if "risk" in role_id or role.get("risk_points") or role.get("counter_arguments"):
                has_risk = True
            if "fundamental" in role_id or "filing" in role_id or "event" in role_id:
                has_fundamental = True
            if "market" in role_id or "technical" in role_id or "price" in role_id:
                has_market = True
        synthesis = signal.get("role_synthesis", {}) if isinstance(signal.get("role_synthesis"), dict) else {}
        if synthesis.get("key_disagreement") or synthesis.get("conflict_level") in {"medium", "high"}:
            has_risk = True

    # Structured feeds do not necessarily have a web URL. Once a traceable
    # snapshot exists, URL-shaped gaps are not evidence blockers. A high-priority
    # case still needs a primary source via the independent primary_count gate.
    if traceable:
        gaps = {
            item for item in gaps
            if not any(token in item.lower() for token in ["url", "original_source"])
            and not any(token in item for token in ["原始来源", "原始链接", "未提供链接"])
        }
    if primary:
        gaps = {
            item for item in gaps
            if not any(token in item.lower() for token in ["official", "filing"])
            and not any(token in item for token in ["公告原文", "公告链接", "监管文件"])
        }
    gaps = {item for item in gaps if _is_material_evidence_gap(item, source_case)}
    return {
        "traceable_source_count": len(traceable),
        "primary_source_count": len(primary),
        "traceable_sources": sorted(traceable),
        "primary_sources": sorted(primary),
        "source_error_count": source_errors,
        "evidence_gaps": sorted(gaps),
        "analysis_questions": sorted(analysis_questions)[:50],
        "has_hypothesis": has_hypothesis,
        "has_risk_challenge": has_risk,
        "has_fundamental_analysis": has_fundamental,
        "has_market_context": has_market,
        "analysis_refs": sorted(analysis_refs),
        "llm_governance_denied_count": llm_governance_denied,
        "llm_governance_output_blocked_count": llm_governance_output_blocked,
        "llm_governance_restricted_count": llm_governance_restricted,
        "llm_governance_injection_count": llm_governance_injection,
        "latest_signal_at": source_case.get("latest_signal_at", ""),
    }


def _apply_assignment(
    case: Dict[str, Any],
    action: str,
    assignee: str,
    actor: str,
    actor_role: str,
) -> None:
    if actor_role not in {"research_coordinator", "research_supervisor"}:
        raise ValueError("Only a research coordinator or supervisor may assign controlled roles")
    assignee = str(assignee or "").strip()
    if not assignee:
        raise ValueError("assignee is required for {0}".format(action))
    key = ASSIGNMENT_ACTIONS[action]
    assignments = dict(case.get("assignments", {}))
    assignments[key] = assignee
    _validate_assignment_separation(assignments)
    case["assignments"] = assignments


def _apply_workstream_assignment(
    case: Dict[str, Any],
    task_id: str,
    assignee: str,
    actor: str,
    actor_role: str,
) -> None:
    if actor_role not in {"research_coordinator", "research_supervisor"}:
        raise ValueError("Only a research coordinator or supervisor may assign workstreams")
    task_id = str(task_id or "").strip()
    assignee = str(assignee or "").strip()
    task = next((item for item in case.get("workstreams", []) if item.get("task_id") == task_id), None)
    if task is None:
        raise ValueError("Unknown workstream task: {0}".format(task_id))
    if not assignee:
        raise ValueError("assignee is required for assign_workstream")
    if task.get("status") in {"completed", "not_required"}:
        raise ValueError("Completed or non-required workstream cannot be reassigned")
    assignments = dict(case.get("task_assignments", {}))
    assignments[task_id] = assignee
    case["task_assignments"] = assignments


def _attach_workpapers(
    case: Dict[str, Any],
    references: List[str],
    actor: str,
    actor_role: str,
    notes: str,
) -> None:
    if actor_role not in {
        "research_coordinator",
        "lead_analyst",
        "information_analyst",
        "fundamental_analyst",
        "market_analyst",
        "risk_reviewer",
    }:
        raise ValueError("actor_role cannot attach research workpapers")
    if not references:
        raise ValueError("at least one --reference is required")
    rows = list(case.get("manual_workpapers", []))
    existing = {str(item.get("reference")) for item in rows if isinstance(item, dict)}
    for reference in references:
        if reference in existing:
            continue
        rows.append({
            "workpaper_id": "workpaper:{0}".format(hashlib.sha1(reference.encode("utf-8")).hexdigest()[:12]),
            "kind": _workpaper_kind(reference),
            "reference": reference,
            "notes": str(notes or ""),
            "attached_by": actor,
            "actor_role": actor_role,
            "attached_at": now_iso(),
        })
        existing.add(reference)
    case["manual_workpapers"] = rows


def _complete_task(
    case: Dict[str, Any],
    task_id: str,
    actor: str,
    actor_role: str,
    notes: str,
    references: List[str],
) -> None:
    task_id = str(task_id or "").strip()
    task = next((item for item in case.get("workstreams", []) if item.get("task_id") == task_id), None)
    if task is None:
        raise ValueError("Unknown workstream task: {0}".format(task_id))
    owner_role = str(task.get("owner_role") or "")
    if actor_role not in {owner_role, "research_coordinator", "research_supervisor"}:
        raise ValueError("Task {0} must be completed by {1}".format(task_id, owner_role))
    if task_id in {"quality_review", "compliance_review", "publication_approval"}:
        raise ValueError("Protected gates require their dedicated approval action")
    if task_id in {"source_verification", "fundamental_analysis", "market_context", "hypothesis_analysis", "independent_challenge"} and not references:
        raise ValueError("Task {0} requires at least one retained workpaper reference".format(task_id))
    if task_id == "independent_challenge":
        lead = str((case.get("assignments", {}) or {}).get("lead_analyst") or "")
        if actor_role != "risk_reviewer":
            raise ValueError("Independent challenge must be completed by actor_role=risk_reviewer")
        if lead and actor == lead:
            raise ValueError("Lead analyst cannot complete the independent challenge")
        if not any(str(reference).lower().startswith("risk_note:") for reference in references):
            raise ValueError("Independent challenge requires a retained risk_note reference")
    overrides = dict(case.get("task_overrides", {}))
    overrides[task_id] = {
        "status": "completed",
        "actor": actor,
        "actor_role": actor_role,
        "notes": str(notes or ""),
        "references": references,
        "completed_at": now_iso(),
    }
    case["task_overrides"] = overrides
    if references:
        _attach_workpapers(case, references, actor, actor_role, notes)


def _request_evidence(case: Dict[str, Any], notes: str, actor: str, actor_role: str) -> None:
    request = str(notes or "").strip()
    if not request:
        raise ValueError("notes must describe the evidence request")
    requests = list(case.get("manual_evidence_requests", []))
    request_id = "evidence_request:{0}:{1:03d}".format(case.get("case_id"), len(requests) + 1)
    requests.append({
        "request_id": request_id,
        "request": request,
        "status": "open",
        "requested_by": actor,
        "actor_role": actor_role,
        "requested_at": now_iso(),
        "resolved_at": "",
        "references": [],
    })
    case["manual_evidence_requests"] = requests
    _invalidate_downstream_approvals(case, "evidence requested after review")


def _resolve_evidence_request(
    case: Dict[str, Any],
    request_id: str,
    references: List[str],
    actor: str,
    actor_role: str,
    notes: str,
) -> None:
    if not references:
        raise ValueError("evidence request resolution requires at least one reference")
    found = False
    for item in case.get("manual_evidence_requests", []):
        if item.get("request_id") == request_id:
            if item.get("status") != "open":
                raise ValueError("Evidence request is not open: {0}".format(request_id))
            item["status"] = "resolved"
            item["resolved_by"] = actor
            item["resolved_actor_role"] = actor_role
            item["resolved_at"] = now_iso()
            item["references"] = references
            item["resolution_notes"] = str(notes or "")
            found = True
            break
    if not found:
        raise ValueError("Unknown evidence request: {0}".format(request_id))
    _attach_workpapers(case, references, actor, actor_role, notes)


def _quality_approve(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "quality_approve", actor, actor_role)
    gates = case.get("gates", {}) or {}
    for gate_name in ["evidence", "analysis", "challenge"]:
        if (gates.get(gate_name, {}) or {}).get("status") != "PASS":
            raise ValueError("Cannot approve quality before {0} gate passes".format(gate_name))
    if not case.get("workpaper_manifest"):
        raise ValueError("Cannot approve quality without retained workpapers")
    case["approvals"]["quality_review"] = _approval_record("approved", actor, actor_role, notes)


def _quality_reject(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "quality_reject", actor, actor_role)
    if not str(notes or "").strip():
        raise ValueError("quality rejection requires notes")
    case["approvals"]["quality_review"] = _approval_record("rejected", actor, actor_role, notes)
    case["approvals"]["compliance_review"] = _pending_approval("invalidated by quality rejection")
    case["approvals"]["publication"] = _pending_approval("invalidated by quality rejection")


def _compliance_clear(
    case: Dict[str, Any],
    actor: str,
    actor_role: str,
    notes: str,
    checks: List[str],
    cfg: Dict[str, Any],
) -> None:
    _require_protected_actor(case, "compliance_clear", actor, actor_role)
    if (case.get("approvals", {}).get("quality_review", {}) or {}).get("status") != "approved":
        raise ValueError("Quality review must be approved before compliance clearance")
    required = set(cfg.get("required_compliance_checks", DEFAULT_COMPLIANCE_CHECKS))
    missing = sorted(required.difference(checks))
    if missing:
        raise ValueError("Missing required compliance checks: {0}".format(", ".join(missing)))
    record = _approval_record("approved", actor, actor_role, notes)
    record["checks"] = sorted(set(checks))
    case["approvals"]["compliance_review"] = record


def _compliance_block(
    case: Dict[str, Any],
    actor: str,
    actor_role: str,
    notes: str,
    checks: List[str],
) -> None:
    _require_protected_actor(case, "compliance_block", actor, actor_role)
    if not str(notes or "").strip():
        raise ValueError("compliance block requires notes")
    record = _approval_record("blocked", actor, actor_role, notes)
    record["checks"] = sorted(set(checks))
    case["approvals"]["compliance_review"] = record
    case["approvals"]["publication"] = _pending_approval("invalidated by compliance block")


def _publication_approve(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "publication_approve", actor, actor_role)
    approvals = case.get("approvals", {}) or {}
    if (approvals.get("quality_review", {}) or {}).get("status") != "approved":
        raise ValueError("Quality review approval is required")
    if (approvals.get("compliance_review", {}) or {}).get("status") != "approved":
        raise ValueError("Compliance clearance is required")
    record = _approval_record("approved", actor, actor_role, notes)
    record["scope"] = "internal_research_only"
    case["approvals"]["publication"] = record
    case["published_at"] = now_iso()


def _publication_reject(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "publication_reject", actor, actor_role)
    if not str(notes or "").strip():
        raise ValueError("publication rejection requires notes")
    case["approvals"]["publication"] = _approval_record("rejected", actor, actor_role, notes)


def _start_monitoring(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "start_monitoring", actor, actor_role)
    if (case.get("approvals", {}).get("publication", {}) or {}).get("status") != "approved":
        raise ValueError("Publication must be approved before monitoring starts")
    case["monitoring_started_at"] = now_iso()
    case["monitoring_notes"] = str(notes or "")


def _close_case(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "close_case", actor, actor_role)
    if not str(notes or "").strip():
        raise ValueError("closing a case requires a reason")
    case["closed_at"] = now_iso()
    case["close_reason"] = str(notes)


def _reopen_case(case: Dict[str, Any], actor: str, actor_role: str, notes: str) -> None:
    _require_protected_actor(case, "reopen_case", actor, actor_role)
    case["closed_at"] = ""
    case["close_reason"] = ""
    case["monitoring_started_at"] = ""
    _invalidate_downstream_approvals(case, str(notes or "case reopened for new material evidence"))


def _require_protected_actor(case: Dict[str, Any], action: str, actor: str, actor_role: str) -> None:
    required_role = PROTECTED_ACTION_ROLES[action]
    if actor_role != required_role:
        raise ValueError("{0} requires actor_role={1}".format(action, required_role))
    if _is_automated_actor(actor):
        raise ValueError("LLM, Agent, and system actors cannot pass protected human gates")
    assignments = case.get("assignments", {}) or {}
    assignment_key = {
        "quality_reviewer": "quality_reviewer",
        "compliance_officer": "compliance_officer",
        "research_supervisor": "publication_approver",
    }[required_role]
    assigned = str(assignments.get(assignment_key) or "")
    if not assigned:
        raise ValueError("Controlled role is not assigned: {0}".format(assignment_key))
    if assigned != actor:
        raise ValueError("Action actor must match assigned {0}".format(assignment_key))
    _validate_assignment_separation(assignments)


def _validate_assignment_separation(assignments: Dict[str, Any]) -> None:
    controlled = [
        str(assignments.get("lead_analyst") or "").strip(),
        str(assignments.get("quality_reviewer") or "").strip(),
        str(assignments.get("compliance_officer") or "").strip(),
        str(assignments.get("publication_approver") or "").strip(),
    ]
    values = [item for item in controlled if item]
    if len(values) != len(set(values)):
        raise ValueError("Lead analyst, quality reviewer, compliance officer, and approver must be separated")


def _invalidate_downstream_approvals(case: Dict[str, Any], reason: str) -> None:
    approvals = case.setdefault("approvals", {})
    approvals["quality_review"] = _pending_approval(reason)
    approvals["compliance_review"] = _pending_approval(reason)
    approvals["publication"] = _pending_approval(reason)
    case["published_at"] = ""
    case["monitoring_started_at"] = ""


def _runtime_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("research_runtime", {}) if isinstance(config, dict) else {}
    operations = config.get("research_operations", {}) if isinstance(config, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "auto_sync": bool(raw.get("auto_sync", True)),
        "retention_years": max(5, int(raw.get("retention_years", 5) or 5)),
        "require_primary_source_for": _clean_list(raw.get("require_primary_source_for", ["P0", "P1"])),
        "required_compliance_checks": _clean_list(raw.get("required_compliance_checks", DEFAULT_COMPLIANCE_CHECKS)),
        "external_publication_enabled": bool(raw.get("external_publication_enabled", False)),
        "human_approval_required": True,
        "auto_publish": False,
        "require_recorded_challenge": bool(operations.get("require_recorded_challenge", False)),
    }


def _runtime_policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scope": "internal_research_only",
        "retention_years": cfg.get("retention_years", 5),
        "human_approval_required": True,
        "independent_quality_review_required": True,
        "compliance_review_required": True,
        "role_separation_required": True,
        "required_compliance_checks": cfg.get("required_compliance_checks", DEFAULT_COMPLIANCE_CHECKS),
        "external_publication_enabled": bool(cfg.get("external_publication_enabled", False)),
        "auto_publish": False,
        "auto_trade": False,
        "recorded_independent_challenge_required": bool(cfg.get("require_recorded_challenge", False)),
    }


def _runtime_model_operations(config: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = load_latest_model_operations(config)
    if not snapshot:
        return {}
    return {
        "version": snapshot.get("version", ""),
        "generated_at": snapshot.get("generated_at", ""),
        "run_id": snapshot.get("run_id", ""),
        "status": snapshot.get("status", "UNKNOWN"),
        "quality": snapshot.get("quality", {}),
        "deadline_decisions": snapshot.get("deadline_decisions", {}),
        "human_control": snapshot.get("human_control", {}),
        "safety_boundary": snapshot.get("safety_boundary", ""),
    }


def _runtime_result(
    runtime: Dict[str, Any],
    summary: Dict[str, Any],
    paths: Dict[str, str],
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result = {
        "research_runtime_enabled": True,
        "research_runtime_case_count": summary.get("case_count", 0),
        "research_runtime_stage_counts": summary.get("stage_counts", {}),
        "research_runtime_blocked_count": summary.get("blocked_count", 0),
        "research_runtime_pending_human_count": summary.get("pending_human_count", 0),
        "research_runtime_monitoring_count": summary.get("monitoring_count", 0),
        "research_runtime": summary,
        "research_runtime_cases": runtime.get("cases", []),
        **paths,
    }
    if diagnostics is not None:
        result["research_runtime_diagnostics"] = diagnostics
        result["research_runtime_degraded"] = bool(diagnostics.get("degraded"))
    return result


def _disabled_result() -> Dict[str, Any]:
    return {
        "research_runtime_enabled": False,
        "research_runtime_case_count": 0,
        "research_runtime_stage_counts": {},
        "research_runtime_blocked_count": 0,
        "research_runtime_pending_human_count": 0,
        "research_runtime_monitoring_count": 0,
        "research_runtime": {},
        "research_runtime_cases": [],
        "research_runtime_diagnostics": _new_diagnostics(),
        "research_runtime_degraded": False,
    }


def _new_diagnostics() -> Dict[str, Any]:
    """Structured degradation diagnostics for one research-runtime sync."""
    return {
        "requested_cases": 0,
        "loaded_cases": 0,
        "requested_signal_count": 0,
        "loaded_signal_count": 0,
        "missing_signal_ids": [],
        "preserved_cases": [],
        "backfill_unavailable": False,
        "empty_input_preserved": False,
        "degraded": False,
        "degradation_reasons": [],
    }


def _empty_input_preserved_diagnostics(previous_cases: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    diagnostics = _new_diagnostics()
    diagnostics["loaded_cases"] = 0
    diagnostics["requested_signal_count"] = 0
    diagnostics["loaded_signal_count"] = 0
    diagnostics["preserved_cases"] = sorted(str(case_id) for case_id in previous_cases)
    diagnostics["empty_input_preserved"] = True
    diagnostics["degraded"] = True
    diagnostics["degradation_reasons"] = [
        "case input is empty while the runtime already contains {0} case(s); refusing to erase existing research state".format(
            len(previous_cases)
        )
    ]
    return diagnostics


def _case_missing_signal_ids(source_case: Dict[str, Any], signal_map: Dict[str, Dict[str, Any]]) -> List[str]:
    return [
        signal_id
        for signal_id in source_case.get("signal_ids", [])
        if str(signal_id).strip() and str(signal_id) not in signal_map
    ]


def _save_runtime(runtime: Dict[str, Any]) -> None:
    path = project_path("data/research_runtime/runtime.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(runtime, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(str(temp), str(path))


def _append_runtime_event(
    event_type: str,
    case: Dict[str, Any],
    *,
    actor: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    occurred_at = now_iso()
    event = {
        "event_id": "runtime:{0}".format(hashlib.sha1("{0}|{1}|{2}".format(event_type, case.get("case_id"), occurred_at).encode("utf-8")).hexdigest()[:20]),
        "occurred_at": occurred_at,
        "event_type": event_type,
        "entity_type": "research_case_runtime",
        "entity_id": case.get("case_id", ""),
        "actor": actor,
        "payload": {
            "stage": case.get("stage", ""),
            "top_priority": case.get("top_priority", ""),
            **(payload or {}),
        },
        "safety_boundary": SAFETY_BOUNDARY,
    }
    path = project_path("data/research_runtime/events_{0}.jsonl".format(occurred_at[:10].replace("-", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    try:
        from . import brokerage_architecture

        original = brokerage_architecture.project_path
        brokerage_architecture.project_path = project_path
        try:
            brokerage_architecture.append_event(
                event_type,
                "research_case_runtime",
                str(case.get("case_id") or ""),
                event["payload"],
                source="research_runtime",
                actor=actor,
                correlation_id=str(case.get("case_id") or ""),
            )
        finally:
            brokerage_architecture.project_path = original
    except Exception:
        pass


def _render_runtime_markdown(runtime: Dict[str, Any], summary: Dict[str, Any]) -> str:
    lines = ["# 机构级投研案件运行台", ""]
    lines.append("- 生成时间: {0}".format(summary.get("generated_at", "")))
    lines.append("- 运行时版本: {0}".format(summary.get("runtime_version", RUNTIME_VERSION)))
    lines.append("- 案件数量: {0}".format(summary.get("case_count", 0)))
    lines.append("- 阻塞案件: {0}".format(summary.get("blocked_count", 0)))
    lines.append("- 等待人工门禁: {0}".format(summary.get("pending_human_count", 0)))
    lines.append("- 输出边界: 仅供内部研究，不自动交易、不下单、不调仓。")
    degradation = runtime.get("degradation") or {}
    if degradation.get("degraded"):
        lines.append("- 运行状态: 降级（已保留既有案件与证据，未用缺失输入覆盖）")
        lines.append("")
        lines.append("## 降级诊断")
        lines.append("")
        for reason in degradation.get("degradation_reasons", []) or []:
            lines.append("- {0}".format(reason))
        lines.append("")
    lines.append("")
    lines.append("## 商业运行顺序")
    lines.append("")
    lines.append("- intake -> triage -> evidence_collection -> analysis -> challenge")
    lines.append("- challenge -> quality_review -> compliance_review -> human_approval")
    lines.append("- human_approval -> published -> monitoring -> closed")
    lines.append("")
    model_ops = summary.get("model_operations", {}) or {}
    lines.append("## Model Operations Gate")
    lines.append("")
    if model_ops:
        quality = model_ops.get("quality", {}) or {}
        grounding = quality.get("grounding", {}) or {}
        lines.append("- status: {0}".format(model_ops.get("status", "UNKNOWN")))
        lines.append("- run_id: {0}".format(model_ops.get("run_id", "")))
        lines.append("- main_success_rate: {0:.1%}".format(float(quality.get("main_success_rate", 1))))
        lines.append("- role_success_rate: {0:.1%}".format(float(quality.get("role_success_rate", 1))))
        lines.append("- usable_grounding_rate: {0:.1%}".format(float(grounding.get("usable_rate", 1))))
        lines.append("- boundary: model quality can block or defer research; it cannot approve, publish, assign, or trade")
    else:
        lines.append("- no model operations snapshot")
    lines.append("- 任一门禁可退回补证、重做分析或停止发布；系统和 LLM 不得通过受保护门禁。")
    lines.append("")
    review_queue = summary.get("review_queue", {}) or {}
    lines.append("## Human Review Queue")
    lines.append("")
    if review_queue:
        lines.append("- outstanding / overdue / at_risk / unassigned: {0} / {1} / {2} / {3}".format(
            review_queue.get("outstanding_count", 0), review_queue.get("overdue_count", 0),
            review_queue.get("at_risk_count", 0), review_queue.get("unassigned_count", 0),
        ))
        lines.append("- assignment_mode: suggest_only; automatic_assignment: false")
    else:
        lines.append("- no canonical review queue snapshot")
    lines.append("")
    market_gate = summary.get("market_data_governance", {}) or {}
    lines.append("## Market Data Gate")
    lines.append("")
    if market_gate:
        lines.append("- status: {0}; pass/degraded/blocked: {1}/{2}/{3}; llm_eligible: {4}".format(
            market_gate.get("status", "UNKNOWN"), market_gate.get("pass_count", 0),
            market_gate.get("degraded_count", 0), market_gate.get("blocked_count", 0),
            market_gate.get("llm_eligible_count", 0),
        ))
        lines.append("- stale, conflicting, unattested, or unverified quotes cannot enter the normal external-LLM path")
    else:
        lines.append("- no market data governance snapshot")
    lines.append("")
    lines.append("## 阶段分布")
    lines.append("")
    for stage, count in summary.get("stage_counts", {}).items():
        lines.append("- {0}: {1}".format(stage, count))
    if not summary.get("stage_counts"):
        lines.append("- 暂无案件。")
    lines.append("")
    lines.append("## 今日操作队列")
    lines.append("")
    queue = summary.get("next_action_queue", [])
    if not queue:
        lines.append("- 暂无待处理案件。")
    for item in queue:
        lines.append("- [{0}] {1} / {2}: {3}".format(
            item.get("priority", ""),
            item.get("case_id", ""),
            item.get("stage", ""),
            "；".join(item.get("next_actions", [])[:3]) or "继续监控",
        ))
    lines.append("")
    lines.append("## 案件明细")
    lines.append("")
    for case in runtime.get("cases", []):
        lines.append("### [{0}] {1}".format(case.get("top_priority", ""), case.get("title", "")))
        lines.append("")
        lines.append("- case_id: {0}".format(case.get("case_id", "")))
        lines.append("- 当前阶段: {0}".format(case.get("stage", "")))
        lines.append("- lead/reviewer/compliance/approver: {0}/{1}/{2}/{3}".format(
            (case.get("assignments", {}) or {}).get("lead_analyst", "未分配"),
            (case.get("assignments", {}) or {}).get("quality_reviewer", "未分配"),
            (case.get("assignments", {}) or {}).get("compliance_officer", "未分配"),
            (case.get("assignments", {}) or {}).get("publication_approver", "未分配"),
        ))
        if case.get("blockers"):
            lines.append("- 阻塞项: {0}".format("；".join(case.get("blockers", [])[:8])))
        if case.get("next_actions"):
            lines.append("- 下一步: {0}".format("；".join(case.get("next_actions", [])[:6])))
        lines.append("")
    lines.append("## 档案与合规策略")
    lines.append("")
    policy = summary.get("policy", {})
    lines.append("- 工作底稿与审批记录保留年限: {0}".format(policy.get("retention_years", 5)))
    lines.append("- 独立质量审核: required")
    lines.append("- 合规审核: required")
    lines.append("- 人工最终批准: required")
    lines.append("- 自动发布: disabled")
    lines.append("- 外部发布: {0}".format("enabled" if policy.get("external_publication_enabled") else "disabled"))
    return "\n".join(lines)


def _load_report(report_path: str) -> Dict[str, Any]:
    if report_path:
        path = project_path(report_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}
    report_dir = project_path("reports/json")
    files = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    if not files:
        return {}
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _signal_map(signals: Sequence[Any]) -> Dict[str, Dict[str, Any]]:
    rows = {}
    for signal in signals:
        if isinstance(signal, dict):
            item = signal
        elif hasattr(signal, "to_dict"):
            item = signal.to_dict()
        else:
            continue
        signal_id = str(item.get("signal_id") or "")
        if signal_id:
            rows[signal_id] = item
    return rows


def _load_persisted_signals(config: Dict[str, Any], signal_ids: Sequence[str]) -> List[Dict[str, Any]]:
    sqlite_value = str((config.get("storage", {}) or {}).get("sqlite_path") or "data/stock_agent.sqlite3")
    sqlite_path = project_path(sqlite_value)
    if not sqlite_path.exists():
        return []
    try:
        from .storage import SignalStore

        store = SignalStore(sqlite_path)
        try:
            return store.fetch_many(signal_ids)
        finally:
            store.close()
    except Exception:
        return []


def _source_case_snapshot(case: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "case_id": case.get("case_id", ""),
        "title": case.get("title", ""),
        "status": case.get("status", "active"),
        "latest_signal_at": case.get("latest_signal_at", ""),
        "signal_count": case.get("signal_count", 0),
        "case_analysis": case.get("case_analysis", {}),
        "case_error": case.get("case_error", ""),
        "case_decision": case.get("case_decision", {}),
    }


def _has_market_snapshot(payload: Dict[str, Any]) -> bool:
    has_symbol = any(payload.get(key) not in (None, "") for key in ["symbol", "code", "name"])
    has_value = any(payload.get(key) not in (None, "") for key in ["latest", "pct_change", "price_change", "amount", "volume"])
    return has_symbol and has_value


def _is_material_evidence_gap(value: Any, source_case: Dict[str, Any]) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.lower().replace(" ", "_")
    non_blocking_exact = {
        "human_review_required",
        "topic_not_matched",
        "risk_points",
        "evidence_gaps",
        "rule_llm_analysis.key_thesis",
        "rule_llm_analysis.risk_points",
    }
    if normalized in non_blocking_exact:
        return False
    if normalized.startswith("source_quality.") or normalized.startswith("rule_llm_analysis."):
        return False
    if normalized.startswith("evidence.") and str(source_case.get("entity") or "unknown") != "unknown":
        return False
    if "=" in text and not any(token in normalized for token in ["missing", "unknown", "缺失", "未知"]):
        return False
    return True


def _is_hard_evidence_gap(value: Any) -> bool:
    """Separate provenance failures from ordinary due-diligence questions."""
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.lower().replace(" ", "_")
    hard_tokens = (
        "missing_url",
        "source_error",
        "collector_exception",
        "no_records",
        "missing_fields",
        "coverage:",
        "unresolved_ref",
        "official_filing",
        "original_source",
        "source_document",
        "原始来源",
        "原始链接",
        "原始记录",
        "公告链接",
        "公告原文",
        "监管文件",
        "来源缺失",
        "缺少来源",
        "缺乏来源",
        "数据源异常",
        "字段缺失",
        "未提供链接",
    )
    return any(token in normalized for token in hard_tokens)


def _workpaper_manifest(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    facts = case.get("evidence_facts", {}) or {}
    for reference in facts.get("traceable_sources", []) or []:
        rows.append({"kind": "source_document", "reference": reference, "origin": "signal"})
    for reference in facts.get("primary_sources", []) or []:
        rows.append({"kind": "official_filing", "reference": reference, "origin": "signal"})
    for reference in facts.get("analysis_refs", []) or []:
        rows.append({"kind": "research_note", "reference": reference, "origin": "analysis"})
    rows.extend(case.get("manual_workpapers", []) or [])
    seen = set()
    unique_rows = []
    for item in rows:
        reference = str(item.get("reference") or "")
        key = (str(item.get("kind") or ""), reference)
        if not reference or key in seen:
            continue
        seen.add(key)
        unique_rows.append(item)
    return unique_rows


def _case_blockers(case: Dict[str, Any], gates: Dict[str, Dict[str, Any]]) -> List[str]:
    stage = case.get("stage")
    stage_gate = {
        "intake": "intake",
        "triage": "triage",
        "evidence_collection": "evidence",
        "analysis": "analysis",
        "challenge": "challenge",
        "quality_review": "quality_review",
        "compliance_review": "compliance_review",
        "human_approval": "publication",
    }.get(stage)
    blockers = list((gates.get(stage_gate, {}) or {}).get("reasons", [])) if stage_gate else []
    assignments = case.get("assignments", {}) or {}
    if stage == "quality_review" and not assignments.get("quality_reviewer"):
        blockers.append("quality reviewer is not assigned")
    if stage == "compliance_review" and not assignments.get("compliance_officer"):
        blockers.append("compliance officer is not assigned")
    if stage == "human_approval" and not assignments.get("publication_approver"):
        blockers.append("publication approver is not assigned")
    return _unique(blockers)


def _case_next_actions(case: Dict[str, Any]) -> List[str]:
    stage = str(case.get("stage") or "")
    assignments = case.get("assignments", {}) or {}
    actions = []
    if not assignments.get("lead_analyst") and stage not in {"closed", "monitoring"}:
        actions.append("assign a lead analyst")
    if stage == "intake":
        actions.append("identify covered entity and research mandate")
    elif stage == "triage":
        actions.append("classify priority, scope, and required workstreams")
    elif stage == "evidence_collection":
        actions.append("attach official source documents and resolve evidence requests")
    elif stage == "analysis":
        actions.append("complete base thesis, assumptions, and downside analysis")
        question_count = len((case.get("evidence_facts", {}) or {}).get("analysis_questions", []) or [])
        if question_count:
            actions.append("address prioritized due-diligence questions ({0} open)".format(question_count))
    elif stage == "challenge":
        actions.append("complete independent falsification and risk challenge")
    elif stage == "quality_review":
        if not assignments.get("quality_reviewer"):
            actions.append("assign an independent quality reviewer")
        actions.append("quality reviewer approves or returns the workpaper")
    elif stage == "compliance_review":
        if not assignments.get("compliance_officer"):
            actions.append("assign a compliance officer")
        actions.append("complete conflict, inside-information, quiet-period, and disclosure checks")
    elif stage == "human_approval":
        if not assignments.get("publication_approver"):
            actions.append("assign the authorized human approver")
        actions.append("authorized human approves internal publication")
    elif stage == "published":
        actions.append("start post-publication monitoring")
    elif stage == "monitoring":
        actions.append("track thesis changes, new evidence, and review outcomes")
    return _unique(actions)


def _compact_case(case: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "case_id": case.get("case_id", ""),
        "title": case.get("title", ""),
        "priority": case.get("top_priority", ""),
        "stage": case.get("stage", ""),
        "blockers": case.get("blockers", []),
        "next_actions": case.get("next_actions", []),
    }


def _queue_item(case: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_case(case)
    compact["human_gate_required"] = bool(case.get("human_gate_required"))
    compact["assignments"] = case.get("assignments", {})
    return compact


def _gate(status: str, reasons: Iterable[str]) -> Dict[str, Any]:
    return {"status": status, "reasons": _unique(reasons), "evaluated_at": now_iso()}


def _approval_gate(approval: Dict[str, Any], pending_reason: str) -> Dict[str, Any]:
    status = str(approval.get("status") or "pending")
    if status == "approved":
        return _gate("PASS", [])
    if status in {"rejected", "blocked"}:
        return _gate("BLOCKED", [str(approval.get("notes") or status)])
    return _gate("PENDING", [pending_reason])


def _normalized_approvals(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "quality_review": dict(raw.get("quality_review", _pending_approval("not reviewed"))),
        "compliance_review": dict(raw.get("compliance_review", _pending_approval("not reviewed"))),
        "publication": dict(raw.get("publication", _pending_approval("not reviewed"))),
    }


def _pending_approval(reason: str) -> Dict[str, Any]:
    return {"status": "pending", "actor": "", "actor_role": "", "notes": reason, "decided_at": ""}


def _approval_record(status: str, actor: str, actor_role: str, notes: str) -> Dict[str, Any]:
    return {
        "status": status,
        "actor": actor,
        "actor_role": actor_role,
        "notes": str(notes or ""),
        "decided_at": now_iso(),
        "human_confirmed": True,
    }


def _workpaper_kind(reference: str) -> str:
    prefix = str(reference or "").split(":", 1)[0].strip().lower()
    allowed = {
        "official_filing",
        "source_document",
        "meeting_note",
        "analysis_model",
        "research_note",
        "risk_note",
        "market_data",
        "conflict_disclosure",
    }
    return prefix if prefix in allowed else "source_document"


def _workpaper_count(manifest: Sequence[Dict[str, Any]], kinds: set) -> int:
    return sum(1 for item in manifest if str(item.get("kind") or "") in kinds)


def _task_completed(overrides: Dict[str, Any], task_id: str) -> bool:
    item = overrides.get(task_id, {}) if isinstance(overrides.get(task_id), dict) else {}
    return item.get("status") == "completed"


def _recorded_independent_challenge(case: Dict[str, Any]) -> bool:
    overrides = case.get("task_overrides", {}) or {}
    item = overrides.get("independent_challenge", {})
    if not isinstance(item, dict) or item.get("status") != "completed":
        return False
    lead = str((case.get("assignments", {}) or {}).get("lead_analyst") or "")
    actor = str(item.get("actor") or "")
    references = item.get("references", []) or []
    return (
        bool(actor)
        and str(item.get("actor_role") or "") == "risk_reviewer"
        and (not lead or actor != lead)
        and any(str(reference).lower().startswith("risk_note:") for reference in references)
    )


def _dependency_incomplete(rows: Sequence[Dict[str, Any]], dependency: str) -> bool:
    item = next((row for row in rows if row.get("task_id") == dependency), None)
    return bool(item and item.get("status") not in {"completed", "not_required"})


def _assignment_key_for_owner(owner_role: str) -> str:
    mapping = {
        "lead_analyst": "lead_analyst",
        "quality_reviewer": "quality_reviewer",
        "compliance_officer": "compliance_officer",
        "research_supervisor": "publication_approver",
    }
    return mapping.get(owner_role, owner_role)


def _is_automated_actor(actor: str) -> bool:
    normalized = str(actor or "").strip().lower().replace("-", "_")
    return any(normalized.startswith(prefix) for prefix in AUTOMATED_ACTOR_PREFIXES)


def _action_id(case_id: str, action: str) -> str:
    value = "{0}|{1}|{2}".format(case_id, action, now_iso())
    return "runtime_action:{0}".format(hashlib.sha1(value.encode("utf-8")).hexdigest()[:18])


def _runtime_case_sort_key(case: Dict[str, Any]) -> Tuple[int, int, str]:
    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(str(case.get("top_priority")), 9)
    return (priority_rank, _stage_rank(str(case.get("stage") or "")), str(case.get("case_id") or ""))


def _stage_rank(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return len(STAGE_ORDER)


def _clean_list(values: Iterable[Any]) -> List[str]:
    return _unique(str(item).strip() for item in values if str(item).strip())


def _unique(values: Iterable[Any]) -> List[str]:
    rows = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def _compact_time(value: Any) -> str:
    return str(value or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
