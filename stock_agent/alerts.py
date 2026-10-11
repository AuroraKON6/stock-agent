"""Notification generation for scheduled ops.

Reads latest JSON report, alerts, pending_review, and action logs,
checks trigger conditions, and writes a notification markdown file.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import project_path, project_path_override
from .review_queue import OPEN_STATUSES, load_latest_review_queue
from .production_pilot import load_latest_production_pilot


def _stamp() -> str:
    return (
        datetime.now()
        .isoformat()
        .replace(":", "")
        .replace("-", "")
        .replace("+", "_")
        .split(".")[0]
    )


def _latest_file(directory: Path, pattern: str) -> Optional[Path]:
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def _load_latest_json_report() -> Dict[str, Any]:
    d = project_path("reports/json")
    f = _latest_file(d, "*.json")
    if not f:
        return {"signals": [], "stats": {}, "source_health": {}, "run_ops": {}, "external_dependencies": {}}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        stats = data.get("stats", {})
        return {
            "signals": data.get("signals", []),
            "stats": stats,
            "source_health": data.get("source_health", stats.get("source_health", {})),
            "run_ops": stats.get("run_ops", {}),
            "external_dependencies": {
                "market_resilience": stats.get("market_resilience", {}),
                "market_data_governance": stats.get("market_data_governance", {}),
                "llm_connectivity": stats.get("llm_connectivity", {}),
            },
        }
    except Exception:
        return {"signals": [], "stats": {}, "source_health": {}, "run_ops": {}, "external_dependencies": {}}


def _load_latest_alerts() -> List[Dict[str, Any]]:
    d = project_path("reports/alerts")
    f = _latest_file(d, "*.jsonl")
    if not f:
        return []
    alerts = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            alerts.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return alerts


def _load_latest_pending_review(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    with project_path_override(project_path):
        queue = load_latest_review_queue(config or {})
    if queue:
        return [
            item for item in queue.get("items", [])
            if str(item.get("status") or "pending") in OPEN_STATUSES
        ]
    d = project_path("data/review")
    f = _latest_file(d, "pending_review_*.jsonl")
    if not f:
        return []
    records = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _load_latest_actions() -> List[Dict[str, Any]]:
    d = project_path("reports/actions")
    f = _latest_file(d, "actions_*.jsonl")
    if not f:
        return []
    records = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _load_research_runtime() -> Dict[str, Any]:
    path = project_path("reports/research_runtime/latest.json")
    if not path.exists():
        return {"summary": {}, "runtime": {"cases": []}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {"summary": {}, "runtime": {"cases": []}}
    except (OSError, json.JSONDecodeError):
        return {"summary": {}, "runtime": {"cases": []}}


def _check_triggers(
    signals: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
    pending_review: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    source_health: Optional[Dict[str, Any]] = None,
    run_ops: Optional[Dict[str, Any]] = None,
    external_dependencies: Optional[Dict[str, Any]] = None,
    research_runtime: Optional[Dict[str, Any]] = None,
    production_pilot: Optional[Dict[str, Any]] = None,
) -> List[Tuple[str, str]]:
    """Return list of (trigger_name, description) for triggered conditions."""
    triggers: List[Tuple[str, str]] = []

    # 1. P0/P1 alerts
    high_alerts = [a for a in alerts if a.get("priority") in ("P0", "P1")]
    if high_alerts:
        triggers.append((
            "high_priority_alerts",
            "P0/P1 预警 {0} 条".format(len(high_alerts)),
        ))

    # 2. High-confidence LLM with human_decision_needed
    llm_decisions = []
    for s in signals:
        llm = s.get("llm_analysis") or {}
        if (
            llm.get("confidence", 0) >= 0.75
            and llm.get("human_decision_needed")
        ):
            llm_decisions.append(s)
    if llm_decisions:
        triggers.append((
            "llm_human_decision",
            "高置信度 LLM 研判需人工决策 {0} 条".format(len(llm_decisions)),
        ))

    # 3. Source errors
    source_errors = [s for s in signals if s.get("source_type") == "source_error"]
    if source_errors:
        triggers.append((
            "source_errors",
            "数据源异常 {0} 条".format(len(source_errors)),
        ))

    # 4. Pending review
    if pending_review:
        triggers.append((
            "pending_review",
            "待复核信号 {0} 条".format(len(pending_review)),
        ))
        overdue = [item for item in pending_review if item.get("sla_status") == "overdue"]
        at_risk = [item for item in pending_review if item.get("sla_status") == "at_risk"]
        if overdue or at_risk:
            triggers.append((
                "review_sla",
                "人工复核 SLA: overdue={0}, at_risk={1}; 仅升级提醒，不自动分派".format(
                    len(overdue), len(at_risk)
                ),
            ))

    source_health = source_health or {}
    if source_health.get("overall_status") in ("WARN", "FAIL"):
        low_quality = source_health.get("low_quality_sources", []) or []
        triggers.append((
            "source_health",
            "Data source health {0}: {1}".format(
                source_health.get("overall_status"),
                ", ".join(str(item) for item in low_quality) if low_quality else "see source health report",
            ),
        ))

    run_ops = run_ops or {}
    if run_ops.get("overall_status") in ("WARN", "FAIL"):
        degradations = run_ops.get("degradations", []) or []
        triggers.append((
            "runtime_ops",
            "Runtime operations {0}: {1}".format(
                run_ops.get("overall_status"),
                ", ".join(str(item.get("reason", "")) for item in degradations[:3]) if degradations else "see run ops summary",
            ),
        ))

    external_dependencies = external_dependencies or {}
    market = external_dependencies.get("market_resilience", {}) or {}
    market_governance = external_dependencies.get("market_data_governance", {}) or {}
    if market_governance.get("status") in ("WARN", "FAIL"):
        triggers.append((
            "market_data_governance",
            "Market data governance {0}: blocked={1}, degraded={2}, llm_eligible={3}".format(
                market_governance.get("status"),
                market_governance.get("blocked_count", 0),
                market_governance.get("degraded_count", 0),
                market_governance.get("llm_eligible_count", 0),
            ),
        ))
    llm = external_dependencies.get("llm_connectivity", {}) or {}
    if market.get("status") in ("WARN", "FAIL") or llm.get("status") in ("WARN", "FAIL"):
        parts = []
        if market.get("status") in ("WARN", "FAIL"):
            parts.append("market={0}".format(market.get("status")))
        if llm.get("status") in ("WARN", "FAIL"):
            parts.append("llm={0}:{1}".format(llm.get("status"), llm.get("diagnosis", "")))
        triggers.append(("external_dependencies", "External dependency degradation: {0}".format(", ".join(parts))))

    research_runtime = research_runtime or {}
    runtime = research_runtime.get("runtime", research_runtime) if isinstance(research_runtime, dict) else {}
    runtime_cases = runtime.get("cases", []) if isinstance(runtime, dict) else []
    human_gate_cases = [
        item for item in runtime_cases
        if item.get("stage") in {"quality_review", "compliance_review", "human_approval"}
    ]
    if human_gate_cases:
        triggers.append((
            "research_runtime_human_gate",
            "机构投研门禁待人工处理 {0} 个案件".format(len(human_gate_cases)),
        ))
    compliance_blocks = [
        item for item in runtime_cases
        if str(((item.get("approvals", {}) or {}).get("compliance_review", {}) or {}).get("status")) == "blocked"
    ]
    if compliance_blocks:
        triggers.append((
            "research_runtime_compliance_block",
            "合规阻断 {0} 个案件，禁止发布".format(len(compliance_blocks)),
        ))

    production_pilot = production_pilot or {}
    if production_pilot:
        technical = str(production_pilot.get("technical_readiness") or "UNKNOWN")
        decision = str(production_pilot.get("go_live_decision") or "NO_GO")
        if technical != "READY":
            triggers.append((
                "production_pilot_technical_block",
                "受控生产试运行技术门禁 {0}，有效模式保持 shadow".format(technical),
            ))
        if decision != "GO":
            triggers.append((
                "production_pilot_no_go",
                "生产决策 NO_GO：{0} 个门禁待真实人工或证据处理".format(
                    len(production_pilot.get("go_live_blockers", []) or [])
                ),
            ))
        incidents = production_pilot.get("incidents", {}) or {}
        if incidents.get("open_critical_count") or incidents.get("open_high_count"):
            triggers.append((
                "production_pilot_incident",
                "生产试运行事件升级：critical={0}, high={1}".format(
                    incidents.get("open_critical_count", 0), incidents.get("open_high_count", 0)
                ),
            ))

    return triggers


def generate_notifications(config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Generate notification markdown from latest data.

    Returns the path to latest.md, or None if no triggers fired.
    """
    data = _load_latest_json_report()
    signals = data["signals"]
    source_health = data.get("source_health", {})
    run_ops = data.get("run_ops", {})
    external_dependencies = data.get("external_dependencies", {})
    alerts = _load_latest_alerts()
    pending_review = _load_latest_pending_review(config)
    actions = _load_latest_actions()
    research_runtime = _load_research_runtime()
    pilot_enabled = bool((config or {}).get("production_pilot", {}).get("enabled", False)) if isinstance(config, dict) else False
    production_pilot = load_latest_production_pilot(config or {}) if pilot_enabled else {}

    triggers = _check_triggers(
        signals,
        alerts,
        pending_review,
        actions,
        source_health=source_health,
        run_ops=run_ops,
        external_dependencies=external_dependencies,
        research_runtime=research_runtime,
        production_pilot=production_pilot,
    )

    now = datetime.now()
    stamp = _stamp()
    notify_dir = project_path("reports/notifications")
    notify_dir.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append("# A股投研系统通知")
    lines.append("")
    lines.append("生成时间: {0}".format(now.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("")

    if not triggers:
        lines.append("## 状态: 正常")
        lines.append("")
        lines.append("本轮运行无异常触发条件。")
        lines.append("- 信号总数: {0}".format(len(signals)))
        lines.append("- 预警总数: {0}".format(len(alerts)))
        lines.append("- 待复核: {0}".format(len(pending_review)))
    else:
        lines.append("## 触发条件 ({0} 项)".format(len(triggers)))
        lines.append("")
        for name, desc in triggers:
            lines.append("- **{0}**: {1}".format(name, desc))
        lines.append("")

        # Detail sections
        high_alerts = [a for a in alerts if a.get("priority") in ("P0", "P1")]
        if high_alerts:
            lines.append("## P0/P1 预警详情")
            lines.append("")
            for a in high_alerts:
                lines.append(
                    "- [{0}] {1} — {2}".format(
                        a.get("priority", "?"),
                        a.get("title", ""),
                        a.get("source", ""),
                    )
                )
            lines.append("")

        llm_decisions = [
            s for s in signals
            if (s.get("llm_analysis") or {}).get("confidence", 0) >= 0.75
            and (s.get("llm_analysis") or {}).get("human_decision_needed")
        ]
        if llm_decisions:
            lines.append("## 高置信度 LLM 研判需人工决策")
            lines.append("")
            for s in llm_decisions:
                llm = s.get("llm_analysis") or {}
                lines.append(
                    "- [{0}] {1} — 置信度 {2:.0%}, 方向: {3}".format(
                        s.get("priority", "?"),
                        s.get("title", ""),
                        llm.get("confidence", 0),
                        llm.get("impact_direction", "unknown"),
                    )
                )
            lines.append("")

        source_errors = [s for s in signals if s.get("source_type") == "source_error"]
        if source_errors:
            lines.append("## 数据源异常")
            lines.append("")
            for s in source_errors:
                lines.append("- {0}".format(s.get("title", "")))
            lines.append("")

        if pending_review:
            lines.append("## 待复核信号")
            lines.append("")
            for r in pending_review[:10]:
                lines.append(
                    "- [{0}] {1}".format(
                        r.get("priority", "?"),
                        r.get("title", ""),
                    )
                )
            if len(pending_review) > 10:
                lines.append("- ... 等共 {0} 条".format(len(pending_review)))
            lines.append("")

        runtime_cases = ((research_runtime.get("runtime", {}) or {}).get("cases", [])
                         if isinstance(research_runtime, dict) else [])
        runtime_attention = [
            item for item in runtime_cases
            if item.get("stage") in {"quality_review", "compliance_review", "human_approval"}
            or str(((item.get("approvals", {}) or {}).get("compliance_review", {}) or {}).get("status")) == "blocked"
        ]
        if runtime_attention:
            lines.append("## 机构投研门禁待办")
            lines.append("")
            for item in runtime_attention[:10]:
                lines.append("- [{0}] {1} / {2} / {3}".format(
                    item.get("top_priority", "?"),
                    item.get("case_id", ""),
                    item.get("stage", ""),
                    item.get("title", ""),
                ))
            lines.append("")

    lines.append("## 统计")
    lines.append("")
    lines.append("- 信号总数: {0}".format(len(signals)))
    lines.append("- 预警总数: {0}".format(len(alerts)))
    lines.append("- 待复核: {0}".format(len(pending_review)))
    lines.append("- 动作建议: {0}".format(len(actions)))
    runtime_summary = research_runtime.get("summary", {}) if isinstance(research_runtime, dict) else {}
    lines.append("- 投研案件: {0}, 待人工门禁: {1}".format(
        runtime_summary.get("case_count", 0),
        runtime_summary.get("pending_human_count", 0),
    ))
    lines.append("")

    if source_health.get("overall_status") in ("WARN", "FAIL"):
        lines.append("## Data Source Health")
        lines.append("")
        lines.append("- overall_status: {0}".format(source_health.get("overall_status")))
        lines.append("- overall_score: {0}".format(source_health.get("overall_score", 0)))
        low_quality = source_health.get("low_quality_sources", []) or []
        if low_quality:
            lines.append("- low_quality_sources: {0}".format(", ".join(str(item) for item in low_quality)))
        for item in source_health.get("sources", [])[:10]:
            if item.get("status") in ("WARN", "FAIL"):
                lines.append("- [{0}] {1}: reasons={2}, gaps={3}".format(
                    item.get("status"),
                    item.get("source_id"),
                    ",".join(item.get("anomaly_reasons", []) or []) or "-",
                    ",".join(item.get("coverage_gap_symbols", []) or []) or "-",
                ))
        lines.append("")

    if run_ops.get("overall_status") in ("WARN", "FAIL"):
        lines.append("## Runtime Operations")
        lines.append("")
        lines.append("- overall_status: {0}".format(run_ops.get("overall_status")))
        lines.append("- elapsed_ms: {0}".format(run_ops.get("elapsed_ms", 0)))
        for item in (run_ops.get("degradations", []) or [])[:10]:
            lines.append("- [{0}] {1}: {2} {3}".format(
                item.get("severity", ""),
                item.get("component", ""),
                item.get("reason", ""),
                item.get("detail", ""),
            ).strip())
        lines.append("")

    market = (external_dependencies.get("market_resilience", {}) or {}) if isinstance(external_dependencies, dict) else {}
    llm = (external_dependencies.get("llm_connectivity", {}) or {}) if isinstance(external_dependencies, dict) else {}
    if market.get("status") in ("WARN", "FAIL") or llm.get("status") in ("WARN", "FAIL"):
        lines.append("## External Dependencies")
        lines.append("")
        if market:
            lines.append("- market_resilience: status={0}, fallback_used={1}, cache_used={2}".format(
                market.get("status"),
                market.get("fallback_used", False),
                market.get("cache_used", False),
            ))
            if market.get("missing_symbols"):
                lines.append("- missing_symbols: {0}".format(",".join(str(item) for item in market.get("missing_symbols", []))))
        if llm:
            lines.append("- llm_connectivity: status={0}, diagnosis={1}".format(llm.get("status"), llm.get("diagnosis", "")))
            for item in (llm.get("repair_guide", []) or [])[:5]:
                lines.append("- repair: {0}".format(item))
        lines.append("")

    content = "\n".join(lines)

    # Write latest.md (overwrite)
    latest_path = notify_dir / "latest.md"
    latest_path.write_text(content, encoding="utf-8")

    # Write timestamped copy
    ts_path = notify_dir / "notification_{0}.md".format(stamp)
    ts_path.write_text(content, encoding="utf-8")

    return str(latest_path)
