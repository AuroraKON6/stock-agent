"""GLM-10 slice 1: read-only workbench overview aggregation.

One honest snapshot of the whole workbench: which engine owns the event
stream, what the market cache actually holds (with missing quotes named),
which events are research-eligible, task-engine state including the
questions waiting on the human, and today's model-call budget usage.
Pure projection over existing authorities — no new state, no writes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

OVERVIEW_SCHEMA_VERSION = "1.0"
OVERVIEW_SCOPE_NOTE = "personal_research_workbench_not_institutional_approval"
RECENT_EVENT_LIMIT = 10
EVENT_LOOKBACK_HOURS = 24


def _topic_symbols(config: Dict[str, Any]) -> list:
    symbols: list = []
    seen: set = set()
    for topic in (config or {}).get("topics") or []:
        if not isinstance(topic, dict):
            continue
        for symbol in topic.get("symbols") or []:
            text = str(symbol).strip()
            if text and text not in seen:
                seen.add(text)
                symbols.append(text)
    return symbols


def build_overview(config: Dict[str, Any], *, store: Optional[Any] = None) -> Dict[str, Any]:
    from .models import now_iso

    config = config or {}

    # Engine: which path owns the event stream.
    from .execution_mode import resolve_event_intake_mode

    try:
        engine_mode = resolve_event_intake_mode(config)
    except ValueError as exc:
        engine_mode = "invalid: {0}".format(exc)

    # Market: quote snapshots straight from the cache with honest missing list.
    from .task_supply import _quote_snapshots

    symbols = _topic_symbols(config)
    quotes = _quote_snapshots(symbols) if symbols else {}
    market = {
        "symbols": symbols,
        "quotes": quotes,
        "quotes_missing": [symbol for symbol in symbols if symbol not in quotes],
        "note": "quotes come from the local cache; a missing quote is reported, never faked",
    }

    # Events: research-eligible public signals in the lookback window.
    from .task_supply import load_research_signals

    rows, selection = load_research_signals(config, lookback_hours=EVENT_LOOKBACK_HOURS)
    events = {
        "lookback_hours": EVENT_LOOKBACK_HOURS,
        "eligible_count": len(rows),
        "recent": [
            {
                "signal_id": str(row.get("signal_id") or ""),
                "title": str(row.get("title") or "")[:200],
                "source_type": str(row.get("source_type") or ""),
                "published_at": str(row.get("published_at") or ""),
            }
            for row in rows[:RECENT_EVENT_LIMIT]
        ],
        "selection": selection,
    }

    # Tasks + budget: only when a store is available.
    tasks: Dict[str, Any] = {"available": False}
    budget: Dict[str, Any] = {"calls_today": 0, "limit": 0, "enabled": False}
    if store is not None:
        summary = store.summary() or {}
        status_counts = dict(summary.get("task_counts") or {})
        awaiting = []
        for task in store.list_tasks(status="awaiting_human"):
            request = task.get("human_request") if isinstance(task.get("human_request"), dict) else {}
            awaiting.append({
                "task_id": str(task.get("task_id") or ""),
                "question": str(request.get("question") or "")[:200],
                "updated_at": str(task.get("updated_at") or ""),
            })
        tasks = {
            "available": True,
            "status_counts": status_counts,
            "remaining_queued": int(status_counts.get("queued") or 0),
            "awaiting_human": awaiting,
            "outbox_events": int(summary.get("outbox_events") or 0),
            "consumption": store.consumption_summary(),
        }
        from .budget_guard import may_start_model_call

        decision = may_start_model_call(store, config)
        budget = {
            "calls_today": decision["calls_today"],
            "limit": decision["limit"],
            "enabled": decision["limit"] > 0,
        }

    return {
        "schema_version": OVERVIEW_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope_note": OVERVIEW_SCOPE_NOTE,
        "engine": {"event_intake_mode": engine_mode},
        "market": market,
        "events": events,
        "tasks": tasks,
        "budget": budget,
    }
