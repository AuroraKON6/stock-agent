"""GLM-10 ops view: run health from existing projections, honestly offline.

Aggregates what can be known WITHOUT running any status command (those may
write reports/projections): task-engine state and budget from the store,
recent terminal failures, the source registry's approval states, and the
freshness of existing latest.json projections. Source HEALTH against real
upstreams stays BLOCKED until sources are approved; every projection is
labeled as a projection with its age, never as live gate truth.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

OPS_VIEW_SCHEMA_VERSION = "1.0"
OPS_VIEW_SCOPE_NOTE = "personal_research_workbench_not_institutional_approval"
PROJECTIONS_NOTE = "只读已有投影（latest.json）并标注年龄；权威门禁状态仍以各 gate CLI 为准"
SOURCE_HEALTH_NOTE = "真实来源健康需来源批准后运行（BLOCKED）；此处仅展示登记与批准状态"
MAX_RECENT_FAILURES = 10

_PROJECTION_PATHS = (
    ("run_coordination", "reports/run_coordination/latest.json"),
    ("evidence_vault", "reports/evidence_vault/latest.json"),
    ("production_pilot", "reports/production_pilot/latest.json"),
    ("llm_governance", "reports/llm_governance/latest.json"),
    ("review_queue", "reports/review_queue/latest.json"),
)


def _age_minutes(stamp: str, now: Optional[datetime] = None) -> Optional[int]:
    try:
        parsed = datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return max(0, int(((now or datetime.now().astimezone()) - parsed).total_seconds() // 60))


def _is_expired(entry: Dict[str, Any]) -> bool:
    try:
        return date.fromisoformat(str(entry.get("expires_at") or "")) < date.today()
    except ValueError:
        return True


def _projection_entries(config: Dict[str, Any], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    from .config import project_path

    entries: List[Dict[str, Any]] = []
    for name, relative in _PROJECTION_PATHS:
        path = project_path(relative)
        entry = {"name": name, "path": relative, "present": False,
                 "generated_at": "", "age_minutes": None}
        if path.is_file():
            try:
                document = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                document = None
            if isinstance(document, dict):
                stamp = str(document.get("generated_at")
                            or document.get("generatedAt")
                            or document.get("as_of") or "")
                entry["present"] = True
                entry["generated_at"] = stamp
                if stamp:
                    entry["age_minutes"] = _age_minutes(stamp, now)
        entries.append(entry)
    return entries


def build_ops_view(config: Dict[str, Any], *, store: Optional[Any] = None,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    from .models import now_iso

    config = config or {}

    from .execution_mode import resolve_event_intake_mode

    try:
        engine_mode = resolve_event_intake_mode(config)
    except ValueError as exc:
        engine_mode = "invalid: {0}".format(exc)

    engine: Dict[str, Any] = {"event_intake_mode": engine_mode,
                              "status_counts": {}, "remaining_queued": 0,
                              "outbox_events": 0, "consumption": None}
    budget: Dict[str, Any] = {"calls_today": 0, "limit": 0, "enabled": False}
    failures: List[Dict[str, Any]] = []
    if store is not None:
        summary = store.summary() or {}
        counts = dict(summary.get("task_counts") or {})
        engine["status_counts"] = counts
        engine["remaining_queued"] = int(counts.get("queued") or 0)
        engine["outbox_events"] = int(summary.get("outbox_events") or 0)
        engine["consumption"] = store.consumption_summary()

        from .budget_guard import may_start_model_call

        decision = may_start_model_call(store, config, now=now)
        budget = {"calls_today": decision["calls_today"], "limit": decision["limit"],
                  "enabled": decision["limit"] > 0}

        terminal = list(store.list_tasks(status="failed")) + list(store.list_tasks(status="blocked"))
        terminal.sort(key=lambda task: str(task.get("updated_at") or ""), reverse=True)
        failures = [
            {
                "task_id": str(task.get("task_id") or ""),
                "title": str(task.get("title") or ""),
                "status": str(task.get("status") or ""),
                "error": str(task.get("last_error") or "")[:200],
                "updated_at": str(task.get("updated_at") or ""),
            }
            for task in terminal[:MAX_RECENT_FAILURES]
        ]

    from .sources.registry import load_source_registry

    registry = load_source_registry(config)
    entries = list((registry.get("entries") or {}).values())
    approval_counts: Dict[str, int] = {}
    for entry in entries:
        state = str((entry.get("approval") or {}).get("state") or "")
        approval_counts[state] = approval_counts.get(state, 0) + 1
    source_registry = {
        "configured": bool(entries),
        "entry_count": len(entries),
        "approval_counts": approval_counts,
        "entries": [
            {
                "source_id": str(entry.get("source_id") or ""),
                "kind": str(entry.get("kind") or ""),
                "approval_state": str((entry.get("approval") or {}).get("state") or ""),
                "expired": _is_expired(entry),
                "external_send_allowed": bool(entry.get("llm_external_send_allowed")),
            }
            for entry in entries
        ],
        "note": SOURCE_HEALTH_NOTE,
    }

    return {
        "schema_version": OPS_VIEW_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope_note": OPS_VIEW_SCOPE_NOTE,
        "engine": engine,
        "budget": budget,
        "recent_failures": failures,
        "source_registry": source_registry,
        "projections": _projection_entries(config, now),
        "projections_note": PROJECTIONS_NOTE,
    }
