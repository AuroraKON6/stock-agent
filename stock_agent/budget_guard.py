"""GLM-05 remainder: model-call budget guard.

The outbox ``research_attempt_started`` events (persisted before every live
model call by the worker) double as the budget ledger: today's attempts are
counted from them, so the guard can never be bypassed by a task that failed
before recording usage. The cap is attempts per day — deterministic and
honest; money-based caps additionally require approved pricing metadata and
stay out of scope here. Limit 0 disables the guard (explicit W10 setup will
set it deliberately).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

ATTEMPT_EVENT = "research_attempt_started"


def budget_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = ((config or {}).get("task_runtime") or {}).get("budget") or {}
    if not isinstance(raw, dict):
        raise ValueError("task_runtime.budget must be an object")
    unexpected = set(raw) - {"daily_model_call_limit"}
    if unexpected:
        raise ValueError("unknown budget fields: {0}".format(sorted(unexpected)))
    limit = raw.get("daily_model_call_limit") or 0
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("daily_model_call_limit must be a non-negative integer (0 = off)")
    return {"daily_model_call_limit": limit}


def model_calls_today(store: Any, *, now: Optional = None) -> int:
    """Count live model attempts started on the EXPLICIT Shanghai business day.

    R07: the budget day is the Asia/Shanghai calendar date (fixed midnight
    boundary), not the host-local date — the counting key and the stamp
    filter both go through ``runtime_layout.business_date``."""
    from .runtime_layout import business_date

    today = business_date(now)
    count = 0
    for event in store.events(limit=100000):
        if event.get("event_type") != ATTEMPT_EVENT:
            continue
        try:
            stamp = datetime.fromisoformat(str(event.get("created_at") or ""))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        if business_date(stamp) == today:
            count += 1
    return count


def may_start_model_call(store: Any, config: Dict[str, Any], *, now=None) -> Dict[str, Any]:
    limit = budget_config(config)["daily_model_call_limit"]
    calls_today = model_calls_today(store, now=now)
    if limit > 0 and calls_today >= limit:
        return {
            "allowed": False,
            "reason": "daily model-call budget exhausted: {0}/{1} attempts today".format(
                calls_today, limit),
            "calls_today": calls_today,
            "limit": limit,
        }
    return {"allowed": True, "reason": "ok", "calls_today": calls_today, "limit": limit}


def reserve_model_attempt(store: Any, config: Dict[str, Any], task_id: str, *,
                          worker_id: str, fencing_token: int,
                          attempt: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically reserve one of today's model attempts.

    Read-only views use may_start_model_call; an actual call MUST go through
    this: the store counts today's attempt events and records the new one in
    a single write transaction, closing the check-then-act race in which two
    concurrent workers both pass a limit-1 check.
    """
    limit = budget_config(config)["daily_model_call_limit"]
    return store.reserve_model_attempt(task_id, worker_id=worker_id,
                                       fencing_token=fencing_token,
                                       attempt=attempt, daily_limit=limit)
