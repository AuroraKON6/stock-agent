from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

from .config import project_path
from .models import now_iso


SAFE_BOUNDARY_NOTE = "仅用于研究、复核、补证和审计；禁止自动执行委托或仓位调整。"

REMEDIATION_EVENT_BY_STATUS = {
    "open": "remediation_created",
    "resolved": "remediation_resolved",
    "rejected": "remediation_rejected",
    "deferred": "remediation_deferred",
}

TEST_QUARANTINE_EVENT = "test_artifact_quarantined"
TEST_QUARANTINE_ENTITY_TYPES = {"run", "signal", "remediation_task", "research_case"}


def append_event(
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: Dict[str, Any] | None = None,
    *,
    source: str = "stock_agent",
    actor: str = "system",
    correlation_id: str = "",
    idempotency_key: str = "",
) -> Dict[str, Any]:
    occurred_at = now_iso()
    semantic_key = str(idempotency_key or "")
    event_id = _event_id_from_key(semantic_key) if semantic_key else _event_id(event_type, entity_type, entity_id, occurred_at)
    if semantic_key:
        existing = _find_event_by_id(event_id)
        if existing:
            duplicate = dict(existing)
            duplicate["duplicate_skipped"] = True
            return duplicate
    event = {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "event_type": str(event_type),
        "entity_type": str(entity_type),
        "entity_id": str(entity_id),
        "source": str(source),
        "actor": str(actor),
        "correlation_id": str(correlation_id or ""),
        "idempotency_key": semantic_key,
        "payload": _safe_payload(payload or {}),
        "safety_boundary": SAFE_BOUNDARY_NOTE,
    }
    path = _event_log_path(occurred_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return event


def quarantine_test_entity(
    entity_type: str,
    entity_id: str,
    test_source: str,
    reason: str,
) -> Dict[str, Any]:
    entity_type = str(entity_type or "").strip()
    entity_id = str(entity_id or "").strip()
    test_source = str(test_source or "").strip().lower()
    reason = str(reason or "").strip()
    if entity_type not in TEST_QUARANTINE_ENTITY_TYPES or not entity_id or not reason:
        raise ValueError("valid test entity_type, entity_id, and reason are required")
    if not (test_source.endswith("_uat") or test_source.startswith("test_")):
        raise ValueError("projection quarantine is restricted to explicit UAT/test sources")
    return append_event(
        TEST_QUARANTINE_EVENT,
        entity_type,
        entity_id,
        {
            "scope": "test_only",
            "test_source": test_source,
            "reason": reason,
            "production_data_deleted": False,
        },
        source="system_maintenance",
        actor="system_maintenance",
        idempotency_key="test-quarantine|{0}|{1}|{2}".format(test_source, entity_type, entity_id),
    )


def load_events() -> List[Dict[str, Any]]:
    event_dir = project_path("data/events")
    if not event_dir.exists():
        return []
    events: List[Dict[str, Any]] = []
    for path in sorted(event_dir.glob("events_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            continue
    return events


def load_compact_events() -> List[Dict[str, Any]]:
    path = project_path("data/events/compact/events_compact_latest.jsonl")
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                data = json.loads(line)
                if isinstance(data, dict):
                    events.append(data)
    except (OSError, json.JSONDecodeError):
        return []
    return events


def load_projection_events() -> List[Dict[str, Any]]:
    compact_events = load_compact_events()
    if compact_events and not is_compact_stale():
        return compact_events
    return load_events()


def is_compact_stale() -> bool:
    compact_events = load_compact_events()
    if not compact_events:
        return False
    raw_total = len(load_events())
    source_raw_total = _compact_source_raw_total()
    return source_raw_total != raw_total


def compact_event_ledger() -> Dict[str, Any]:
    raw_events = load_events()
    compact_events: List[Dict[str, Any]] = []
    seen = set()
    duplicate_rows = 0
    duplicate_ids = set()
    for event in raw_events:
        event_id = str(event.get("event_id") or _event_id_from_key(_payload_fingerprint(event)))
        if event_id in seen:
            duplicate_rows += 1
            duplicate_ids.add(event_id)
            continue
        seen.add(event_id)
        compact_events.append(event)

    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    compact_dir = project_path("data/events/compact")
    compact_dir.mkdir(parents=True, exist_ok=True)
    compact_path = compact_dir / "events_compact_{0}.jsonl".format(stamp)
    compact_latest = compact_dir / "events_compact_latest.jsonl"
    text = "\n".join(json.dumps(event, ensure_ascii=False, default=str) for event in compact_events)
    if text:
        text += "\n"
    compact_path.write_text(text, encoding="utf-8")
    compact_latest.write_text(text, encoding="utf-8")

    summary = {
        "generated_at": now_iso(),
        "raw_total_events": len(raw_events),
        "compact_total_events": len(compact_events),
        "duplicate_rows": duplicate_rows,
        "duplicate_event_id_count": len(duplicate_ids),
        "compact_path": str(compact_path),
        "compact_latest_path": str(compact_latest),
        "safety_boundary": SAFE_BOUNDARY_NOTE,
    }
    summary.update(_write_compaction_report(summary))
    return summary


def compute_event_health(
    events: Iterable[Dict[str, Any]] | None = None,
    *,
    duplicate_skipped: int = 0,
    projection_source: str = "",
) -> Dict[str, Any]:
    event_list = list(events if events is not None else load_events())
    compact_events = load_compact_events()
    raw_events = load_events()
    compact_source_raw_total = _compact_source_raw_total()
    compact_stale = bool(compact_events) and compact_source_raw_total != len(raw_events)
    counts, duplicate_ids = _event_duplicate_counts(event_list)
    raw_counts, raw_duplicate_ids = _event_duplicate_counts(raw_events)
    if not projection_source:
        if compact_events and compact_stale:
            projection_source = "raw_stale_compact"
        elif compact_events:
            projection_source = "compact"
        elif events is not None:
            projection_source = "provided"
        else:
            projection_source = "raw"
    return {
        "total_events": len(event_list),
        "unique_events": len(counts),
        "duplicate_event_id_count": len(duplicate_ids),
        "duplicate_rows": sum(count - 1 for count in duplicate_ids.values()),
        "duplicate_skipped": int(duplicate_skipped or 0),
        "raw_total_events": len(raw_events),
        "raw_unique_events": len(raw_counts),
        "raw_duplicate_event_id_count": len(raw_duplicate_ids),
        "raw_duplicate_rows": sum(count - 1 for count in raw_duplicate_ids.values()),
        "compact_available": bool(compact_events),
        "compact_total_events": len(compact_events),
        "compact_source_raw_total_events": compact_source_raw_total,
        "compact_stale": compact_stale,
        "needs_compaction": compact_stale or (not compact_events and bool(raw_events)),
        "projection_source": projection_source,
        "event_type_counts": dict(sorted(Counter(str(event.get("event_type", "")) for event in event_list).items())),
    }


def get_event_timeline(entity_type: str = "", entity_id: str = "", limit: int = 50) -> Dict[str, Any]:
    entity_type = str(entity_type or "").strip()
    entity_id = str(entity_id or "").strip()
    limit = max(1, int(limit or 50))
    events = load_projection_events()
    state = project_brokerage_state()
    filtered = []
    for event in events:
        if entity_type and str(event.get("entity_type", "")) != entity_type:
            continue
        if entity_id and str(event.get("entity_id", "")) != entity_id:
            continue
        filtered.append(event)
    filtered = sorted(filtered, key=lambda item: str(item.get("occurred_at", "")))
    if limit:
        filtered = filtered[-limit:]
    return {
        "generated_at": now_iso(),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "event_count": len(filtered),
        "events": filtered,
        "current_state": _current_entity_state(state, entity_type, entity_id),
        "projection_source": (state.get("event_health", {}) or {}).get("projection_source", ""),
        "event_health": state.get("event_health", {}),
        "safety_boundary": SAFE_BOUNDARY_NOTE,
    }


def write_event_audit(entity_type: str = "", entity_id: str = "", limit: int = 50) -> Dict[str, Any]:
    audit = get_event_timeline(entity_type, entity_id, limit)
    out_dir = project_path("reports/architecture")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(audit.get("generated_at") or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    json_path = out_dir / "event_audit_{0}.json".format(stamp)
    md_path = out_dir / "event_audit_{0}.md".format(stamp)
    latest_json = out_dir / "event_audit_latest.json"
    latest_md = out_dir / "event_audit_latest.md"
    payload = json.dumps(audit, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    markdown = _render_event_audit_markdown(audit)
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        **audit,
        "event_audit_json_path": str(json_path),
        "event_audit_path": str(md_path),
        "event_audit_latest_json_path": str(latest_json),
        "event_audit_latest_path": str(latest_md),
    }


def load_latest_event_audit() -> Dict[str, Any]:
    path = project_path("reports/architecture/event_audit_latest.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_remediation_event(
    task: Dict[str, Any],
    record: Dict[str, Any] | None = None,
    *,
    correlation_id: str = "",
) -> Dict[str, Any]:
    record = record or {}
    status = str(record.get("status") or task.get("status") or "open")
    event_type = _remediation_event_type(status)
    entity_id = str(task.get("task_id") or record.get("task_id") or "")
    idempotency_key = _semantic_event_key(
        correlation_id or str(record.get("record_id") or ""),
        event_type,
        "remediation_task",
        entity_id,
        str(record.get("action") or task.get("last_action") or status),
    )
    return append_event(
        event_type,
        "remediation_task",
        entity_id,
        {
            "status": status,
            "gap_type": record.get("gap_type", task.get("gap_type", "")),
            "source_id": record.get("source_id", task.get("source_id", "")),
            "signal_id": record.get("signal_id", task.get("signal_id", "")),
            "task_type": record.get("task_type", task.get("task_type", "")),
            "action": record.get("action", task.get("last_action", "")),
        },
        source="manual_remediation" if record else "stock_agent",
        actor=str(record.get("actor") or task.get("owner") or "system"),
        correlation_id=correlation_id or str(record.get("record_id") or ""),
        idempotency_key=idempotency_key,
    )


def record_decision_event(record: Dict[str, Any], *, correlation_id: str = "") -> Dict[str, Any]:
    entity_id = str(
        record.get("record_id")
        or "decision:{0}:{1}".format(record.get("signal_id", ""), _compact_time(record.get("decided_at", "")))
    )
    return append_event(
        "decision_recorded",
        "decision",
        entity_id,
        {
            "signal_id": record.get("signal_id", ""),
            "decision": record.get("decision", ""),
            "decision_family": record.get("decision_family", ""),
            "status": record.get("status", record.get("decision", "recorded")),
            "outcome": record.get("outcome", ""),
            "actor": record.get("actor", ""),
            "evidence_refs": record.get("evidence_refs", []),
        },
        source=str(record.get("source") or "manual_supervisor_decision"),
        actor=str(record.get("actor") or "user"),
        correlation_id=correlation_id,
        idempotency_key=_semantic_event_key(
            correlation_id or str(record.get("record_id") or entity_id),
            "decision_recorded",
            "decision",
            entity_id,
            str(record.get("decision") or record.get("status") or ""),
        ),
    )


def record_research_case_event(record: Dict[str, Any], *, correlation_id: str = "") -> Dict[str, Any]:
    case_id = str(record.get("case_id") or "")
    return append_event(
        "research_case_updated",
        "research_case",
        case_id,
        {
            "case_id": case_id,
            "title": record.get("title", ""),
            "status": record.get("status", ""),
            "decision": record.get("decision", ""),
            "top_priority": record.get("top_priority", ""),
            "signal_count": record.get("signal_count", 0),
            "actor": record.get("actor", ""),
        },
        source=str(record.get("source") or "research_cases"),
        actor=str(record.get("actor") or "system"),
        correlation_id=correlation_id,
        idempotency_key=_semantic_event_key(
            correlation_id or str(record.get("record_id") or record.get("decided_at") or ""),
            "research_case_updated",
            "research_case",
            case_id,
            str(record.get("decision") or record.get("status") or ""),
        ),
    )


def record_research_draft_event(record: Dict[str, Any], *, correlation_id: str = "") -> Dict[str, Any]:
    draft_id = str(record.get("draft_id") or "")
    payload = record.get("payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    event_type = str(record.get("event_type") or "research_draft_updated")
    return append_event(
        event_type,
        "research_draft",
        draft_id,
        {
            "draft_id": draft_id,
            "status": record.get("status", ""),
            "entity_type": payload.get("entity_type", ""),
            "entity_id": payload.get("entity_id", ""),
            "title": payload.get("title", ""),
            "thesis": payload.get("thesis", ""),
            "evidence_refs": payload.get("evidence_refs", []),
            "review_questions": payload.get("review_questions", []),
            "message": payload.get("message", ""),
            "commit_hash": payload.get("commit_hash", ""),
            "notes": payload.get("notes", ""),
            "actor": record.get("actor", ""),
            "forbidden_actions_note": record.get("forbidden_actions_note", SAFE_BOUNDARY_NOTE),
        },
        source=str(record.get("source") or "research_git"),
        actor=str(record.get("actor") or "system"),
        correlation_id=correlation_id or str(record.get("record_id") or ""),
        idempotency_key=_semantic_event_key(
            correlation_id or str(record.get("record_id") or ""),
            event_type,
            "research_draft",
            draft_id,
            str(record.get("status") or ""),
        ),
    )


def project_brokerage_state(events: Iterable[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    projection_source = "provided"
    if events is None:
        compact_events = load_compact_events()
        if compact_events and not is_compact_stale():
            event_list = compact_events
            projection_source = "compact"
        elif compact_events:
            event_list = load_events()
            projection_source = "raw_stale_compact"
        else:
            event_list = load_events()
            projection_source = "raw"
    else:
        event_list = list(events)
    audit_event_list = list(event_list)
    event_list, quarantined_entities = _filter_projection_quarantine(event_list)
    signals: Dict[str, Dict[str, Any]] = {}
    remediation_tasks: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Dict[str, Any]] = {}
    decisions: Dict[str, Dict[str, Any]] = {}
    research_cases: Dict[str, Dict[str, Any]] = {}
    research_drafts: Dict[str, Dict[str, Any]] = {}
    reviews: Dict[str, Dict[str, Any]] = {}

    for event in event_list:
        event_type = str(event.get("event_type", ""))
        entity_type = str(event.get("entity_type", ""))
        entity_id = str(event.get("entity_id", ""))
        payload = event.get("payload", {}) or {}
        occurred_at = event.get("occurred_at", "")
        if entity_type == "signal":
            state = signals.setdefault(entity_id, {"signal_id": entity_id})
            state.update({
                "status": _signal_status(event_type, state.get("status", "")),
                "priority": payload.get("priority", state.get("priority", "")),
                "source": payload.get("source", state.get("source", "")),
                "title": payload.get("title", state.get("title", "")),
                "llm_human_decision_needed": bool(payload.get("llm_human_decision_needed", state.get("llm_human_decision_needed", False))),
                "llm_confidence": payload.get("llm_confidence", state.get("llm_confidence", "")),
                "llm_impact_direction": payload.get("llm_impact_direction", state.get("llm_impact_direction", "")),
                "updated_at": occurred_at,
            })
        elif entity_type == "remediation_task":
            state = remediation_tasks.setdefault(entity_id, {"task_id": entity_id})
            state.update({
                "status": _remediation_status(event_type, payload, str(state.get("status") or "")),
                "gap_type": payload.get("gap_type", state.get("gap_type", "")),
                "source_id": payload.get("source_id", state.get("source_id", "")),
                "signal_id": payload.get("signal_id", state.get("signal_id", "")),
                "task_type": payload.get("task_type", state.get("task_type", "")),
                "action": payload.get("action", state.get("action", "")),
                "updated_at": occurred_at,
            })
        elif entity_type == "source":
            state = sources.setdefault(entity_id, {"source_id": entity_id})
            state.update({
                "status": _source_status(event_type, payload, str(state.get("status") or "")),
                "severity": payload.get("severity", state.get("severity", "")),
                "detail": payload.get("detail", state.get("detail", "")),
                "updated_at": occurred_at,
            })
        elif entity_type == "decision":
            decisions[entity_id] = {
                "decision_id": entity_id,
                "status": payload.get("status", payload.get("decision", "recorded")),
                "decision": payload.get("decision", ""),
                "signal_id": payload.get("signal_id", ""),
                "outcome": payload.get("outcome", ""),
                "actor": payload.get("actor", ""),
                "updated_at": occurred_at,
            }
        elif entity_type == "research_case":
            state = research_cases.setdefault(entity_id, {"case_id": entity_id})
            state.update({
                "status": str(payload.get("status") or state.get("status") or "active"),
                "title": payload.get("title", state.get("title", "")),
                "decision": payload.get("decision", state.get("decision", "")),
                "top_priority": payload.get("top_priority", state.get("top_priority", "")),
                "signal_count": payload.get("signal_count", state.get("signal_count", 0)),
                "updated_at": occurred_at,
            })
        elif entity_type == "research_draft":
            state = research_drafts.setdefault(entity_id, {"draft_id": entity_id})
            state.update({
                "status": str(payload.get("status") or state.get("status") or "draft"),
                "entity_type": payload.get("entity_type", state.get("entity_type", "")),
                "entity_id": payload.get("entity_id", state.get("entity_id", "")),
                "title": payload.get("title", state.get("title", "")),
                "thesis": payload.get("thesis", state.get("thesis", "")),
                "evidence_refs": payload.get("evidence_refs", state.get("evidence_refs", [])),
                "review_questions": payload.get("review_questions", state.get("review_questions", [])),
                "commit_hash": payload.get("commit_hash", state.get("commit_hash", "")),
                "commit_message": payload.get("message", state.get("commit_message", "")),
                "human_notes": payload.get("notes", state.get("human_notes", "")),
                "actor": payload.get("actor", state.get("actor", "")),
                "forbidden_actions_note": payload.get("forbidden_actions_note", state.get("forbidden_actions_note", SAFE_BOUNDARY_NOTE)),
                "updated_at": occurred_at,
            })
        elif entity_type == "review":
            reviews[entity_id] = {
                "review_id": entity_id,
                "signal_id": payload.get("signal_id", ""),
                "was_effective": payload.get("was_effective", ""),
                "reviewed_at": payload.get("reviewed_at", occurred_at),
                "updated_at": occurred_at,
            }

    signal_priorities = Counter(item.get("priority", "") for item in signals.values())
    signal_statuses = Counter(item.get("status", "active") for item in signals.values())
    remediation_statuses = Counter(item.get("status", "open") for item in remediation_tasks.values())
    source_statuses = Counter(item.get("status", "ok") for item in sources.values())
    case_statuses = Counter(item.get("status", "active") for item in research_cases.values())
    draft_statuses = Counter(item.get("status", "draft") for item in research_drafts.values())
    latest_event_at = event_list[-1].get("occurred_at", "") if event_list else ""
    event_health = compute_event_health(audit_event_list, projection_source=projection_source)

    return {
        "generated_at": now_iso(),
        "architecture_version": "brokerage_event_core_v2",
        "domains": {
            "research_front_office": {
                "signal_count": len(signals),
                "collected_signal_count": signal_statuses.get("collected", 0),
                "analyzed_signal_count": signal_statuses.get("analyzed", 0),
                "llm_reviewed_signal_count": signal_statuses.get("llm_reviewed", 0),
                "high_priority_signal_count": signal_priorities.get("P0", 0) + signal_priorities.get("P1", 0),
                "human_decision_needed_count": sum(1 for item in signals.values() if item.get("llm_human_decision_needed")),
                "research_case_count": len(research_cases),
                "active_research_case_count": (
                    case_statuses.get("active", 0)
                    + case_statuses.get("continue_tracking", 0)
                    + case_statuses.get("needs_evidence", 0)
                ),
                "research_draft_count": len(research_drafts),
                "research_draft_pending_review_count": draft_statuses.get("committed", 0) + draft_statuses.get("deferred", 0),
            },
            "data_platform": {
                "source_count": len(sources),
                "source_degraded_count": source_statuses.get("degraded", 0),
                "source_recovered_count": source_statuses.get("ok", 0),
            },
            "operations_middle_office": {
                "remediation_count": len(remediation_tasks),
                "remediation_open_count": remediation_statuses.get("open", 0),
                "remediation_deferred_count": remediation_statuses.get("deferred", 0),
                "remediation_resolved_count": remediation_statuses.get("resolved", 0),
                "remediation_rejected_count": remediation_statuses.get("rejected", 0),
                "review_completed_count": len(reviews),
            },
            "risk_compliance": {
                "decision_event_count": len(decisions),
                "human_gate_count": (
                    remediation_statuses.get("open", 0)
                    + remediation_statuses.get("deferred", 0)
                    + draft_statuses.get("committed", 0)
                    + draft_statuses.get("deferred", 0)
                ),
                "safety_boundary": SAFE_BOUNDARY_NOTE,
            },
            "audit_back_office": {
                "event_count": len(event_list),
                "raw_event_count": event_health["raw_total_events"],
                "compact_event_count": event_health["compact_total_events"],
                "unique_event_count": event_health["unique_events"],
                "duplicate_event_id_count": event_health["duplicate_event_id_count"],
                "latest_event_at": latest_event_at,
            },
        },
        "objects": {
            "signals": signals,
            "remediation_tasks": remediation_tasks,
            "sources": sources,
            "decisions": decisions,
            "research_cases": research_cases,
            "research_drafts": research_drafts,
            "reviews": reviews,
        },
        "event_type_counts": dict(sorted(Counter(str(event.get("event_type", "")) for event in event_list).items())),
        "event_health": event_health,
        "projection_quarantine": {
            "entity_count": len(quarantined_entities),
            "entities": quarantined_entities,
            "raw_events_retained": True,
        },
        "recent_events": _recent_events(event_list, limit=10),
        "latest_event_audit": _latest_event_audit_ref(),
        "safety_boundary": SAFE_BOUNDARY_NOTE,
    }


def _filter_projection_quarantine(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    quarantined = set()
    for event in events:
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        test_source = str(payload.get("test_source") or "").strip().lower()
        if (
            event.get("event_type") == TEST_QUARANTINE_EVENT
            and event.get("source") == "system_maintenance"
            and event.get("actor") == "system_maintenance"
            and payload.get("scope") == "test_only"
            and (test_source.endswith("_uat") or test_source.startswith("test_"))
        ):
            quarantined.add((str(event.get("entity_type") or ""), str(event.get("entity_id") or "")))
    filtered = [
        event for event in events
        if event.get("event_type") != TEST_QUARANTINE_EVENT
        and (str(event.get("entity_type") or ""), str(event.get("entity_id") or "")) not in quarantined
    ]
    labels = sorted("{0}:{1}".format(entity_type, entity_id) for entity_type, entity_id in quarantined)
    return filtered, labels


def sync_brokerage_architecture_from_latest_report(report_path: str = "") -> Dict[str, Any]:
    report = _load_latest_report(report_path)
    run_id = str(report.get("generated_at") or now_iso())
    events: List[Dict[str, Any]] = []
    events.append(append_event(
        "run_completed",
        "run",
        run_id,
        {
            "inserted": (report.get("stats", {}) or {}).get("inserted", 0),
            "duplicates": (report.get("stats", {}) or {}).get("duplicates", 0),
        },
        correlation_id=run_id,
        idempotency_key=_semantic_event_key(run_id, "run_completed", "run", run_id),
    ))
    for signal in report.get("signals", []) or []:
        signal_id = str(signal.get("signal_id") or "")
        signal_payload = {
            "title": signal.get("title", ""),
            "priority": signal.get("priority", ""),
            "source": signal.get("source", ""),
            "source_type": signal.get("source_type", ""),
            "published_at": signal.get("published_at", ""),
        }
        events.append(append_event(
            "signal_collected",
            "signal",
            signal_id,
            signal_payload,
            correlation_id=run_id,
            idempotency_key=_semantic_event_key(run_id, "signal_collected", "signal", signal_id),
        ))
        events.append(append_event(
            "signal_analyzed",
            "signal",
            signal_id,
            {
                **signal_payload,
                "llm_human_decision_needed": bool((signal.get("llm_analysis") or {}).get("human_decision_needed")),
                "role_conflict_level": (signal.get("role_synthesis") or {}).get("conflict_level", ""),
            },
            correlation_id=run_id,
            idempotency_key=_semantic_event_key(run_id, "signal_analyzed", "signal", signal_id),
        ))
        llm_analysis = signal.get("llm_analysis") if isinstance(signal.get("llm_analysis"), dict) else {}
        if llm_analysis or signal.get("llm_error"):
            events.append(append_event(
                "llm_reviewed",
                "signal",
                signal_id,
                {
                    **signal_payload,
                    "llm_human_decision_needed": bool(llm_analysis.get("human_decision_needed")),
                    "llm_confidence": llm_analysis.get("confidence", ""),
                    "llm_impact_direction": llm_analysis.get("impact_direction", ""),
                    "llm_error": signal.get("llm_error", ""),
                },
                correlation_id=run_id,
                idempotency_key=_semantic_event_key(run_id, "llm_reviewed", "signal", signal_id),
            ))
    stats = report.get("stats", {}) or {}
    source_health = stats.get("source_health", {}) or {}
    for event in source_health.get("source_degraded_events", []) or stats.get("source_degraded_events", []) or []:
        source_id = str(event.get("source_id") or "unknown")
        events.append(append_event(
            "source_degraded",
            "source",
            source_id,
            {
                "severity": event.get("severity", ""),
                "detail": event.get("detail", ""),
            },
            correlation_id=run_id,
            idempotency_key=_semantic_event_key(
                run_id,
                "source_degraded",
                "source",
                source_id,
                _payload_fingerprint(event),
            ),
        ))
    for task in stats.get("remediation_queue", []) or []:
        events.append(record_remediation_event(task, correlation_id=run_id))
    for record in stats.get("remediation_decision_records", []) or []:
        events.append(record_remediation_event(record, record, correlation_id=run_id))
        events.append(append_event(
            "decision_recorded",
            "decision",
            str(record.get("record_id") or record.get("task_id") or ""),
            {
                "status": record.get("status", ""),
                "action": record.get("action", ""),
                "actor": record.get("actor", ""),
                "task_id": record.get("task_id", ""),
                "signal_id": record.get("signal_id", ""),
            },
            correlation_id=run_id,
            idempotency_key=_semantic_event_key(
                run_id,
                "decision_recorded",
                "decision",
                str(record.get("record_id") or record.get("task_id") or ""),
                str(record.get("action") or record.get("status") or ""),
            ),
        ))
    for case in report.get("research_cases", []) or stats.get("research_cases", []) or []:
        events.append(record_research_case_event(case, correlation_id=run_id))
    for review in _load_completed_reviews():
        review_id = _review_id(review)
        events.append(append_event(
            "review_completed",
            "review",
            review_id,
            {
                "signal_id": review.get("signal_id", ""),
                "title": review.get("title", ""),
                "priority": review.get("priority", ""),
                "was_effective": review.get("was_effective", ""),
                "reviewed_at": review.get("reviewed_at", ""),
            },
            source="completed_review",
            actor=str(review.get("actor") or "user"),
            correlation_id=run_id,
            idempotency_key=_semantic_event_key(run_id, "review_completed", "review", review_id),
        ))

    architecture_state = project_brokerage_state()
    paths = write_architecture_state(architecture_state)
    duplicate_skipped = sum(1 for event in events if event.get("duplicate_skipped"))
    events_written = len(events) - duplicate_skipped
    compaction_result: Dict[str, Any] = {}
    if events_written > 0 and load_compact_events():
        compaction_result = compact_event_ledger()
        architecture_state = project_brokerage_state()
        paths = write_architecture_state(architecture_state)
    event_health = compute_event_health(duplicate_skipped=duplicate_skipped)
    return {
        **paths,
        "architecture_state": architecture_state,
        "events_written": events_written,
        "duplicate_skipped": duplicate_skipped,
        "compaction_refreshed": bool(compaction_result),
        "compaction_result": compaction_result,
        "event_health": event_health,
        "safety_boundary": SAFE_BOUNDARY_NOTE,
    }


def write_architecture_state(state: Dict[str, Any] | None = None) -> Dict[str, str]:
    state = state or project_brokerage_state()
    out_dir = project_path("reports/architecture")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(state.get("generated_at") or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    json_path = out_dir / "architecture_{0}.json".format(stamp)
    md_path = out_dir / "architecture_{0}.md".format(stamp)
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    payload = json.dumps(state, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    markdown = _render_architecture_markdown(state)
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        "architecture_json_path": str(json_path),
        "architecture_path": str(md_path),
        "architecture_latest_json_path": str(latest_json),
        "architecture_latest_path": str(latest_md),
    }


def _write_compaction_report(summary: Dict[str, Any]) -> Dict[str, str]:
    out_dir = project_path("reports/architecture")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(summary.get("generated_at") or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    json_path = out_dir / "event_compaction_{0}.json".format(stamp)
    md_path = out_dir / "event_compaction_{0}.md".format(stamp)
    latest_json = out_dir / "event_compaction_latest.json"
    latest_md = out_dir / "event_compaction_latest.md"
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    lines = [
        "# Event Compaction Report",
        "",
        "- generated_at: {0}".format(summary.get("generated_at", "")),
        "- raw_total_events: {0}".format(summary.get("raw_total_events", 0)),
        "- compact_total_events: {0}".format(summary.get("compact_total_events", 0)),
        "- duplicate_rows: {0}".format(summary.get("duplicate_rows", 0)),
        "- duplicate_event_id_count: {0}".format(summary.get("duplicate_event_id_count", 0)),
        "- compact_latest_path: {0}".format(summary.get("compact_latest_path", "")),
        "- safety_boundary: {0}".format(summary.get("safety_boundary", "")),
    ]
    markdown = "\n".join(lines)
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md.write_text(markdown, encoding="utf-8-sig")
    return {
        "compaction_json_path": str(json_path),
        "compaction_path": str(md_path),
        "compaction_latest_json_path": str(latest_json),
        "compaction_latest_path": str(latest_md),
    }


def _load_compaction_summary() -> Dict[str, Any]:
    path = project_path("reports/architecture/event_compaction_latest.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _compact_source_raw_total() -> int:
    summary = _load_compaction_summary()
    if not summary:
        return 0
    try:
        return int(summary.get("raw_total_events", 0) or 0)
    except (TypeError, ValueError):
        return 0


def load_latest_architecture_state() -> Dict[str, Any]:
    path = project_path("reports/architecture/latest.json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _event_log_path(occurred_at: str):
    stamp = str(occurred_at).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    return project_path("data/events/events_{0}.jsonl".format(stamp[:8]))


def _event_id(event_type: str, entity_type: str, entity_id: str, occurred_at: str) -> str:
    raw = "{0}|{1}|{2}|{3}".format(event_type, entity_type, entity_id, occurred_at)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _event_id_from_key(idempotency_key: str) -> str:
    return hashlib.sha1("event-key:{0}".format(idempotency_key).encode("utf-8")).hexdigest()[:16]


def _event_duplicate_counts(events: Iterable[Dict[str, Any]]):
    ids = [str(event.get("event_id", "")) for event in events if event.get("event_id")]
    counts = Counter(ids)
    duplicate_ids = {event_id: count for event_id, count in counts.items() if count > 1}
    return counts, duplicate_ids


def _current_entity_state(state: Dict[str, Any], entity_type: str, entity_id: str) -> Dict[str, Any]:
    if not entity_type or not entity_id:
        return {}
    objects = state.get("objects", {}) or {}
    mapping = {
        "signal": "signals",
        "remediation_task": "remediation_tasks",
        "source": "sources",
        "decision": "decisions",
        "research_case": "research_cases",
        "research_draft": "research_drafts",
        "review": "reviews",
    }
    bucket = objects.get(mapping.get(entity_type, ""), {}) or {}
    value = bucket.get(entity_id, {})
    return dict(value) if isinstance(value, dict) else {}


def _recent_events(events: List[Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
    result = []
    for event in sorted(events, key=lambda item: str(item.get("occurred_at", "")))[-limit:]:
        result.append({
            "occurred_at": event.get("occurred_at", ""),
            "event_type": event.get("event_type", ""),
            "entity_type": event.get("entity_type", ""),
            "entity_id": event.get("entity_id", ""),
        })
    return result


def _latest_event_audit_ref() -> Dict[str, str]:
    latest_json = project_path("reports/architecture/event_audit_latest.json")
    latest_md = project_path("reports/architecture/event_audit_latest.md")
    return {
        "event_audit_latest_json_path": str(latest_json) if latest_json.exists() else "",
        "event_audit_latest_path": str(latest_md) if latest_md.exists() else "",
    }


def _render_event_audit_markdown(audit: Dict[str, Any]) -> str:
    lines = ["# Event Audit Timeline", ""]
    entity = "{0}:{1}".format(audit.get("entity_type", "") or "*", audit.get("entity_id", "") or "*")
    lines.append("- generated_at: {0}".format(audit.get("generated_at", "")))
    lines.append("- entity: {0}".format(entity))
    lines.append("- event_count: {0}".format(audit.get("event_count", 0)))
    lines.append("- projection_source: {0}".format(audit.get("projection_source", "")))
    lines.append("- safety_boundary: {0}".format(audit.get("safety_boundary", "")))
    current = audit.get("current_state", {}) or {}
    if current:
        lines.append("")
        lines.append("## Current State")
        for key, value in sorted(current.items()):
            lines.append("- {0}: {1}".format(key, value))
    lines.append("")
    lines.append("## Timeline")
    for event in audit.get("events", []) or []:
        lines.append("- {0} | {1} | {2}:{3}".format(
            event.get("occurred_at", ""),
            event.get("event_type", ""),
            event.get("entity_type", ""),
            event.get("entity_id", ""),
        ))
    if not audit.get("events"):
        lines.append("- no matching events")
    return "\n".join(lines)


def _find_event_by_id(event_id: str) -> Dict[str, Any] | None:
    for event in load_events():
        if str(event.get("event_id") or "") == event_id:
            return event
    return None


def _semantic_event_key(
    scope: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    qualifier: str = "",
) -> str:
    parts = [
        str(scope or "global"),
        str(event_type),
        str(entity_type),
        str(entity_id),
        str(qualifier or ""),
    ]
    return "|".join(parts)


def _payload_fingerprint(payload: Dict[str, Any]) -> str:
    data = json.dumps(_safe_payload(payload or {}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def _safe_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    blocked = {"allowed_actions", "forbidden_actions_note", "llm_actions"}
    return {str(key): value for key, value in payload.items() if str(key) not in blocked}


def _remediation_event_type(status: str) -> str:
    return REMEDIATION_EVENT_BY_STATUS.get(str(status or "open"), "remediation_created")


def _remediation_status(event_type: str, payload: Dict[str, Any], previous: str) -> str:
    mapping = {
        "remediation_created": "open",
        "remediation_resolved": "resolved",
        "remediation_rejected": "rejected",
        "remediation_deferred": "deferred",
    }
    return mapping.get(event_type, str(payload.get("status") or previous or "open"))


def _source_status(event_type: str, payload: Dict[str, Any], previous: str) -> str:
    if event_type == "source_degraded":
        return "degraded"
    if event_type == "source_recovered":
        return "ok"
    return str(payload.get("status") or previous or "ok")


def _signal_status(event_type: str, previous: str) -> str:
    mapping = {
        "signal_collected": "collected",
        "signal_analyzed": "analyzed",
        "llm_reviewed": "llm_reviewed",
        "decision_recorded": "decision_recorded",
    }
    return mapping.get(event_type, previous or "active")


def _compact_time(value: Any) -> str:
    return str(value or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]


def _review_id(review: Dict[str, Any]) -> str:
    raw = "{0}|{1}|{2}".format(
        review.get("signal_id", ""),
        review.get("reviewed_at", ""),
        review.get("was_effective", ""),
    )
    return "review:{0}".format(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16])


def _load_completed_reviews() -> List[Dict[str, Any]]:
    review_dir = project_path("data/review")
    if not review_dir.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(review_dir.glob("completed_review_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        records.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    return records[-200:]


def _load_latest_report(report_path: str) -> Dict[str, Any]:
    if report_path:
        path = project_path(report_path)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    json_dir = project_path("reports/json")
    if not json_dir.exists():
        return {"signals": [], "stats": {}}
    files = sorted(json_dir.glob("*.json"))
    if not files:
        return {"signals": [], "stats": {}}
    return json.loads(files[-1].read_text(encoding="utf-8-sig"))


def _render_architecture_markdown(state: Dict[str, Any]) -> str:
    lines = ["# Brokerage Architecture State", ""]
    lines.append("- generated_at: {0}".format(state.get("generated_at", "")))
    lines.append("- architecture_version: {0}".format(state.get("architecture_version", "")))
    lines.append("- safety_boundary: {0}".format(state.get("safety_boundary", "")))
    lines.append("")
    lines.append("## Domains")
    for domain, values in (state.get("domains", {}) or {}).items():
        lines.append("")
        lines.append("### {0}".format(domain))
        for key, value in sorted((values or {}).items()):
            lines.append("- {0}: {1}".format(key, value))
    lines.append("")
    lines.append("## Event Types")
    counts = state.get("event_type_counts", {}) or {}
    if not counts:
        lines.append("- none")
    for key, value in sorted(counts.items()):
        lines.append("- {0}: {1}".format(key, value))
    return "\n".join(lines)
