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
from datetime import datetime, timedelta
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
    retried = 0
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
            # A crash either terminally failed the task or just consumed one
            # attempt and requeued it; count the two outcomes separately so
            # the report never overstates terminal failures.
            fresh = store.get_task(task["task_id"])
            if str(fresh.get("status")) == "failed":
                failed += 1
            else:
                retried += 1
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
        "retried": retried,
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
    from .research_trigger import (
        PRIORITY_RANKS as _PRIORITY_RANKS,
        cluster_key as _cluster_key,
        decide_trigger,
        research_trigger_config as _trigger_config,
    )
    from .models import now_iso as _now_iso

    trigger_cfg = _trigger_config(config or {})
    # Cross-event trigger state for this cycle: caps, cooldowns and story
    # clusters must see what earlier rows — and, within the cooldown window,
    # earlier cycles — already opened.
    trigger_state: Dict[str, Any] = {
        "symbol_cycle_counts": {}, "symbol_last_trigger": {}, "cluster_last_trigger": {},
    }
    _window_minutes = max(trigger_cfg["symbol_cooldown_minutes"],
                          trigger_cfg["cluster_cooldown_minutes"])
    if _window_minutes > 0:
        _seed_cross_cycle_state(store, trigger_state, within_minutes=_window_minutes)
    skipped_by_trigger = 0
    skipped_by_cluster = 0
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
    now_value = datetime.now().astimezone()

    def _member_order(row: Dict[str, Any]):
        # Story winner: official filing beats news, then higher priority,
        # then the earlier publication.
        return (
            0 if str(row.get("source_type") or "") == "filing" else 1,
            _PRIORITY_RANKS.get(str(row.get("priority") or "P3"), 3),
            str(row.get("published_at") or ""),
        )

    pending = []
    for row, item, key in zip(rows, normalized, keys):
        if key in consumed:
            skipped += 1
            continue
        pending.append((row, item, key))

    # Same-story clustering: content revisions always reopen research; the
    # cross-cycle window skips a whole story already covered recently;
    # otherwise the best member of each in-cycle cluster opens the task.
    ordered = sorted(pending, key=lambda entry: _member_order(entry[0]))
    if trigger_cfg["cluster_within_cycle"] or trigger_cfg["cluster_cooldown_minutes"] > 0:
        selected = []
        seen_clusters: set = set()
        for entry in ordered:
            row = entry[0]
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if str(payload.get("content_version") or "1") != "1":
                selected.append(entry)
                continue
            story = _cluster_key(row)
            if trigger_cfg["cluster_cooldown_minutes"] > 0:
                stamp = str(trigger_state["cluster_last_trigger"].get(story) or "")
                if stamp:
                    try:
                        covered_at = datetime.fromisoformat(stamp)
                        covered = (now_value - covered_at).total_seconds() \
                            < trigger_cfg["cluster_cooldown_minutes"] * 60
                    except ValueError:
                        covered = False
                    if covered:
                        skipped_by_cluster += 1
                        continue
            if trigger_cfg["cluster_within_cycle"]:
                if story in seen_clusters:
                    skipped_by_cluster += 1
                    continue
                seen_clusters.add(story)
            selected.append(entry)
    else:
        selected = ordered

    for row, item, key in selected:
        decision = decide_trigger(row, config=config, state=trigger_state,
                                  now=now_value)
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
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if str(payload.get("content_version") or "1") == "1":
                trigger_state["cluster_last_trigger"][_cluster_key(row)] = _now_iso()
        else:
            duplicates += 1
    return {
        "created": created,
        "duplicates": duplicates,
        "skipped": skipped,
        "skipped_by_trigger": skipped_by_trigger,
        "skipped_by_cluster": skipped_by_cluster,
        "rejected_by_selection": int(selection.get("rejected") or 0),
        "created_signal_ids": created_signal_ids,
        "selection": selection,
    }


def _seed_cross_cycle_state(store: Any, state: Dict[str, Any], *, within_minutes: int) -> None:
    """Derive cross-cycle cooldown state from recently created tasks.

    Recency is each task's created_at; symbols and the story title come from
    its input origin. Best-effort: malformed timestamps simply do not seed a
    cooldown, and the LATEST timestamp per symbol/story wins.
    """
    from .research_trigger import cluster_key as _cluster_key

    cutoff = datetime.now().astimezone() - timedelta(minutes=max(1, int(within_minutes)))

    def _remember(target: Dict[str, str], name: str, stamp: str) -> None:
        existing = str(target.get(name) or "")
        if not existing or stamp > existing:
            target[name] = stamp

    try:
        tasks = store.list_tasks()
    except Exception:
        return
    for task in tasks:
        created = str(task.get("created_at") or "")
        try:
            created_dt = datetime.fromisoformat(created)
        except ValueError:
            continue
        if created_dt.tzinfo is None:
            created_dt = created_dt.astimezone()
        if created_dt < cutoff:
            continue
        payload = task.get("input_payload") or {}
        if not isinstance(payload, dict):
            continue
        symbols = [str(symbol) for symbol in (payload.get("symbols") or []) if str(symbol)]
        origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        title = str(origin.get("title") or "")
        for symbol in symbols:
            _remember(state["symbol_last_trigger"], symbol, created)
        if title:
            story = _cluster_key({"title": title, "symbols": symbols})
            _remember(state["cluster_last_trigger"], story, created)
