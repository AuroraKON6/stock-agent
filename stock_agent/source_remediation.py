from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from .config import project_path
from .models import RawSignal, now_iso
from .text import unique_keep_order


def split_source_degraded_signals(signals: Iterable[RawSignal]) -> Tuple[List[RawSignal], List[Dict[str, Any]]]:
    normal: List[RawSignal] = []
    degraded: List[Dict[str, Any]] = []
    for signal in signals:
        if getattr(signal, "source_type", "") == "source_error":
            degraded.append(_source_degraded_event(
                source_id=str(getattr(signal, "source", "") or "unknown"),
                source_kind=str(getattr(signal, "source", "") or "unknown"),
                detail=str(getattr(signal, "summary", "") or getattr(signal, "title", "")),
                title=str(getattr(signal, "title", "") or "source degraded"),
                fallback_candidates=[],
            ))
        else:
            normal.append(signal)
    return normal, degraded


def build_source_degraded_events(
    events: Iterable[Dict[str, Any]],
    config: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    config = config or {}
    degraded: List[Dict[str, Any]] = []
    for event in events:
        signals = list(event.get("signals", []) or [])
        source_errors = [signal for signal in signals if getattr(signal, "source_type", "") == "source_error"]
        exception = str(event.get("exception") or "").strip()
        if not exception and not source_errors:
            continue
        details = []
        if exception:
            details.append(exception)
        for signal in source_errors:
            details.append(str(getattr(signal, "summary", "") or getattr(signal, "title", "")))
        degraded.append(_source_degraded_event(
            source_id=str(event.get("source_id") or event.get("source_kind") or "unknown"),
            source_kind=str(event.get("source_kind") or event.get("source_id") or "unknown"),
            detail="; ".join(unique_keep_order(details)),
            title="{0} source degraded".format(event.get("source_id") or event.get("source_kind") or "unknown"),
            fallback_candidates=_fallback_candidates(str(event.get("source_kind") or event.get("source_id") or ""), config),
            fallback_resolved=bool(event.get("fallback_resolved", False)),
            latency_ms=int(event.get("latency_ms", 0) or 0),
            expected_symbols=[str(item) for item in event.get("expected_symbols", []) or []],
        ))
    return degraded


def build_remediation_summary(
    signals: Iterable[Any],
    source_health: Dict[str, Any] | None,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    source_health = source_health or {}
    config = config or {}
    degraded_events = list(source_health.get("source_degraded_events", []) or [])
    tasks: List[Dict[str, Any]] = []

    for event in degraded_events:
        tasks.append(_task_from_degraded_event(event, len(tasks) + 1))

    for item in source_health.get("sources", []) or []:
        if isinstance(item, dict):
            tasks.extend(_tasks_from_source_health_item(item, len(tasks) + 1, config))

    for signal in signals:
        for gap in _signal_gaps(signal):
            tasks.append(_task_from_signal_gap(signal, gap, len(tasks) + 1, config))

    tasks = _dedupe_tasks(tasks)
    decision_records = load_remediation_decision_records()
    tasks = _apply_decision_records_to_tasks(tasks, decision_records)
    gap_counts = Counter(task.get("gap_type", "unknown") for task in tasks)
    source_failures = Counter(str(event.get("source_id", "unknown")) for event in degraded_events)
    status_counts = Counter(str(task.get("status", "open")) for task in tasks)
    return {
        "generated_at": now_iso(),
        "source_degraded_event_count": len(degraded_events),
        "source_degraded_events": degraded_events,
        "remediation_queue_count": len(tasks),
        "remediation_queue": tasks,
        "remediation_gap_type_counts": dict(sorted(gap_counts.items())),
        "remediation_status_counts": dict(sorted(status_counts.items())),
        "source_failure_counts": dict(sorted(source_failures.items())),
        "remediation_decision_records": decision_records[-20:],
        "open_count": status_counts.get("open", 0),
        "resolved_count": status_counts.get("resolved", 0),
        "rejected_count": status_counts.get("rejected", 0),
        "deferred_count": status_counts.get("deferred", 0),
    }


def write_remediation_summary(summary: Dict[str, Any]) -> Dict[str, str]:
    out_dir = project_path("reports/remediation")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary.get("generated_at", now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    json_path = out_dir / "remediation_{0}.json".format(stamp)
    md_path = out_dir / "remediation_{0}.md".format(stamp)
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")
    md = _render_markdown(summary)
    md_path.write_text(md, encoding="utf-8-sig")
    latest_md.write_text(md, encoding="utf-8-sig")
    return {
        "remediation_path": str(md_path),
        "remediation_json_path": str(json_path),
        "remediation_latest_path": str(latest_md),
        "remediation_latest_json_path": str(latest_json),
    }


def load_latest_remediation_summary() -> Dict[str, Any]:
    path = project_path("reports/remediation/latest.json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def summarize_remediation_from_report(data: Dict[str, Any]) -> Dict[str, Any]:
    stats = data.get("stats", {}) or {}
    queue = list(stats.get("remediation_queue", []) or [])
    degraded = list(stats.get("source_degraded_events", []) or [])
    if not queue and data.get("signals"):
        queue = build_remediation_summary(data.get("signals", []), stats.get("source_health", {}), {}).get("remediation_queue", [])
    status_counts = Counter(str(task.get("status", "open")) for task in queue)
    gap_counts = Counter(str(task.get("gap_type") or task.get("task_type") or "unknown") for task in queue)
    task_type_counts = Counter(str(task.get("task_type") or "unknown") for task in queue)
    source_counts = Counter(str(event.get("source_id", "unknown")) for event in degraded)
    for task in queue:
        source_id = str(task.get("source_id") or "")
        if source_id and source_id not in source_counts:
            source_counts[source_id] += 0
    average_hours = _average_resolution_hours(queue)
    total = len(queue)
    resolved_count = status_counts.get("resolved", 0)
    return {
        "source_degraded_event_count": len(degraded),
        "remediation_queue_count": len(queue),
        "open_count": status_counts.get("open", 0),
        "resolved_count": resolved_count,
        "rejected_count": status_counts.get("rejected", 0),
        "deferred_count": status_counts.get("deferred", 0),
        "completion_rate": resolved_count / total if total else 0.0,
        "average_resolution_hours": average_hours,
        "status_counts": dict(sorted(status_counts.items())),
        "gap_type_counts": dict(stats.get("remediation_gap_type_counts") or sorted(gap_counts.items())),
        "common_task_types": dict(task_type_counts.most_common(10)),
        "source_failure_counts": dict(sorted(source_counts.items())),
        "recent_records": list(stats.get("remediation_decision_records", []) or [])[-20:],
    }


def attach_evidence_url(task_id: str, evidence_url: str, actor: str = "user", notes: str = "") -> Dict[str, Any]:
    return record_remediation_action(
        task_id,
        "attach_evidence_url",
        actor=actor,
        notes=notes,
        status="resolved",
        evidence_url=evidence_url,
    )


def attach_structured_fields(
    task_id: str,
    structured_fields: Dict[str, Any],
    actor: str = "user",
    notes: str = "",
) -> Dict[str, Any]:
    return record_remediation_action(
        task_id,
        "attach_structured_fields",
        actor=actor,
        notes=notes,
        status="resolved",
        structured_fields=structured_fields,
    )


def resolve_remediation_task(task_id: str, notes: str = "", actor: str = "user") -> Dict[str, Any]:
    return record_remediation_action(task_id, "resolve_remediation", actor=actor, notes=notes, status="resolved")


def reject_remediation_task(task_id: str, reason: str = "", actor: str = "user") -> Dict[str, Any]:
    return record_remediation_action(task_id, "reject_remediation", actor=actor, notes=reason, status="rejected")


def defer_remediation_task(task_id: str, reason: str = "", actor: str = "user") -> Dict[str, Any]:
    return record_remediation_action(task_id, "defer_remediation", actor=actor, notes=reason, status="deferred")


def record_remediation_action(
    task_id: str,
    action: str,
    *,
    actor: str = "user",
    notes: str = "",
    status: str = "open",
    evidence_url: str = "",
    structured_fields: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if action not in _allowed_record_actions():
        raise ValueError("Unsupported remediation action: {0}".format(action))
    if status not in {"open", "resolved", "rejected", "deferred"}:
        raise ValueError("Unsupported remediation status: {0}".format(status))
    summary = _load_latest_summary()
    task = _find_task(summary, task_id)
    recorded_at = now_iso()
    record = {
        "record_id": "remediation:{0}:{1}".format(
            recorded_at.replace(":", "").replace("-", "").replace("+", "_").split(".")[0],
            uuid.uuid4().hex[:8],
        ),
        "task_id": task_id,
        "task_type": task.get("task_type", ""),
        "gap_type": task.get("gap_type", ""),
        "source_id": task.get("source_id", ""),
        "signal_id": task.get("signal_id", ""),
        "action": action,
        "status": status,
        "actor": actor,
        "notes": notes,
        "evidence_url": evidence_url,
        "structured_fields": structured_fields or {},
        "recorded_at": recorded_at,
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }
    _append_decision_log(record)
    _apply_record_to_task(task, record)
    summary["remediation_decision_records"] = load_remediation_decision_records()[-20:]
    _refresh_summary_counts(summary)
    _write_latest_summary(summary)
    _emit_remediation_event(task, record)
    return record


def load_remediation_decision_records() -> List[Dict[str, Any]]:
    log_dir = project_path("data/remediation")
    if not log_dir.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(log_dir.glob("decision_log_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def _source_degraded_event(
    source_id: str,
    source_kind: str,
    detail: str,
    title: str,
    fallback_candidates: List[str],
    fallback_resolved: bool = False,
    latency_ms: int = 0,
    expected_symbols: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "event_type": "source_degraded",
        "source_id": source_id,
        "source_kind": source_kind,
        "severity": "WARN" if fallback_resolved else "FAIL",
        "title": title,
        "detail": detail,
        "detected_at": now_iso(),
        "latency_ms": latency_ms,
        "expected_symbols": expected_symbols or [],
        "fallback_resolved": bool(fallback_resolved),
        "fallback_candidates": fallback_candidates,
        "allowed_actions": ["retry_source", "enable_fallback_source", "open_source_health_report", "defer_signal"],
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }


def _allowed_record_actions() -> set:
    return {
        "attach_evidence_url",
        "attach_structured_fields",
        "resolve_remediation",
        "reject_remediation",
        "defer_remediation",
    }


def _load_latest_summary() -> Dict[str, Any]:
    path = project_path("reports/remediation/latest.json")
    if not path.exists():
        raise FileNotFoundError("No remediation latest.json found at {0}".format(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid remediation latest.json: {0}".format(exc)) from exc


def _find_task(summary: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    for task in summary.get("remediation_queue", []) or []:
        if task.get("task_id") == task_id:
            return task
    raise KeyError("Remediation task not found: {0}".format(task_id))


def _append_decision_log(record: Dict[str, Any]) -> str:
    log_dir = project_path("data/remediation")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    path = log_dir / "decision_log_{0}.jsonl".format(stamp[:8])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return str(path)


def _apply_decision_records_to_tasks(tasks: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {task.get("task_id"): task for task in tasks}
    by_stable_key = {
        key: task
        for task in tasks
        for key in [_remediation_stable_key(task)]
        if key
    }
    for record in records:
        task = by_id.get(record.get("task_id"))
        if not task:
            task = by_stable_key.get(_remediation_stable_key(record))
        if task:
            _apply_record_to_task(task, record)
    return tasks


def _remediation_stable_key(item: Dict[str, Any]) -> tuple | None:
    key = (
        str(item.get("task_type") or ""),
        str(item.get("source_id") or ""),
        str(item.get("signal_id") or ""),
        str(item.get("gap_type") or ""),
    )
    if not any(key):
        return None
    return key


def _apply_record_to_task(task: Dict[str, Any], record: Dict[str, Any]) -> None:
    status = str(record.get("status") or "")
    if status:
        task["status"] = status
    task["last_action"] = record.get("action", "")
    task["last_actor"] = record.get("actor", "")
    task["last_notes"] = record.get("notes", "")
    task["updated_at"] = record.get("recorded_at", now_iso())
    if record.get("evidence_url"):
        task["evidence_url"] = record.get("evidence_url")
    if record.get("structured_fields"):
        existing = task.get("structured_fields", {})
        if not isinstance(existing, dict):
            existing = {}
        existing.update(record.get("structured_fields", {}) or {})
        task["structured_fields"] = existing
    if status == "resolved":
        task["resolved_at"] = record.get("recorded_at", now_iso())
        task["resolved_by"] = record.get("actor", "")
    elif status == "rejected":
        task["rejected_at"] = record.get("recorded_at", now_iso())
        task["rejected_by"] = record.get("actor", "")
    elif status == "deferred":
        task["deferred_at"] = record.get("recorded_at", now_iso())
        task["deferred_by"] = record.get("actor", "")
    history = task.get("decision_history", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "action": record.get("action", ""),
        "status": record.get("status", ""),
        "actor": record.get("actor", ""),
        "notes": record.get("notes", ""),
        "recorded_at": record.get("recorded_at", ""),
    })
    task["decision_history"] = history[-20:]


def _refresh_summary_counts(summary: Dict[str, Any]) -> None:
    queue = list(summary.get("remediation_queue", []) or [])
    status_counts = Counter(str(task.get("status", "open")) for task in queue)
    gap_counts = Counter(str(task.get("gap_type") or "unknown") for task in queue)
    summary["remediation_queue_count"] = len(queue)
    summary["remediation_status_counts"] = dict(sorted(status_counts.items()))
    summary["remediation_gap_type_counts"] = dict(sorted(gap_counts.items()))
    summary["open_count"] = status_counts.get("open", 0)
    summary["resolved_count"] = status_counts.get("resolved", 0)
    summary["rejected_count"] = status_counts.get("rejected", 0)
    summary["deferred_count"] = status_counts.get("deferred", 0)


def _write_latest_summary(summary: Dict[str, Any]) -> None:
    out_dir = project_path("reports/remediation")
    out_dir.mkdir(parents=True, exist_ok=True)
    _refresh_summary_counts(summary)
    (out_dir / "latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "latest.md").write_text(_render_markdown(summary), encoding="utf-8-sig")


def _emit_remediation_event(task: Dict[str, Any], record: Dict[str, Any]) -> None:
    try:
        from . import brokerage_architecture
        original_project_path = brokerage_architecture.project_path
        brokerage_architecture.project_path = project_path
        try:
            brokerage_architecture.record_remediation_event(task, record)
        finally:
            brokerage_architecture.project_path = original_project_path
    except Exception:
        return


def _average_resolution_hours(queue: List[Dict[str, Any]]) -> float:
    values: List[float] = []
    for task in queue:
        if task.get("status") != "resolved":
            continue
        created = _parse_dt(task.get("created_at"))
        resolved = _parse_dt(task.get("resolved_at") or task.get("updated_at"))
        if not created or not resolved:
            continue
        seconds = max(0.0, (resolved - created).total_seconds())
        values.append(seconds / 3600.0)
    if not values:
        return 0.0
    return round(sum(values) / len(values), 3)


def _parse_dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _fallback_candidates(source_kind: str, config: Dict[str, Any]) -> List[str]:
    sources = config.get("sources", {}) if isinstance(config.get("sources", {}), dict) else {}
    candidates: List[str] = []
    if source_kind in ("eastmoney", "akshare", "pysnowball", "alltick", "free_quote", "market_cache"):
        for name in ["eastmoney", "akshare", "pysnowball", "alltick", "free_quote", "market_cache"]:
            if name != source_kind:
                candidates.append(name)
    elif source_kind == "eastmoney_filing":
        candidates.extend(["filing_jsonl", "local_jsonl"])
    elif source_kind in ("rss", "local_jsonl"):
        candidates.extend(["local_jsonl", "rss"])
    for name, value in sources.items():
        if name not in candidates and _source_config_enabled(value):
            candidates.append(str(name))
    return unique_keep_order(candidates)


def _source_config_enabled(value: Any) -> bool:
    if isinstance(value, list):
        return any(bool(item.get("enabled", True)) for item in value if isinstance(item, dict))
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return False


def _signal_gaps(signal: Any) -> List[str]:
    gaps: List[str] = []
    if isinstance(signal, dict):
        gap_tasks = signal.get("evidence_gap_tasks", []) or []
        payload = signal.get("payload", {}) or {}
        url = signal.get("url", "")
    else:
        gap_tasks = getattr(signal, "evidence_gap_tasks", []) or []
        payload = getattr(signal, "payload", {}) or {}
        url = getattr(signal, "url", "")
    for task in gap_tasks:
        if isinstance(task, dict):
            gaps.append(str(task.get("gap") or task.get("gap_type") or ""))
        else:
            gaps.append(str(task))
    source_quality = payload.get("source_quality", {}) if isinstance(payload, dict) else {}
    if isinstance(source_quality, dict):
        gaps.extend(str(item) for item in source_quality.get("evidence_gaps", []) or [])
    filing = payload.get("filing_evidence", {}) if isinstance(payload, dict) else {}
    if isinstance(filing, dict):
        gaps.extend(str(item) for item in filing.get("missing_fields", []) or [])
        status = str(filing.get("status") or "")
        if status and status not in ("ok", "complete"):
            gaps.append("official_filing")
    if not url:
        source_type = str(signal.get("source_type", "") if isinstance(signal, dict) else getattr(signal, "source_type", ""))
        if source_type in ("filing", "news", "manual", "social"):
            gaps.append("missing_url")
    return unique_keep_order(gap for gap in gaps if str(gap).strip())


def _task_from_degraded_event(event: Dict[str, Any], idx: int) -> Dict[str, Any]:
    source_id = str(event.get("source_id") or "unknown")
    return {
        "task_id": "remediation:source:{0}:{1:03d}".format(source_id, idx),
        "task_type": "repair_source",
        "gap_type": "source_degraded",
        "gap": "source_degraded",
        "source_id": source_id,
        "signal_id": "",
        "status": "open",
        "owner": "human",
        "created_at": now_iso(),
        "detail": event.get("detail", ""),
        "fallback_candidates": event.get("fallback_candidates", []) or [],
        "allowed_actions": ["retry_source", "enable_fallback_source", "open_source_health_report", "defer_signal"],
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }


def _tasks_from_source_health_item(item: Dict[str, Any], start_idx: int, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    status = str(item.get("status") or "")
    if status not in ("WARN", "FAIL"):
        return []
    source_id = str(item.get("source_id") or item.get("source_kind") or "unknown")
    source_kind = str(item.get("source_kind") or source_id)
    reasons = {str(value) for value in item.get("anomaly_reasons", []) or []}
    evidence_gaps = {str(value) for value in item.get("evidence_gaps", []) or []}
    coverage_symbols = [str(value) for value in item.get("coverage_gap_symbols", []) or [] if str(value).strip()]
    fallback_candidates = _fallback_candidates(source_kind, config)
    if source_id and source_id not in fallback_candidates:
        fallback_candidates.append(source_id)
    tasks: List[Dict[str, Any]] = []

    def add_task(task_type: str, gap_type: str, gap: str, allowed_actions: List[str]) -> None:
        idx = start_idx + len(tasks)
        tasks.append({
            "task_id": "remediation:source:{0}:{1:03d}".format(source_id, idx),
            "task_type": task_type,
            "gap_type": gap_type,
            "gap": gap,
            "source_id": source_id,
            "signal_id": "",
            "symbol": ",".join(coverage_symbols),
            "status": "open",
            "owner": "human",
            "created_at": now_iso(),
            "detail": "status={0}; score={1}; reasons={2}".format(
                status,
                item.get("score", ""),
                ",".join(sorted(reasons)) or "-",
            ),
            "fallback_candidates": fallback_candidates,
            "allowed_actions": allowed_actions,
            "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
        })

    if coverage_symbols or any(str(gap).startswith("coverage:") for gap in evidence_gaps):
        add_task(
            "fill_source_coverage",
            "coverage_gap",
            "coverage:{0}".format(",".join(coverage_symbols) if coverage_symbols else "unknown"),
            _allowed_actions("coverage_gap"),
        )
    if "field_missing" in reasons or "missing_fields" in evidence_gaps:
        add_task(
            "fill_missing_fields",
            "missing_fields",
            "missing_fields",
            _allowed_actions("missing_fields"),
        )
    if "duplicates" in reasons or int(item.get("duplicate_count", 0) or 0) > 0:
        add_task(
            "reduce_source_duplicates",
            "duplicates",
            "duplicates",
            _allowed_actions("duplicates"),
        )
    if "low_trust" in reasons:
        add_task(
            "verify_source_trust",
            "low_trust",
            "low_trust",
            _allowed_actions("low_trust"),
        )
    if status == "FAIL" and not tasks:
        add_task(
            "review_source_quality",
            "source_quality",
            "source_quality",
            _allowed_actions("source_quality"),
        )
    return tasks


def _task_from_signal_gap(signal: Any, gap: str, idx: int, config: Dict[str, Any]) -> Dict[str, Any]:
    signal_id = str(signal.get("signal_id", "") if isinstance(signal, dict) else getattr(signal, "signal_id", ""))
    source_id = _signal_source_id(signal)
    gap_type = _gap_type(gap)
    task_type = _task_type(gap_type)
    return {
        "task_id": "remediation:signal:{0}:{1:03d}".format(signal_id or source_id, idx),
        "task_type": task_type,
        "gap_type": gap_type,
        "gap": gap,
        "source_id": source_id,
        "signal_id": signal_id,
        "symbol": _signal_symbol(signal),
        "status": _gap_status(signal, gap),
        "owner": "human",
        "created_at": now_iso(),
        "fallback_candidates": _fallback_candidates(source_id, config),
        "allowed_actions": _allowed_actions(gap_type),
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }


def _signal_source_id(signal: Any) -> str:
    payload = signal.get("payload", {}) if isinstance(signal, dict) else getattr(signal, "payload", {}) or {}
    quality = payload.get("source_quality", {}) if isinstance(payload, dict) else {}
    if isinstance(quality, dict) and quality.get("source_id"):
        return str(quality.get("source_id"))
    return str(signal.get("source", "") if isinstance(signal, dict) else getattr(signal, "source", ""))


def _signal_symbol(signal: Any) -> str:
    payload = signal.get("payload", {}) if isinstance(signal, dict) else getattr(signal, "payload", {}) or {}
    if isinstance(payload, dict):
        for key in ["symbol", "code"]:
            if payload.get(key):
                return str(payload.get(key))
        filing = payload.get("filing_evidence", {})
        if isinstance(filing, dict) and filing.get("symbol"):
            return str(filing.get("symbol"))
    symbols = signal.get("symbols", []) if isinstance(signal, dict) else getattr(signal, "symbols", []) or []
    return str(symbols[0]) if symbols else ""


def _gap_status(signal: Any, gap: str) -> str:
    tasks = signal.get("evidence_gap_tasks", []) if isinstance(signal, dict) else getattr(signal, "evidence_gap_tasks", []) or []
    for task in tasks:
        if isinstance(task, dict) and str(task.get("gap") or task.get("gap_type") or "") == gap:
            return str(task.get("status") or "open")
    return "open"


def _gap_type(gap: str) -> str:
    text = str(gap or "").lower()
    if "official_filing" in text or "filing_status" in text:
        return "official_filing"
    if "url" in text:
        return "missing_url"
    if "missing_fields" in text or text in {"announcement_type", "report_period", "title", "summary"}:
        return "missing_fields"
    if "coverage:" in text:
        return "coverage_gap"
    if "source_error" in text or "collector_exception" in text:
        return "source_degraded"
    return "unknown"


def _task_type(gap_type: str) -> str:
    return {
        "missing_url": "fill_missing_url",
        "missing_fields": "fill_missing_fields",
        "official_filing": "verify_official_filing",
        "source_degraded": "repair_source",
        "coverage_gap": "fill_source_coverage",
    }.get(gap_type, "review_evidence_gap")


def _allowed_actions(gap_type: str) -> List[str]:
    mapping = {
        "missing_url": ["attach_evidence_url", "verify_source_url", "defer_signal"],
        "missing_fields": ["attach_structured_fields", "request_more_data", "defer_signal"],
        "official_filing": ["verify_official_filing", "attach_filing_url", "confirm_signal", "reject_signal", "defer_signal"],
        "source_degraded": ["retry_source", "enable_fallback_source", "open_source_health_report", "defer_signal"],
        "coverage_gap": ["retry_source", "enable_fallback_source", "request_more_data"],
        "duplicates": ["review_dedupe_rule", "adjust_source_weight", "defer_signal"],
        "low_trust": ["verify_source_trust", "replace_source", "defer_signal"],
        "source_quality": ["open_source_health_report", "request_more_data", "defer_signal"],
    }
    return mapping.get(gap_type, ["request_more_data", "defer_signal"])


def _dedupe_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for task in tasks:
        key = (task.get("task_type"), task.get("source_id"), task.get("signal_id"), task.get("gap_type"))
        if key in seen:
            continue
        seen.add(key)
        result.append(task)
    return result


def _render_markdown(summary: Dict[str, Any]) -> str:
    lines = ["# Source Remediation Queue", ""]
    lines.append("- generated_at: {0}".format(summary.get("generated_at", "")))
    lines.append("- source_degraded_event_count: {0}".format(summary.get("source_degraded_event_count", 0)))
    lines.append("- remediation_queue_count: {0}".format(summary.get("remediation_queue_count", 0)))
    lines.append("- open_count: {0}".format(summary.get("open_count", 0)))
    lines.append("- resolved_count: {0}".format(summary.get("resolved_count", 0)))
    lines.append("")
    lines.append("## Gap Types")
    for gap_type, count in sorted((summary.get("remediation_gap_type_counts", {}) or {}).items()):
        lines.append("- {0}: {1}".format(gap_type, count))
    if not summary.get("remediation_gap_type_counts"):
        lines.append("- none")
    lines.append("")
    lines.append("## Tasks")
    for task in summary.get("remediation_queue", [])[:50]:
        lines.append("- [{0}] {1} source={2} signal={3} gap={4}".format(
            task.get("status", ""),
            task.get("task_type", ""),
            task.get("source_id", ""),
            task.get("signal_id", "") or "-",
            task.get("gap", ""),
        ))
    if not summary.get("remediation_queue"):
        lines.append("- no tasks")
    return "\n".join(lines)
