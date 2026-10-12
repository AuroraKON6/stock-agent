"""GLM-04 unified task intake: one creation service for events and questions.

Both the CLI and (future) pages call these functions; business rules live
here once instead of being copied per entry point. Deduplication for events
is by event identity + purpose (same re-delivery returns the existing task;
a real content revision creates a traceable new task that supersedes the
old one). The consumption record commits in the same transaction as the
task, and rescans reconcile from the consumption set — never from a
source-time watermark that would drop late-arriving events.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .event_contract import event_base_identity, event_identity
from .task_supply import build_task_spec

DEFAULT_EVENT_PURPOSE = "event_research_v1"
MAX_TITLE_CHARS = 120


class TaskIntakeError(ValueError):
    pass


def _required_event_field(event: Dict[str, Any], field: str) -> str:
    value = str(event.get(field) or "").strip()
    if not value:
        raise TaskIntakeError("event field {0} is required".format(field))
    return value


def normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the event fields the intake contract needs."""
    if not isinstance(event, dict):
        raise TaskIntakeError("event must be an object")
    source_id = _required_event_field(event, "source_id")
    title = _required_event_field(event, "title")
    url = str(event.get("url") or "")
    symbols = [str(symbol).strip() for symbol in (event.get("symbols") or [])
               if str(symbol).strip()]
    if not symbols:
        raise TaskIntakeError("event must name at least one symbol")
    content_version = str(event.get("content_version") or "1").strip() or "1"
    return {
        "source_id": source_id,
        "title": title,
        "url": url,
        "symbols": symbols,
        "content_version": content_version,
        "signal_id": str(event.get("signal_id") or "").strip(),
    }


def event_from_signal_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map a stored signal row (as returned by load_research_signals) to an event."""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "source_id": str(row.get("source") or ""),
        "url": str(row.get("url") or ""),
        "title": str(row.get("title") or ""),
        "symbols": list(row.get("symbols") or []),
        "content_version": str(payload.get("content_version") or "1"),
        "signal_id": str(row.get("signal_id") or ""),
    }


def ensure_event_task(
    store: Any,
    *,
    event: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    purpose: str = DEFAULT_EVENT_PURPOSE,
    actor: str = "operator",
    execution_mode: str = "replay",
    research_question: str = "",
) -> Dict[str, Any]:
    normalized = normalize_event(event)
    event_key = event_identity(
        source_id=normalized["source_id"], url=normalized["url"],
        title=normalized["title"], content_version=normalized["content_version"])
    base_key = event_base_identity(
        source_id=normalized["source_id"], url=normalized["url"],
        title=normalized["title"])

    existing = store.find_consumption(event_key, purpose)
    if existing is not None:
        # The consumption ledger is the dedup authority for events; a repeat
        # delivery returns the task it named without rebuilding context.
        return {
            "task": store.get_task(str(existing["task_id"])),
            "status": "DUPLICATE",
            "event_key": event_key,
            "base_key": base_key,
            "supersedes_task_id": None,
        }

    supersedes_task_id = ""
    previous = store.latest_consumption_for_base(base_key, purpose=purpose)
    if previous is not None:
        supersedes_task_id = str(previous["task_id"])

    question = str(research_question or "").strip() or "研究公开事件：{0}".format(normalized["title"])
    spec = build_task_spec(
        research_question=question, symbols=normalized["symbols"],
        execution_mode=execution_mode, config=config)
    spec["origin"] = {
        "kind": "event",
        "event_key": event_key,
        "base_key": base_key,
        "content_version": normalized["content_version"],
        "source_id": normalized["source_id"],
        "url": normalized["url"],
        "title": normalized["title"],
        "signal_id": normalized["signal_id"],
        "supersedes_task_id": supersedes_task_id or None,
    }
    task, status = store.create_task(
        title="事件研究：{0}".format(normalized["title"])[:MAX_TITLE_CHARS],
        input_payload=spec,
        execution_mode=execution_mode,
        actor=actor,
        idempotency_key="event::{0}::{1}".format(event_key, purpose),
        consume_event={
            "event_key": event_key, "purpose": purpose,
            "base_key": base_key, "signal_id": normalized["signal_id"],
        },
    )
    return {
        "task": task,
        "status": status,
        "event_key": event_key,
        "base_key": base_key,
        "supersedes_task_id": supersedes_task_id or None,
    }


def ensure_question_task(
    store: Any,
    *,
    research_question: str,
    symbols: List[str],
    config: Optional[Dict[str, Any]] = None,
    actor: str = "operator",
    execution_mode: str = "replay",
    lookback_hours: int = 48,
) -> Dict[str, Any]:
    question = str(research_question or "").strip()
    if not question:
        raise TaskIntakeError("research_question must not be empty")
    clean_symbols = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
    if not clean_symbols:
        raise TaskIntakeError("question tasks must name at least one symbol")
    spec = build_task_spec(
        research_question=question, symbols=clean_symbols,
        lookback_hours=lookback_hours, execution_mode=execution_mode, config=config)
    spec["origin"] = {
        "kind": "question",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "symbols": clean_symbols,
    }
    # Question identity = the question text + symbol scope + mode. A rebuilt
    # public context (fresh cache/timestamps) is not a new question; the store
    # applies the same logical-request exemption as for events.
    question_key = "question::{0}::{1}::{2}".format(
        spec["origin"]["question_sha256"],
        ",".join(sorted(clean_symbols)),
        execution_mode,
    )
    task, status = store.create_task(
        title="用户问题：{0}".format(question)[:MAX_TITLE_CHARS],
        input_payload=spec,
        execution_mode=execution_mode,
        actor=actor,
        idempotency_key=question_key,
    )
    return {
        "task": task,
        "status": status,
        "event_key": None,
        "base_key": None,
        "supersedes_task_id": None,
    }


def run_daily_task_cycle(
    store: Any,
    *,
    config: Optional[Dict[str, Any]] = None,
    purpose: str = DEFAULT_EVENT_PURPOSE,
    actor: str = "operator",
    worker_id: str = "w-daily-cycle",
    execution_mode: str = "replay",
    max_tasks: int = 20,
    ttl_seconds: int = 240,
) -> Dict[str, Any]:
    """One scheduled task-engine cycle: rescan events, then drain the queue.

    Python-level so the scheduled script needs only a single command swap
    when the task engine becomes authoritative. Batch mode is refused (the
    exclusivity gate); recovery of abandoned leases runs before draining so
    a crashed previous cycle's tasks requeue per store semantics. Every
    outcome is counted: failures are reported, never hidden or retried
    beyond the task's own attempt budget.
    """
    from .execution_mode import resolve_event_intake_mode, EVENT_INTAKE_TASK
    from .research_worker import run_research_task

    if resolve_event_intake_mode(config or {}) != EVENT_INTAKE_TASK:
        raise TaskIntakeError(
            "daily-task-cycle is the task engine's scheduled entry; "
            "task_runtime.event_intake_mode=batch keeps the batch pipeline "
            "authoritative (agent-run). Switch the setting to run this cycle.")

    recovered = store.recover_abandoned()
    rescan_report = rescan_pending_events(
        store, config=config, purpose=purpose, actor=actor,
        execution_mode=execution_mode)

    processed = succeeded = failed = blocked = awaiting_human = cancelled = 0
    while processed < max(1, int(max_tasks)):
        task = store.claim_next(worker_id, ttl_seconds=ttl_seconds)
        if task is None:
            break
        processed += 1
        try:
            result = run_research_task(
                store, task["task_id"], worker_id=worker_id,
                config=config, ttl_seconds=ttl_seconds)
            status = str(result.get("status") or "")
        except TaskIntakeError:
            raise
        except Exception as exc:  # defensive: a worker crash must not kill the cycle
            store.fail(task["task_id"], worker_id=worker_id,
                       fencing_token=task["fencing_token"],
                       error="cycle worker crashed: {0}: {1}".format(type(exc).__name__, str(exc)[:300]),
                       retryable=True)
            failed += 1
            continue
        if status == "succeeded":
            succeeded += 1
        elif status == "failed":
            failed += 1
        elif status == "blocked":
            blocked += 1
        elif status == "awaiting_human":
            awaiting_human += 1
        elif status == "cancelled":
            cancelled += 1
        else:
            failed += 1
    remaining = len(store.list_tasks(status="queued"))
    # Full store status counts so pre-existing terminal failures are visible
    # in every scheduled report, not just this cycle's own work.
    return {
        "recovered_tasks": len(recovered),
        "rescan": rescan_report,
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "blocked": blocked,
        "awaiting_human": awaiting_human,
        "cancelled": cancelled,
        "remaining_queued": remaining,
        "status_counts": dict((store.summary() or {}).get("task_counts") or {}),
        "stopped_reason": "queue_empty" if processed < max_tasks else "max_tasks_reached",
    }


def rescan_pending_events(
    store: Any,
    *,
    config: Optional[Dict[str, Any]] = None,
    purpose: str = DEFAULT_EVENT_PURPOSE,
    actor: str = "operator",
    execution_mode: str = "replay",
    symbols: Optional[List[str]] = None,
    lookback_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """Reconcile the signal store into tasks; safe to run after any crash.

    Eligibility comes from load_research_signals (public classification,
    example/synthetic provenance excluded). Already-consumed events are
    skipped by consumption-set membership — not by a source-time watermark —
    so events that arrive late are never silently dropped.
    """
    from .execution_mode import assert_event_rescan_allowed
    from .task_supply import load_research_signals

    # Single-engine rule: while the batch pipeline owns the event stream,
    # event-task creation would analyze the same signals twice.
    try:
        assert_event_rescan_allowed(config or {})
    except ValueError as exc:
        raise TaskIntakeError(str(exc)) from exc
    rows, selection = load_research_signals(
        config, symbols=symbols, lookback_hours=lookback_hours)
    from .research_trigger import decide_trigger
    from .models import now_iso as _now_iso

    # Cross-event trigger state for this cycle: caps and cooldowns need to see
    # what earlier rows in the same scan already opened.
    trigger_state: Dict[str, Any] = {"symbol_cycle_counts": {}, "symbol_last_trigger": {}}
    skipped_by_trigger = 0
    events = [event_from_signal_row(row) for row in rows]
    normalized = [normalize_event(event) for event in events]
    keys = [event_identity(
        source_id=item["source_id"], url=item["url"], title=item["title"],
        content_version=item["content_version"]) for item in normalized]
    consumed = store.consumed_event_keys(keys)

    created_signal_ids: List[str] = []
    created = 0
    duplicates = 0
    skipped = 0
    for row, item, key in zip(rows, normalized, keys):
        if key in consumed:
            skipped += 1
            continue
        decision = decide_trigger(row, config=config, state=trigger_state,
                                  now=datetime.now().astimezone())
        if not decision["trigger"]:
            skipped_by_trigger += 1
            continue
        result = ensure_event_task(
            store, event=item, config=config, purpose=purpose,
            actor=actor, execution_mode=execution_mode)
        if result["status"] == "RECORDED":
            created += 1
            created_signal_ids.append(item["signal_id"])
            for symbol in decision.get("symbols") or []:
                trigger_state["symbol_cycle_counts"][symbol] = (
                    int(trigger_state["symbol_cycle_counts"].get(symbol, 0)) + 1)
                trigger_state["symbol_last_trigger"][symbol] = _now_iso()
        else:
            duplicates += 1
    return {
        "created": created,
        "duplicates": duplicates,
        "skipped": skipped,
        "skipped_by_trigger": skipped_by_trigger,
        "rejected_by_selection": int(selection.get("rejected") or 0),
        "created_signal_ids": created_signal_ids,
        "selection": selection,
    }
