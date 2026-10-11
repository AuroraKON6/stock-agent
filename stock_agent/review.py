from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import project_path
from .models import now_iso


def export_review(config: Dict[str, Any], signals: Optional[List[Dict[str, Any]]] = None) -> str:
    if signals is None:
        signals = _load_signals_from_latest_json()
    existing_ids = _existing_review_signal_ids()
    qualifying = [
        s for s in signals
        if _is_qualifying(s) and str(s.get("signal_id") or "") not in existing_ids
    ]
    if not qualifying:
        return ""
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    path = project_path("data/review/pending_review_{0}.jsonl".format(stamp))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for s in qualifying:
        lines.append(json.dumps(_build_review_record(s), ensure_ascii=False))
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _existing_review_signal_ids() -> set:
    review_dir = project_path("data/review")
    if not review_dir.exists():
        return set()
    signal_ids = set()
    for pattern in ("pending_review_*.jsonl", "completed_review_*.jsonl"):
        for path in review_dir.glob(pattern):
            try:
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    signal_id = str(item.get("signal_id") or "").strip()
                    if signal_id:
                        signal_ids.add(signal_id)
            except (OSError, json.JSONDecodeError):
                continue
    return signal_ids


def _load_signals_from_latest_json() -> List[Dict[str, Any]]:
    json_dir = project_path("reports/json")
    if not json_dir.exists():
        return []
    files = sorted(json_dir.glob("*.json"))
    if not files:
        return []
    latest = files[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        return data.get("signals", [])
    except (json.JSONDecodeError, OSError):
        return []


def _is_qualifying(signal: Dict[str, Any]) -> bool:
    priority = signal.get("priority", "P3")
    if priority in ("P0", "P1", "P2"):
        return True
    if signal.get("needs_review"):
        return True
    if signal.get("evidence_gap_tasks"):
        return True
    llm = signal.get("llm_analysis")
    if llm and isinstance(llm, dict) and len(llm) > 0:
        return True
    if signal.get("source_type") == "price":
        reasons = signal.get("reasons", [])
        if any(("异常" in r or "显著" in r) for r in reasons):
            return True
    return False


def _build_review_record(signal: Dict[str, Any]) -> Dict[str, Any]:
    llm = signal.get("llm_analysis") or {}
    review_questions = []
    if isinstance(llm, dict):
        review_questions = llm.get("review_questions", [])
    return {
        "signal_id": signal.get("signal_id", ""),
        "title": signal.get("title", ""),
        "priority": signal.get("priority", "P3"),
        "source": signal.get("source", ""),
        "source_type": signal.get("source_type", ""),
        "topics": signal.get("topics", []),
        "symbols": signal.get("symbols", []),
        "llm_analysis": llm,
        "role_analyses": signal.get("role_analyses", []),
        "role_synthesis": signal.get("role_synthesis", {}),
        "evidence_chain": signal.get("evidence_chain", {}),
        "evidence_gap_tasks": signal.get("evidence_gap_tasks", []),
        "reasons": signal.get("reasons", []),
        "suggested_review_questions": review_questions,
        "decision_taken": "",
        "actual_price_move": "",
        "actual_news_followup": "",
        "was_effective": "unknown",
        "false_positive_reason": "",
        "notes": "",
        "reviewed_at": "",
    }


def compute_review_summary() -> Dict[str, Any]:
    review_dir = project_path("data/review")
    if not review_dir.exists():
        return _empty_summary()
    files = sorted(review_dir.glob("completed_review_*.jsonl"))
    if not files:
        return _empty_summary()

    records: List[Dict[str, Any]] = []
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            continue

    if not records:
        return _empty_summary()

    total = len(records)
    effective = [r for r in records if r.get("was_effective") == "true"]
    ineffective = [r for r in records if r.get("was_effective") == "false"]
    unknown = [r for r in records if r.get("was_effective") not in ("true", "false")]

    effective_count = len(effective)
    ineffective_count = len(ineffective)
    unknown_count = len(unknown)

    rate_by_priority = _rate_by_key(records, "priority")
    rate_by_source = _rate_by_key(records, "source")
    rate_by_source_type = _rate_by_key(records, "source_type")
    rate_by_topic = _rate_by_topics(records)

    fp_counter: Counter = Counter()
    for r in records:
        reason = r.get("false_positive_reason", "")
        if reason:
            fp_counter[reason] += 1
    common_fp = fp_counter.most_common(10)

    low_quality = []
    for key, data in rate_by_source.items():
        if data["count"] >= 5 and data["rate"] < 0.3:
            low_quality.append(key)

    suggestions = _compute_suggestions(records, rate_by_priority, rate_by_topic)

    return {
        "total": total,
        "effective_count": effective_count,
        "ineffective_count": ineffective_count,
        "unknown_count": unknown_count,
        "effective_rate": effective_count / total if total else 0.0,
        "effective_rate_by_priority": rate_by_priority,
        "effective_rate_by_source": rate_by_source,
        "effective_rate_by_source_type": rate_by_source_type,
        "effective_rate_by_topic": rate_by_topic,
        "common_false_positive_reasons": common_fp,
        "low_quality_sources": low_quality,
        "suggestions": suggestions,
        "research_case_summary": _research_case_summary(),
        "decision_ledger_summary": _decision_ledger_summary(),
        "evidence_chain_summary": _evidence_chain_summary(),
        "source_remediation_summary": _source_remediation_summary(),
    }


def _empty_summary() -> Dict[str, Any]:
    return {
        "total": 0,
        "effective_count": 0,
        "ineffective_count": 0,
        "unknown_count": 0,
        "effective_rate": 0.0,
        "effective_rate_by_priority": {},
        "effective_rate_by_source": {},
        "effective_rate_by_source_type": {},
        "effective_rate_by_topic": {},
        "common_false_positive_reasons": [],
        "low_quality_sources": [],
        "suggestions": {"keyword_adjustments": [], "threshold_adjustments": []},
        "research_case_summary": _research_case_summary(),
        "decision_ledger_summary": _decision_ledger_summary(),
        "evidence_chain_summary": _evidence_chain_summary(),
        "source_remediation_summary": _source_remediation_summary(),
    }


def _research_case_summary() -> Dict[str, Any]:
    try:
        from .research_cases import compute_case_summary
        return compute_case_summary()
    except Exception:
        return {
            "total_cases": 0,
            "active_cases": 0,
            "status_counts": {},
            "status_changes": [],
            "cases": [],
        }


def _decision_ledger_summary() -> Dict[str, Any]:
    try:
        from .decisions import compute_decision_summary
        return compute_decision_summary()
    except Exception:
        return {
            "total": 0,
            "decision_counts": {},
            "decision_family_counts": {},
            "outcome_counts": {},
            "pending": [],
        }


def _evidence_chain_summary() -> Dict[str, Any]:
    try:
        from .evidence_chain import summarize_evidence_chains
        json_dir = project_path("reports/json")
        if not json_dir.exists():
            return summarize_evidence_chains([])
        files = sorted(json_dir.glob("*.json"))
        if not files:
            return summarize_evidence_chains([])
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        return summarize_evidence_chains(data.get("signals", []))
    except Exception:
        return {
            "signal_count": 0,
            "avg_score": 0.0,
            "gap_task_count": 0,
            "unresolved_ref_count": 0,
            "status_counts": {},
        }


def _source_remediation_summary() -> Dict[str, Any]:
    try:
        from .source_remediation import summarize_remediation_from_report
        remediation_latest = project_path("reports/remediation/latest.json")
        if remediation_latest.exists():
            remediation = json.loads(remediation_latest.read_text(encoding="utf-8-sig"))
            return summarize_remediation_from_report({"stats": remediation})
        json_dir = project_path("reports/json")
        if not json_dir.exists():
            return summarize_remediation_from_report({})
        files = sorted(json_dir.glob("*.json"))
        if not files:
            return summarize_remediation_from_report({})
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        return summarize_remediation_from_report(data)
    except Exception:
        return {
            "source_degraded_event_count": 0,
            "remediation_queue_count": 0,
            "open_count": 0,
            "resolved_count": 0,
            "status_counts": {},
            "gap_type_counts": {},
            "source_failure_counts": {},
        }


def _rate_by_key(records: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        val = r.get(key, "")
        if val:
            groups.setdefault(val, []).append(r)
    result: Dict[str, Dict[str, Any]] = {}
    for val, items in groups.items():
        eff = sum(1 for r in items if r.get("was_effective") == "true")
        result[val] = {"count": len(items), "effective": eff, "rate": eff / len(items) if items else 0.0}
    return result


def _rate_by_topics(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        for topic in r.get("topics", []):
            groups.setdefault(topic, []).append(r)
    result: Dict[str, Dict[str, Any]] = {}
    for topic, items in groups.items():
        eff = sum(1 for r in items if r.get("was_effective") == "true")
        result[topic] = {"count": len(items), "effective": eff, "rate": eff / len(items) if items else 0.0}
    return result


def _compute_suggestions(
    records: List[Dict[str, Any]],
    rate_by_priority: Dict[str, Dict[str, Any]],
    rate_by_topic: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    keyword_adjustments: List[str] = []
    threshold_adjustments: List[str] = []

    for topic, data in rate_by_topic.items():
        if data["count"] >= 3 and data["rate"] < 0.3:
            keyword_adjustments.append(
                "主题「{0}」有效率仅{1:.0%}({2}条)，建议检查关键词是否过于宽泛".format(
                    topic, data["rate"], data["count"]
                )
            )

    p1 = rate_by_priority.get("P1", {})
    if p1.get("count", 0) >= 5 and p1.get("rate", 1.0) < 0.5:
        threshold_adjustments.append(
            "P1 有效率仅{0:.0%}({1}条)，建议考虑将 min_priority_for_alert 从 P1 提升到 P0".format(
                p1["rate"], p1["count"]
            )
        )

    p2 = rate_by_priority.get("P2", {})
    if p2.get("count", 0) >= 5 and p2.get("rate", 0.0) > 0.8:
        threshold_adjustments.append(
            "P2 有效率高达{0:.0%}({1}条)，建议考虑将 min_priority_for_alert 从 P1 降低到 P2".format(
                p2["rate"], p2["count"]
            )
        )

    return {
        "keyword_adjustments": keyword_adjustments,
        "threshold_adjustments": threshold_adjustments,
    }


def write_review_summary(summary: Dict[str, Any]) -> Tuple[str, str]:
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    md_path = project_path("reports/review/review_summary_{0}.md".format(stamp))
    json_path = project_path("reports/review/review_summary_{0}.json".format(stamp))
    md_path.parent.mkdir(parents=True, exist_ok=True)

    md_path.write_text(_render_summary_markdown(summary), encoding="utf-8-sig")
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(md_path), str(json_path)


def _render_summary_markdown(summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# 复盘汇总报告")
    lines.append("")
    lines.append("- 生成时间: {0}".format(now_iso()))
    lines.append("- 总复盘数: {0}".format(summary["total"]))
    lines.append("- 有效信号: {0}".format(summary["effective_count"]))
    lines.append("- 无效信号: {0}".format(summary["ineffective_count"]))
    lines.append("- 未知: {0}".format(summary["unknown_count"]))
    lines.append("- 整体有效率: {0:.0%}".format(summary["effective_rate"]))
    lines.append("")

    lines.append("## 按优先级有效率")
    lines.append("")
    for prio, data in sorted(summary.get("effective_rate_by_priority", {}).items()):
        lines.append("- {0}: {1:.0%} ({2}/{3})".format(prio, data["rate"], data["effective"], data["count"]))
    if not summary.get("effective_rate_by_priority"):
        lines.append("暂无数据。")
    lines.append("")

    lines.append("## 按数据源有效率")
    lines.append("")
    for src, data in sorted(summary.get("effective_rate_by_source", {}).items()):
        lines.append("- {0}: {1:.0%} ({2}/{3})".format(src, data["rate"], data["effective"], data["count"]))
    if not summary.get("effective_rate_by_source"):
        lines.append("暂无数据。")
    lines.append("")

    lines.append("## 按主题有效率")
    lines.append("")
    for topic, data in sorted(summary.get("effective_rate_by_topic", {}).items()):
        lines.append("- {0}: {1:.0%} ({2}/{3})".format(topic, data["rate"], data["effective"], data["count"]))
    if not summary.get("effective_rate_by_topic"):
        lines.append("暂无数据。")
    lines.append("")

    lines.append("## 常见无效原因")
    lines.append("")
    fp = summary.get("common_false_positive_reasons", [])
    if fp:
        for reason, count in fp:
            lines.append("- {0} ({1}次)".format(reason, count))
    else:
        lines.append("暂无记录。")
    lines.append("")

    low = summary.get("low_quality_sources", [])
    if low:
        lines.append("## 低质量来源")
        lines.append("")
        for src in low:
            lines.append("- {0}（有效率低于30%，建议降低权重或移除）".format(src))
        lines.append("")

    suggestions = summary.get("suggestions", {})
    kw = suggestions.get("keyword_adjustments", [])
    th = suggestions.get("threshold_adjustments", [])
    if kw or th:
        lines.append("## 优化建议（仅供参考，不自动修改配置）")
        lines.append("")
        if kw:
            lines.append("### 关键词建议")
            lines.append("")
            for item in kw:
                lines.append("- {0}".format(item))
            lines.append("")
        if th:
            lines.append("### 阈值建议")
            lines.append("")
            for item in th:
                lines.append("- {0}".format(item))
            lines.append("")

    evidence_summary = summary.get("evidence_chain_summary", {}) or {}
    lines.append("## Evidence Chain Quality")
    lines.append("")
    lines.append("- signal_count: {0}".format(evidence_summary.get("signal_count", 0)))
    lines.append("- avg_score: {0:.0%}".format(float(evidence_summary.get("avg_score", 0) or 0)))
    lines.append("- gap_task_count: {0}".format(evidence_summary.get("gap_task_count", 0)))
    lines.append("- unresolved_ref_count: {0}".format(evidence_summary.get("unresolved_ref_count", 0)))
    status_counts = evidence_summary.get("status_counts", {}) or {}
    if status_counts:
        for status, count in sorted(status_counts.items()):
            lines.append("- {0}: {1}".format(status, count))
    lines.append("")

    remediation = summary.get("source_remediation_summary", {}) or {}
    lines.append("## Source Remediation")
    lines.append("")
    lines.append("- source_degraded_event_count: {0}".format(remediation.get("source_degraded_event_count", 0)))
    lines.append("- remediation_queue_count: {0}".format(remediation.get("remediation_queue_count", 0)))
    lines.append("- open_count: {0}".format(remediation.get("open_count", 0)))
    lines.append("- resolved_count: {0}".format(remediation.get("resolved_count", 0)))
    lines.append("- rejected_count: {0}".format(remediation.get("rejected_count", 0)))
    lines.append("- deferred_count: {0}".format(remediation.get("deferred_count", 0)))
    lines.append("- completion_rate: {0:.0%}".format(float(remediation.get("completion_rate", 0) or 0)))
    lines.append("- average_resolution_hours: {0}".format(remediation.get("average_resolution_hours", 0)))
    gap_counts = remediation.get("gap_type_counts", {}) or {}
    if gap_counts:
        lines.append("- gap_types: {0}".format(
            "; ".join("{0}={1}".format(k, v) for k, v in sorted(gap_counts.items()))
        ))
    common_task_types = remediation.get("common_task_types", {}) or {}
    if common_task_types:
        lines.append("- common_task_types: {0}".format(
            "; ".join("{0}={1}".format(k, v) for k, v in sorted(common_task_types.items()))
        ))
    source_failures = remediation.get("source_failure_counts", {}) or {}
    if source_failures:
        lines.append("- source_failures: {0}".format(
            "; ".join("{0}={1}".format(k, v) for k, v in sorted(source_failures.items()))
        ))
    lines.append("")

    decision_ledger = summary.get("decision_ledger_summary", {}) or {}
    lines.append("## Decision Ledger")
    lines.append("")
    lines.append("- total_decisions: {0}".format(decision_ledger.get("total", 0)))
    decision_counts = decision_ledger.get("decision_counts", {}) or {}
    if decision_counts:
        for decision, count in sorted(decision_counts.items()):
            lines.append("- {0}: {1}".format(decision, count))
    else:
        lines.append("- no manual decisions")
    outcome_counts = decision_ledger.get("outcome_counts", {}) or {}
    if outcome_counts:
        lines.append("")
        lines.append("### Outcomes")
        lines.append("")
        for outcome, count in sorted(outcome_counts.items()):
            lines.append("- {0}: {1}".format(outcome, count))
    lines.append("")

    case_summary = summary.get("research_case_summary", {}) or {}
    lines.append("## Research Case Status Changes")
    lines.append("")
    lines.append("- total_cases: {0}".format(case_summary.get("total_cases", 0)))
    status_counts = case_summary.get("status_counts", {})
    if status_counts:
        for status, count in sorted(status_counts.items()):
            lines.append("- {0}: {1}".format(status, count))
    else:
        lines.append("- no research cases")
    changes = case_summary.get("status_changes", [])
    if changes:
        lines.append("")
        lines.append("### Manual Case Decisions")
        lines.append("")
        for item in changes[:20]:
            lines.append("- {0}: {1} -> {2}".format(
                item.get("case_id", ""),
                item.get("decision", ""),
                item.get("status", ""),
            ))

    return "\n".join(lines)
