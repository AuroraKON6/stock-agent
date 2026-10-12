"""GLM-10 events view: deduped events with sources, revisions, linked tasks.

Groups research-eligible public signals into STORY events by the same
cluster identity the trigger engine uses (normalized title + symbols), so
what the user sees matches what the engine acted on. Each event lists its
member reports with their sources, flags content revisions, and shows the
research tasks created from it (via the consumption ledger) with their
current status. Pure projection; no writes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

EVENTS_VIEW_SCHEMA_VERSION = "1.0"
EVENTS_VIEW_SCOPE_NOTE = "personal_research_workbench_not_institutional_approval"
DEFAULT_LOOKBACK_HOURS = 72
DEFAULT_EVENT_LIMIT = 50
MAX_REPORTS_PER_EVENT = 20
MAX_LINKED_TASKS_PER_EVENT = 10


def build_events_view(
    config: Dict[str, Any],
    *,
    store: Optional[Any] = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    limit: int = DEFAULT_EVENT_LIMIT,
    purpose: str = "",
) -> Dict[str, Any]:
    from .event_contract import event_identity
    from .models import now_iso
    from .research_trigger import cluster_key
    from .task_intake import DEFAULT_EVENT_PURPOSE
    from .task_supply import load_research_signals

    purpose = purpose or DEFAULT_EVENT_PURPOSE
    rows, selection = load_research_signals(config, lookback_hours=lookback_hours)

    clusters: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = cluster_key(row)
        entry = clusters.get(key)
        if entry is None:
            entry = clusters[key] = {"reports": [], "symbols": set()}
        entry["reports"].append(row)
        entry["symbols"].update(str(symbol) for symbol in (row.get("symbols") or []))

    events: List[Dict[str, Any]] = []
    for key, entry in clusters.items():
        reports = sorted(entry["reports"],
                         key=lambda row: str(row.get("published_at") or ""),
                         reverse=True)
        newest = reports[0]
        versions = sorted({str((row.get("payload") or {}).get("content_version") or "1")
                           for row in reports if isinstance(row.get("payload"), dict)})
        sources = sorted({str(row.get("source") or "") for row in reports if row.get("source")})

        linked_tasks: List[Dict[str, Any]] = []
        if store is not None:
            seen_tasks = set()
            for row in reports:
                if len(linked_tasks) >= MAX_LINKED_TASKS_PER_EVENT:
                    break
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                event_key = event_identity(
                    source_id=str(row.get("source") or ""),
                    url=str(row.get("url") or ""),
                    title=str(row.get("title") or ""),
                    content_version=str(payload.get("content_version") or "1"))
                consumption = store.find_consumption(event_key, purpose)
                if consumption is None:
                    continue
                task_id = str(consumption.get("task_id") or "")
                if not task_id or task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
                try:
                    status = str(store.get_task(task_id).get("status") or "")
                except Exception:
                    status = "unknown"
                linked_tasks.append({"task_id": task_id, "status": status})

        events.append({
            "cluster_key": key,
            "title": str(newest.get("title") or "")[:200],
            "symbols": sorted(entry["symbols"]),
            "sources": sources,
            "report_count": len(reports),
            "reports": [
                {
                    "signal_id": str(row.get("signal_id") or ""),
                    "source": str(row.get("source") or ""),
                    "url": str(row.get("url") or ""),
                    "content_version": str((row.get("payload") or {}).get("content_version") or "1"),
                    "published_at": str(row.get("published_at") or ""),
                }
                for row in reports[:MAX_REPORTS_PER_EVENT]
            ],
            "versions": versions,
            "revision": any(version != "1" for version in versions),
            "first_published": str(reports[-1].get("published_at") or ""),
            "last_published": str(reports[0].get("published_at") or ""),
            "linked_tasks": linked_tasks,
        })

    events.sort(key=lambda event: event["last_published"], reverse=True)
    return {
        "schema_version": EVENTS_VIEW_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope_note": EVENTS_VIEW_SCOPE_NOTE,
        "lookback_hours": lookback_hours,
        "event_count": len(events),
        "events": events[:max(1, int(limit))],
        "selection": selection,
    }
