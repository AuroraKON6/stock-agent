"""Canonical human-review queue projected from append-only review artifacts.

Historical pending/completed JSONL files remain immutable. Queue state is a
materialized projection of those files plus append-only human action records.
No function in this module trades, publishes, approves research, or assigns a
person automatically.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import project_path
from .models import now_iso
from .research_operations import (
    add_business_hours,
    business_hours_elapsed,
    load_operations_inputs,
    operations_config,
)


REVIEW_QUEUE_VERSION = "23.0"
OPEN_STATUSES = {"pending", "in_review", "waiting_evidence", "deferred"}
CLOSED_STATUSES = {"completed", "superseded"}
ACTION_ROLES = {
    "start_review": {"lead_analyst"},
    "request_more_data": {"lead_analyst"},
    "defer_review": {"lead_analyst"},
    "complete_review": {"lead_analyst"},
    "reopen_review": {"research_coordinator", "research_supervisor"},
    "supersede_review": {"research_coordinator", "research_supervisor"},
}
TRANSITIONS = {
    "pending": {"start_review": "in_review", "request_more_data": "waiting_evidence", "defer_review": "deferred", "supersede_review": "superseded"},
    "in_review": {"request_more_data": "waiting_evidence", "defer_review": "deferred", "complete_review": "completed", "supersede_review": "superseded"},
    "waiting_evidence": {"start_review": "in_review", "defer_review": "deferred", "supersede_review": "superseded"},
    "deferred": {"reopen_review": "pending", "supersede_review": "superseded"},
    "completed": {"reopen_review": "pending"},
    "superseded": {"reopen_review": "pending"},
}
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class ReviewQueuePolicyError(ValueError):
    pass


def review_queue_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("review_queue", {}) if isinstance(config, dict) else {}
    sla = raw.get("sla_hours", {}) if isinstance(raw.get("sla_hours"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "enforce_roster": bool(raw.get("enforce_roster", True)),
        "assignment_mode": "suggest_only",
        "review_dir": str(raw.get("review_dir") or "data/review"),
        "report_dir": str(raw.get("report_dir") or "reports/review_queue"),
        "index_path": str(raw.get("index_path") or "data/review/queue_index.json"),
        "actions_dir": str(raw.get("actions_dir") or "data/review/queue_actions"),
        "at_risk_ratio": min(0.95, max(0.5, float(raw.get("at_risk_ratio", 0.8) or 0.8))),
        "sla_hours": {
            "P0": max(0.25, float(sla.get("P0", 0.5) or 0.5)),
            "P1": max(0.25, float(sla.get("P1", 2) or 2)),
            "P2": max(0.25, float(sla.get("P2", 8) or 8)),
            "P3": max(0.25, float(sla.get("P3", 24) or 24)),
        },
    }


def build_review_queue(config: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    cfg = review_queue_config(config)
    current = now or datetime.now().astimezone()
    review_dir = project_path(cfg["review_dir"])
    pending, pending_meta = _read_review_records(review_dir.glob("pending_review_*.jsonl"), "pending")
    completed, completed_meta = _read_review_records(review_dir.glob("completed_review_*.jsonl"), "completed")
    actions_dir = project_path(cfg["actions_dir"])
    actions, action_meta = _read_review_records(actions_dir.glob("review_action_*.jsonl"), "action")

    pending_by_id = _group_by_signal_id(pending)
    completed_by_id = _group_by_signal_id(completed)
    actions_by_id = _group_by_signal_id(actions)
    signal_ids = sorted(set(pending_by_id) | set(completed_by_id) | set(actions_by_id))
    operations = load_operations_inputs(config)
    business_cfg = operations_config(config)
    items = []
    for signal_id in signal_ids:
        item = _project_item(
            signal_id,
            pending_by_id.get(signal_id, []),
            completed_by_id.get(signal_id, []),
            actions_by_id.get(signal_id, []),
            cfg,
            business_cfg,
            operations,
            current,
        )
        if item:
            items.append(item)

    items.sort(key=lambda item: (
        -int(item.get("escalation_level", 0)),
        PRIORITY_RANK.get(str(item.get("priority") or "P3"), 9),
        -float(item.get("sla_ratio", 0) or 0),
        str(item.get("created_at") or ""),
        str(item.get("signal_id") or ""),
    ))
    status_counts = Counter(str(item.get("status") or "pending") for item in items)
    sla_counts = Counter(str(item.get("sla_status") or "unknown") for item in items)
    outstanding = [item for item in items if item.get("status") in OPEN_STATUSES]
    summary = {
        "item_count": len(items),
        "outstanding_count": len(outstanding),
        "completed_count": status_counts.get("completed", 0),
        "superseded_count": status_counts.get("superseded", 0),
        "unassigned_count": sum(not item.get("assigned_to") for item in outstanding),
        "overdue_count": sla_counts.get("overdue", 0),
        "at_risk_count": sla_counts.get("at_risk", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "sla_status_counts": dict(sorted(sla_counts.items())),
        "pending_rows": pending_meta["row_count"],
        "pending_unique": len(pending_by_id),
        "pending_duplicate_rows": max(0, pending_meta["row_count"] - len(pending_by_id)),
        "pending_file_count": pending_meta["file_count"],
        "completed_rows": completed_meta["row_count"],
        "completed_unique": len(completed_by_id),
        "action_rows": action_meta["row_count"],
        "invalid_record_count": pending_meta["invalid_count"] + completed_meta["invalid_count"] + action_meta["invalid_count"],
        "human_principal_count": sum(
            item.get("principal_type") == "human" and item.get("active")
            for item in operations.get("principals", [])
        ),
        "missing_human_capabilities": operations.get("missing_mandatory_human_capabilities", []),
    }
    return {
        "version": REVIEW_QUEUE_VERSION,
        "generated_at": current.isoformat(timespec="seconds"),
        "enabled": cfg["enabled"],
        "assignment_mode": "suggest_only",
        "automatic_assignment_executed": False,
        "sla_policy_type": "internal_operating_policy_not_regulatory_deadline",
        "sla_hours": cfg["sla_hours"],
        "summary": summary,
        "items": items,
        "source_files": {
            "pending": pending_meta,
            "completed": completed_meta,
            "actions": action_meta,
        },
        "safety_boundary": "Human review support only. No automatic assignment, approval, publication, trading, order placement, or position change.",
    }


def write_review_queue_snapshot(config: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    cfg = review_queue_config(config)
    snapshot = build_review_queue(config, now=now)
    index_path = project_path(cfg["index_path"])
    report_dir = project_path(cfg["report_dir"])
    index_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_stamp(snapshot["generated_at"])
    json_path = report_dir / "review_queue_{0}.json".format(stamp)
    md_path = report_dir / "review_queue_{0}.md".format(stamp)
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    json_text = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    markdown = render_review_queue_markdown(snapshot)
    index_path.write_text(json_text, encoding="utf-8")
    json_path.write_text(json_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        "review_queue": snapshot,
        "review_queue_summary": snapshot["summary"],
        "review_queue_index_path": str(index_path),
        "review_queue_path": str(md_path),
        "review_queue_json_path": str(json_path),
        "review_queue_latest_path": str(latest_md),
        "review_queue_latest_json_path": str(latest_json),
    }


def load_latest_review_queue(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = review_queue_config(config or {})
    candidates = [
        project_path(str(Path(cfg["report_dir"]) / "latest.json")),
        project_path(cfg["index_path"]),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                return value
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def record_review_queue_action(
    config: Dict[str, Any],
    signal_id: str,
    action: str,
    actor: str,
    actor_role: str,
    *,
    notes: str = "",
    fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    signal_id = str(signal_id or "").strip()
    action = str(action or "").strip()
    actor = str(actor or "").strip()
    actor_role = str(actor_role or "").strip()
    if not signal_id or action not in ACTION_ROLES:
        raise ReviewQueuePolicyError("signal_id and a supported review action are required")
    queue = build_review_queue(config)
    item = next((row for row in queue.get("items", []) if row.get("signal_id") == signal_id), None)
    if item is None:
        raise ReviewQueuePolicyError("review queue item not found")
    current_status = str(item.get("status") or "pending")
    next_status = (TRANSITIONS.get(current_status, {}) or {}).get(action)
    if not next_status:
        raise ReviewQueuePolicyError("invalid review transition: {0} -> {1}".format(current_status, action))
    principal = _authorize_human_action(config, item, action, actor, actor_role)
    notes = str(notes or "").strip()
    if action in {"defer_review", "request_more_data", "supersede_review"} and not notes:
        raise ReviewQueuePolicyError("notes are required for {0}".format(action))
    values = dict(fields or {})
    # Review transitions may happen back-to-back in the workbench. Preserve
    # sub-second ordering so a later reopen cannot be hidden by a completion
    # recorded in the same wall-clock second.
    recorded_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    if action == "complete_review":
        decision = str(values.get("decision_taken") or "").strip()
        outcome = str(values.get("was_effective") or "unknown").strip().lower()
        if not decision:
            raise ReviewQueuePolicyError("decision_taken is required to complete review")
        if outcome not in {"true", "false", "unknown"}:
            raise ReviewQueuePolicyError("was_effective must be true, false, or unknown")
        completed = _completion_record(item, values, actor, actor_role, recorded_at)
        output_path = _write_unique_record(review_queue_config(config)["review_dir"], "completed_review", completed)
        record = completed
    else:
        record = {
            "event_id": "review-event:{0}".format(uuid.uuid4().hex),
            "signal_id": signal_id,
            "action": action,
            "from_status": current_status,
            "to_status": next_status,
            "actor": actor,
            "actor_role": actor_role,
            "principal_type": principal.get("principal_type", ""),
            "notes": notes,
            "recorded_at": recorded_at,
            "automatic_assignment_executed": False,
        }
        output_path = _write_unique_record(review_queue_config(config)["actions_dir"], "review_action", record)
    paths = write_review_queue_snapshot(config)
    return {"record": record, "record_path": output_path, **paths}


def render_review_queue_markdown(snapshot: Dict[str, Any]) -> str:
    summary = snapshot.get("summary", {}) or {}
    lines = [
        "# Human Review Queue",
        "",
        "- generated_at: {0}".format(snapshot.get("generated_at", "")),
        "- outstanding: {0}".format(summary.get("outstanding_count", 0)),
        "- overdue: {0}".format(summary.get("overdue_count", 0)),
        "- at_risk: {0}".format(summary.get("at_risk_count", 0)),
        "- unassigned: {0}".format(summary.get("unassigned_count", 0)),
        "- pending rows / unique / duplicates: {0} / {1} / {2}".format(
            summary.get("pending_rows", 0), summary.get("pending_unique", 0), summary.get("pending_duplicate_rows", 0)
        ),
        "- assignment_mode: suggest_only",
        "- safety: {0}".format(snapshot.get("safety_boundary", "")),
        "",
        "| Esc | Priority | SLA | Status | Signal | Title | Assigned | Suggested |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for item in snapshot.get("items", [])[:200]:
        lines.append("| {escalation} | {priority} | {sla} | {status} | {signal} | {title} | {assigned} | {suggested} |".format(
            escalation=item.get("escalation_level", 0),
            priority=item.get("priority", ""),
            sla=item.get("sla_status", ""),
            status=item.get("status", ""),
            signal=item.get("signal_id", ""),
            title=str(item.get("title", "")).replace("|", "/"),
            assigned=item.get("assigned_to", "") or "unassigned",
            suggested=item.get("assignment_recommendation", "") or "none",
        ))
    return "\n".join(lines) + "\n"


def _project_item(
    signal_id: str,
    pending: Sequence[Dict[str, Any]],
    completed: Sequence[Dict[str, Any]],
    actions: Sequence[Dict[str, Any]],
    cfg: Dict[str, Any],
    business_cfg: Dict[str, Any],
    operations: Dict[str, Any],
    current: datetime,
) -> Dict[str, Any]:
    records = sorted(list(pending), key=_record_sort_key)
    completions = sorted(list(completed), key=_record_sort_key)
    events = sorted(list(actions), key=_record_sort_key)
    base = dict(records[-1] if records else (completions[-1] if completions else {}))
    if not base:
        return {}
    status = "completed" if completions else "pending"
    assigned_to = ""
    last_action = ""
    latest_completion_at = _record_datetime(completions[-1]).isoformat(timespec="seconds") if completions else ""
    completion_time = _record_datetime(completions[-1]) if completions else None
    for event in events:
        event_time = _record_datetime(event)
        if completion_time and event_time <= completion_time:
            continue
        action = str(event.get("action") or "")
        projected = (TRANSITIONS.get(status, {}) or {}).get(action)
        if projected:
            status = projected
            last_action = action
            if action == "start_review":
                assigned_to = str(event.get("actor") or "")
            if action == "reopen_review":
                assigned_to = ""
    created = min((_record_datetime(row) for row in records), default=_record_datetime(base))
    priority = str(base.get("priority") or "P3").upper()
    if priority not in PRIORITY_RANK:
        priority = "P3"
    target = float(cfg["sla_hours"].get(priority, cfg["sla_hours"]["P3"]))
    due = add_business_hours(created, target, business_cfg)
    elapsed = business_hours_elapsed(created, current, business_cfg)
    ratio = elapsed / target if target else 0.0
    if status in CLOSED_STATUSES:
        sla_status = "closed"
    elif current > due:
        sla_status = "overdue"
    elif ratio >= cfg["at_risk_ratio"]:
        sla_status = "at_risk"
    else:
        sla_status = "on_track"
    escalation = {"P0": 3, "P1": 2, "P2": 1, "P3": 0}.get(priority, 0)
    if sla_status == "overdue":
        escalation = min(3, escalation + 1)
    eligible = _eligible_reviewers(base, operations)
    llm = base.get("llm_analysis", {}) if isinstance(base.get("llm_analysis"), dict) else {}
    questions = base.get("suggested_review_questions", []) or llm.get("review_questions", []) or []
    return {
        "queue_id": "review:{0}".format(signal_id),
        "signal_id": signal_id,
        "title": str(base.get("title") or ""),
        "priority": priority,
        "source": str(base.get("source") or ""),
        "source_type": str(base.get("source_type") or ""),
        "topics": _clean_list(base.get("topics", [])),
        "symbols": _clean_list(base.get("symbols", [])),
        "status": status,
        "created_at": created.isoformat(timespec="seconds"),
        "due_at": due.isoformat(timespec="seconds"),
        "closed_at": latest_completion_at,
        "target_business_hours": target,
        "elapsed_business_hours": round(elapsed, 2),
        "sla_ratio": round(ratio, 4),
        "sla_status": sla_status,
        "escalation_level": escalation,
        "assigned_to": assigned_to,
        "required_role": "lead_analyst",
        "assignment_recommendation": eligible[0] if eligible else "",
        "eligible_human_reviewers": eligible,
        "automatic_assignment_executed": False,
        "human_action_required": status in OPEN_STATUSES,
        "allowed_actions": sorted((TRANSITIONS.get(status, {}) or {}).keys()),
        "last_action": last_action,
        "pending_record_count": len(records),
        "completed_record_count": len(completions),
        "action_record_count": len(events),
        "duplicate_pending_records": max(0, len(records) - 1),
        "review_questions": [str(item) for item in questions[:10]],
        "key_thesis": str(llm.get("key_thesis") or ""),
        "confidence": llm.get("confidence"),
        "impact_direction": str(llm.get("impact_direction") or ""),
        "evidence_gap_count": len(base.get("evidence_gap_tasks", []) or []),
        "reasons": [str(item) for item in (base.get("reasons", []) or [])[:10]],
    }


def _authorize_human_action(
    config: Dict[str, Any], item: Dict[str, Any], action: str, actor: str, actor_role: str
) -> Dict[str, Any]:
    inputs = load_operations_inputs(config)
    if inputs.get("errors"):
        raise ReviewQueuePolicyError("research operations roster is invalid")
    principal = inputs.get("principal_map", {}).get(actor)
    if principal is None:
        raise ReviewQueuePolicyError("actor is not registered in the research operations roster")
    if principal.get("principal_type") != "human" or not principal.get("active"):
        raise ReviewQueuePolicyError("review queue actions require an active human principal")
    if actor_role not in ACTION_ROLES[action] or actor_role not in principal.get("roles", []):
        raise ReviewQueuePolicyError("actor role is not authorized for {0}".format(action))
    if actor_role == "lead_analyst" and not _verified_analyst(principal):
        raise ReviewQueuePolicyError("verified securities analyst registration is required")
    if actor_role == "lead_analyst" and str(principal.get("information_side") or "") != "public":
        raise ReviewQueuePolicyError("lead analyst must be on the public information side")
    symbols = set(_clean_list(item.get("symbols", [])))
    coverage = set(_clean_list(principal.get("coverage_entities", [])))
    if symbols and coverage and "*" not in coverage and not symbols.intersection(coverage):
        raise ReviewQueuePolicyError("review item is outside actor coverage")
    if symbols.intersection(set(_clean_list(principal.get("restricted_entities", [])))):
        raise ReviewQueuePolicyError("review item is restricted for actor")
    assigned = str(item.get("assigned_to") or "")
    if action in {"complete_review", "defer_review", "request_more_data"} and assigned and assigned != actor:
        raise ReviewQueuePolicyError("only the reviewing analyst may perform this transition")
    return principal


def _eligible_reviewers(record: Dict[str, Any], operations: Dict[str, Any]) -> List[str]:
    symbols = set(_clean_list(record.get("symbols", [])))
    eligible = []
    for principal in operations.get("principals", []) or []:
        if principal.get("principal_type") != "human" or not principal.get("active"):
            continue
        if "lead_analyst" not in principal.get("roles", []) or not _verified_analyst(principal):
            continue
        if str(principal.get("information_side") or "") != "public":
            continue
        coverage = set(_clean_list(principal.get("coverage_entities", [])))
        restricted = set(_clean_list(principal.get("restricted_entities", [])))
        if symbols.intersection(restricted):
            continue
        if symbols and coverage and "*" not in coverage and not symbols.intersection(coverage):
            continue
        eligible.append(str(principal.get("actor_id") or ""))
    return sorted(item for item in eligible if item)


def _verified_analyst(principal: Dict[str, Any]) -> bool:
    item = (principal.get("qualifications", {}) or {}).get("securities_analyst_registration", {})
    return (
        isinstance(item, dict)
        and str(item.get("status") or "").lower() == "verified"
        and bool(str(item.get("registration_id") or "").strip())
    )


def _completion_record(
    item: Dict[str, Any], values: Dict[str, Any], actor: str, actor_role: str, recorded_at: str
) -> Dict[str, Any]:
    return {
        "signal_id": item.get("signal_id", ""),
        "title": item.get("title", ""),
        "priority": item.get("priority", "P3"),
        "source": item.get("source", ""),
        "source_type": item.get("source_type", ""),
        "topics": item.get("topics", []),
        "symbols": item.get("symbols", []),
        "decision_taken": str(values.get("decision_taken") or "").strip(),
        "actual_price_move": str(values.get("actual_price_move") or "").strip(),
        "actual_news_followup": str(values.get("actual_news_followup") or "").strip(),
        "was_effective": str(values.get("was_effective") or "unknown").strip().lower(),
        "false_positive_reason": str(values.get("false_positive_reason") or "").strip(),
        "notes": str(values.get("notes") or "").strip(),
        "reviewed_at": recorded_at,
        "reviewed_by": actor,
        "reviewer_role": actor_role,
        "queue_action": "complete_review",
        "automatic_approval_executed": False,
    }


def _write_unique_record(directory: str, prefix: str, record: Dict[str, Any]) -> str:
    out_dir = project_path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _compact_stamp(str(record.get("recorded_at") or record.get("reviewed_at") or now_iso()))
    path = out_dir / "{0}_{1}_{2}.jsonl".format(prefix, stamp, uuid.uuid4().hex[:8])
    path.write_text(json.dumps(record, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return str(path)


def _read_review_records(files: Iterable[Path], kind: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    records = []
    invalid = 0
    paths = sorted(files)
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            invalid += 1
            continue
        for line_no, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid += 1
                continue
            if not isinstance(value, dict) or not str(value.get("signal_id") or "").strip():
                invalid += 1
                continue
            value = dict(value)
            value["_queue_meta"] = {
                "kind": kind,
                "path": str(path),
                "line": line_no,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            }
            records.append(value)
    return records, {"file_count": len(paths), "row_count": len(records), "invalid_count": invalid}


def _group_by_signal_id(records: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        signal_id = str(record.get("signal_id") or "").strip()
        if signal_id:
            grouped[signal_id].append(record)
    return dict(grouped)


def _record_datetime(record: Dict[str, Any]) -> datetime:
    for key in ("recorded_at", "reviewed_at", "exported_at", "created_at"):
        parsed = _parse_datetime(record.get(key))
        if parsed is not None:
            return parsed
    meta = record.get("_queue_meta", {}) if isinstance(record.get("_queue_meta"), dict) else {}
    parsed = _parse_datetime(meta.get("mtime"))
    return parsed or datetime.now().astimezone()


def _record_sort_key(record: Dict[str, Any]) -> Tuple[str, str, int]:
    meta = record.get("_queue_meta", {}) if isinstance(record.get("_queue_meta"), dict) else {}
    return (_record_datetime(record).isoformat(), str(meta.get("path") or ""), int(meta.get("line") or 0))


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone()


def _clean_list(values: Any) -> List[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _compact_stamp(value: str) -> str:
    return str(value or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
