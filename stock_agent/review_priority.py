from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List

from .config import project_path
from .models import now_iso
from .source_remediation import load_latest_remediation_summary


FORBIDDEN_ACTIONS_NOTE = "禁止自动交易、下单、调仓，需人工确认。"


def build_priority_queue() -> Dict[str, Any]:
    remediation = load_latest_remediation_summary()
    signals = _load_latest_signals()
    signal_by_id = {str(signal.get("signal_id") or ""): signal for signal in signals}
    records = list(remediation.get("remediation_decision_records", []) or [])
    source_failures = set(str(key) for key in (remediation.get("source_failure_counts", {}) or {}).keys())
    degraded_events = list(remediation.get("source_degraded_events", []) or [])
    for event in degraded_events:
        source_id = str(event.get("source_id") or "")
        if source_id:
            source_failures.add(source_id)

    tasks = []
    for task in remediation.get("remediation_queue", []) or []:
        enriched = dict(task)
        enriched["allowed_actions"] = _safe_allowed_actions(enriched.get("allowed_actions", []) or [])
        enriched["forbidden_actions_note"] = FORBIDDEN_ACTIONS_NOTE
        signal = signal_by_id.get(str(task.get("signal_id") or ""), {})
        scored = score_remediation_task(enriched, signal, records, source_failures)
        enriched.update(scored)
        tasks.append(enriched)

    tasks.sort(
        key=lambda item: (
            int(item.get("review_priority_score", 0)),
            _risk_rank(str(item.get("evidence_risk_level", ""))),
            _status_rank(str(item.get("status", ""))),
        ),
        reverse=True,
    )
    risk_counts = Counter(str(task.get("evidence_risk_level") or "low") for task in tasks)
    return {
        "generated_at": now_iso(),
        "top10": tasks[:10],
        "tasks": tasks,
        "risk_counts": {
            "high": risk_counts.get("high", 0),
            "medium": risk_counts.get("medium", 0),
            "low": risk_counts.get("low", 0),
        },
        "forbidden_actions_note": FORBIDDEN_ACTIONS_NOTE,
    }


def score_remediation_task(
    task: Dict[str, Any],
    signal: Dict[str, Any] | None = None,
    records: List[Dict[str, Any]] | None = None,
    source_failures: set[str] | None = None,
) -> Dict[str, Any]:
    signal = signal or {}
    records = records or []
    source_failures = source_failures or set()
    score = 0
    reasons: List[str] = []

    status = str(task.get("status") or "open")
    status_score = {"open": 25, "deferred": 10, "resolved": 0, "rejected": 0}.get(status, 0)
    if status_score:
        score += status_score
        reasons.append("task status {0}".format(status))

    priority = str(signal.get("priority") or task.get("signal_priority") or "P3")
    priority_score = {"P0": 30, "P1": 24, "P2": 16, "P3": 6}.get(priority, 0)
    if priority_score:
        score += priority_score
        reasons.append("signal priority {0}".format(priority))

    gap_type = str(task.get("gap_type") or "unknown")
    gap_score = {
        "official_filing": 25,
        "missing_url": 20,
        "source_degraded": 18,
        "missing_fields": 12,
        "coverage_gap": 10,
        "unknown": 8,
    }.get(gap_type, 8)
    score += gap_score
    reasons.append("gap type {0}".format(gap_type))

    source_id = str(task.get("source_id") or "")
    if gap_type == "source_degraded" or source_id in source_failures:
        score += 15
        reasons.append("source degraded or failed")

    llm = signal.get("llm_analysis", {}) if isinstance(signal.get("llm_analysis", {}), dict) else {}
    if llm.get("human_decision_needed"):
        score += 15
        reasons.append("LLM asks for human decision")
    try:
        confidence = float(llm.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence >= 0.75:
        score += 8
        reasons.append("LLM confidence >= 0.75")

    role_synthesis = signal.get("role_synthesis", {}) if isinstance(signal.get("role_synthesis", {}), dict) else {}
    conflict = str(role_synthesis.get("conflict_level") or "").lower()
    disagreement = str(role_synthesis.get("key_disagreement") or "").strip()
    if conflict in {"high", "severe"} or disagreement:
        score += 12
        reasons.append("multi-role disagreement")
    elif conflict == "medium":
        score += 6
        reasons.append("multi-role medium conflict")

    defer_count = _matching_defer_count(task, records)
    if defer_count:
        bump = min(10, defer_count * 5)
        score += bump
        reasons.append("manual defer history x{0}".format(defer_count))

    score = max(0, min(100, int(score)))
    return {
        "review_priority_score": score,
        "evidence_risk_level": risk_level_for_score(score),
        "priority_reasons": reasons,
    }


def risk_level_for_score(score: int | float) -> str:
    value = float(score)
    if value >= 70:
        return "high"
    if value >= 40:
        return "medium"
    return "low"


def write_priority_queue(queue: Dict[str, Any] | None = None) -> Dict[str, str]:
    queue = queue or build_priority_queue()
    out_dir = project_path("reports/workbench")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(queue.get("generated_at") or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    path = out_dir / "priority_queue_{0}.json".format(stamp)
    latest = out_dir / "priority_queue_latest.json"
    payload = json.dumps(queue, ensure_ascii=False, indent=2, default=str)
    path.write_text(payload, encoding="utf-8")
    latest.write_text(payload, encoding="utf-8")
    return {
        "priority_queue_path": str(path),
        "priority_queue_latest_path": str(latest),
    }


def load_latest_priority_queue() -> Dict[str, Any]:
    path = project_path("reports/workbench/priority_queue_latest.json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_latest_signals() -> List[Dict[str, Any]]:
    json_dir = project_path("reports/json")
    if not json_dir.exists():
        return []
    files = sorted(json_dir.glob("*.json"))
    if not files:
        return []
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.get("signals", []) or [])


def _safe_allowed_actions(actions: List[Any]) -> List[str]:
    safe = []
    for action in actions:
        text = str(action)
        lowered = text.lower()
        if any(term in lowered for term in ["buy", "sell", "order", "rebalance", "trade"]):
            continue
        safe.append(text)
    return safe


def _matching_defer_count(task: Dict[str, Any], records: List[Dict[str, Any]]) -> int:
    count = 0
    for record in records:
        if record.get("action") != "defer_remediation":
            continue
        if record.get("task_id") == task.get("task_id") or _stable_key(record) == _stable_key(task):
            count += 1
    return count


def _stable_key(item: Dict[str, Any]) -> tuple:
    return (
        str(item.get("task_type") or ""),
        str(item.get("source_id") or ""),
        str(item.get("signal_id") or ""),
        str(item.get("gap_type") or ""),
    )


def _risk_rank(level: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(level, 0)


def _status_rank(status: str) -> int:
    return {"open": 3, "deferred": 2, "resolved": 1, "rejected": 0}.get(status, 0)
