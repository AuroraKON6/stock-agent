from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any, Dict, Iterable, List

from .config import project_path
from .models import AnalyzedSignal, now_iso


VALID_DECISIONS = {
    "approve_research_followup",
    "request_more_data",
    "mark_as_noise",
    "defer_until_more_evidence",
    "confirm_signal",
    "reject_signal",
    "defer_signal",
}

VALID_OUTCOMES = {
    "",
    "unknown",
    "effective",
    "false_positive",
    "inconclusive",
    "pending",
}

FORBIDDEN_DECISIONS = {
    "auto_trade",
    "place_order",
    "order_placement",
    "rebalance_position",
    "position_rebalance",
    "buy",
    "sell",
}


def record_decision(
    signal_id: str,
    decision: str,
    notes: str = "",
    actor: str = "user",
    outcome: str = "",
    followup_result: str = "",
    evidence_refs: List[str] | None = None,
) -> Dict[str, Any]:
    decision = str(decision).strip()
    if decision in FORBIDDEN_DECISIONS or decision not in VALID_DECISIONS:
        raise ValueError("Invalid or unsafe decision: {0}".format(decision))
    outcome = str(outcome or "").strip()
    if outcome not in VALID_OUTCOMES:
        raise ValueError("Invalid decision outcome: {0}".format(outcome))
    decided_at = now_iso()
    record = {
        "record_id": "decision:{0}:{1}".format(_compact_time(decided_at), uuid.uuid4().hex[:8]),
        "signal_id": str(signal_id).strip(),
        "decision": decision,
        "decision_family": _decision_family(decision),
        "notes": str(notes or ""),
        "actor": str(actor or "user"),
        "outcome": outcome,
        "followup_result": str(followup_result or ""),
        "evidence_refs": [str(item) for item in (evidence_refs or [])],
        "decided_at": decided_at,
        "source": "manual_supervisor_decision",
        "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
    }
    if not record["signal_id"]:
        raise ValueError("signal_id is required")
    path = _decision_log_path(record["decided_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    _emit_decision_event(record)
    return record


def list_pending_decisions() -> List[Dict[str, Any]]:
    queue = _load_latest_human_intervention_queue()
    decided = load_decision_context().get("by_signal", {})
    return [item for item in queue if item.get("signal_id") not in decided]


def load_decision_context() -> Dict[str, Any]:
    records = _load_decision_records()
    by_signal: Dict[str, Dict[str, Any]] = {}
    for record in records:
        signal_id = record.get("signal_id")
        if signal_id:
            by_signal[signal_id] = record
    return {
        "records": records,
        "by_signal": by_signal,
        "latest": list(by_signal.values())[-50:],
    }


def apply_decision_context(
    signals: List[AnalyzedSignal],
    decision_context: Dict[str, Any],
) -> List[AnalyzedSignal]:
    by_signal = decision_context.get("by_signal", {}) if decision_context else {}
    for signal in signals:
        decision = by_signal.get(signal.signal_id)
        if decision:
            signal.human_decision = decision
    return signals


def compute_decision_summary() -> Dict[str, Any]:
    context = load_decision_context()
    records = context.get("records", [])
    counts = Counter(record.get("decision", "unknown") for record in records)
    family_counts = Counter(record.get("decision_family", "legacy") for record in records)
    outcome_counts = Counter(record.get("outcome", "unknown") or "unknown" for record in records)
    return {
        "generated_at": now_iso(),
        "total": len(records),
        "decision_counts": dict(counts),
        "decision_family_counts": dict(family_counts),
        "outcome_counts": dict(outcome_counts),
        "latest_by_signal": context.get("by_signal", {}),
        "pending": list_pending_decisions(),
        "valid_decisions": sorted(VALID_DECISIONS),
        "valid_outcomes": sorted(VALID_OUTCOMES),
    }


def write_decision_summary(summary: Dict[str, Any] | None = None) -> Dict[str, str]:
    summary = summary or compute_decision_summary()
    stamp = summary.get("generated_at", now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    out_dir = project_path("reports/decisions")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "decision_summary_{0}.md".format(stamp)
    json_path = out_dir / "decision_summary_{0}.json".format(stamp)
    latest_md = out_dir / "latest.md"
    latest_json = out_dir / "latest.json"
    md = _render_decision_summary_markdown(summary)
    js = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    md_path.write_text(md, encoding="utf-8-sig")
    latest_md.write_text(md, encoding="utf-8-sig")
    json_path.write_text(js, encoding="utf-8")
    latest_json.write_text(js, encoding="utf-8")
    return {
        "decision_summary_path": str(md_path),
        "decision_summary_json_path": str(json_path),
        "decision_summary_latest_path": str(latest_md),
        "decision_summary_latest_json_path": str(latest_json),
    }


def _decision_log_path(decided_at: str):
    date_part = decided_at[:10].replace("-", "")
    return project_path("data/decisions/decision_log_{0}.jsonl".format(date_part))


def _compact_time(value: str) -> str:
    return str(value or now_iso()).replace(":", "").replace("-", "").replace("+", "_").split(".")[0]


def _emit_decision_event(record: Dict[str, Any]) -> None:
    try:
        from . import brokerage_architecture
        original_project_path = brokerage_architecture.project_path
        brokerage_architecture.project_path = project_path
        try:
            brokerage_architecture.record_decision_event(record)
        finally:
            brokerage_architecture.project_path = original_project_path
    except Exception:
        return


def _decision_family(decision: str) -> str:
    if decision in {"confirm_signal", "approve_research_followup"}:
        return "confirm"
    if decision in {"reject_signal", "mark_as_noise"}:
        return "reject"
    if decision in {"defer_signal", "defer_until_more_evidence", "request_more_data"}:
        return "defer"
    return "other"


def _load_decision_records() -> List[Dict[str, Any]]:
    decision_dir = project_path("data/decisions")
    if not decision_dir.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(decision_dir.glob("decision_log_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        records.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _load_latest_human_intervention_queue() -> List[Dict[str, Any]]:
    path = project_path("reports/team/latest.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    queue = data.get("human_intervention_queue", [])
    return [item for item in queue if isinstance(item, dict)]


def _render_decision_summary_markdown(summary: Dict[str, Any]) -> str:
    lines = ["# 人工决策汇总", ""]
    lines.append("- 生成时间: {0}".format(summary.get("generated_at", "")))
    lines.append("- 已记录决策: {0}".format(summary.get("total", 0)))
    lines.append("")
    lines.append("## 决策分布")
    lines.append("")
    counts = summary.get("decision_counts", {})
    if counts:
        for decision, count in sorted(counts.items()):
            lines.append("- {0}: {1}".format(decision, count))
    else:
        lines.append("- 暂无人工决策。")
    lines.append("")
    lines.append("## 待上司处理")
    lines.append("")
    lines.append("## Decision Outcomes")
    lines.append("")
    outcomes = summary.get("outcome_counts", {})
    if outcomes:
        for outcome, count in sorted(outcomes.items()):
            lines.append("- {0}: {1}".format(outcome, count))
    else:
        lines.append("- no recorded outcomes")
    lines.append("")
    pending = summary.get("pending", [])
    if pending:
        for item in pending:
            lines.append("- [{0}] {1}: {2}".format(
                item.get("priority", ""),
                item.get("signal_id", ""),
                item.get("title", ""),
            ))
    else:
        lines.append("- 暂无待处理事项。")
    lines.append("")
    lines.append("## 有效决策类型")
    lines.append("")
    for decision in summary.get("valid_decisions", sorted(VALID_DECISIONS)):
        lines.append("- {0}".format(decision))
    return "\n".join(lines)
