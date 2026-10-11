from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .analysis import min_priority_passes
from .audit_log import atomic_write_text
from .config import project_path
from .models import AnalyzedSignal, now_iso


def write_outputs(
    signals: List[AnalyzedSignal],
    config: Dict[str, Any],
    stats: Dict[str, Any],
) -> Tuple[str, str, str, List[AnalyzedSignal]]:
    generated_at = now_iso()
    report_config = config.get("report", {})
    top_n = int(report_config.get("daily_top_n", 20))
    include_low_priority = bool(report_config.get("include_low_priority", False))
    min_alert_priority = config.get("priority_rules", {}).get("min_priority_for_alert", "P1")

    sorted_signals = sorted(signals, key=lambda item: (-item.priority_score, item.published_at), reverse=False)
    price_signals = [item for item in sorted_signals if item.source_type == "price"]
    filing_signals = [item for item in sorted_signals if isinstance((item.payload or {}).get("filing_evidence"), dict)]
    llm_scope_signals = [item for item in sorted_signals if item.llm_analysis or item.llm_error or item.llm_actions]
    role_scope_signals = [item for item in sorted_signals if item.role_analyses or item.role_synthesis or item.role_analysis_error]
    if not include_low_priority:
        report_signals = [item for item in sorted_signals if item.priority != "P3"]
    else:
        report_signals = sorted_signals
    report_signals = report_signals[:top_n]
    alerts = [item for item in signals if min_priority_passes(item.priority, min_alert_priority)]

    stamp = generated_at.replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    daily_dir = project_path(str(report_config.get("daily_dir") or "reports/daily"))
    json_dir = project_path(str(report_config.get("json_dir") or "reports/json"))
    alert_dir = project_path(str(report_config.get("alert_dir") or "reports/alerts"))
    for folder in (daily_dir, json_dir, alert_dir):
        folder.mkdir(parents=True, exist_ok=True)
    # run and agent-run hold different lease names by design and may legally
    # finish within the same second. Existence checks cannot decide ownership
    # under concurrency (check-then-write race), so the JSON artifact is
    # claimed EXCLUSIVELY with O_CREAT|O_EXCL: whoever wins the claim owns
    # this stamp and every other writer moves to the next suffix. The claim is
    # then replaced by the atomic write, so readers never observe partial
    # content.
    base_stamp = stamp
    suffix = 0
    while True:
        if suffix:
            stamp = "{0}-{1}".format(base_stamp, suffix)
        report_path = daily_dir / "{0}.md".format(stamp)
        json_path = json_dir / "{0}.json".format(stamp)
        alert_path = alert_dir / "{0}.jsonl".format(stamp)
        try:
            claim_fd = os.open(str(json_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(claim_fd)
            break
        except FileExistsError:
            suffix += 1

    atomic_write_text(
        report_path,
        _render_markdown(config, generated_at, report_signals, price_signals, alerts, stats, filing_signals, llm_scope_signals, role_scope_signals),
        encoding="utf-8-sig",
    )
    atomic_write_text(
        json_path,
        json.dumps(
            {
                "generated_at": generated_at,
                "stats": stats,
                "team": stats.get("team", {}),
                "team_ops": stats.get("team_ops", {}),
                "research_cases": stats.get("research_cases", []),
                "research_runtime": stats.get("research_runtime", {}),
                "research_runtime_cases": stats.get("research_runtime_cases", []),
                "research_workpapers": stats.get("research_workpapers", {}),
                "research_workpaper_items": stats.get("research_workpaper_items", []),
                "official_evidence_vault": stats.get("official_evidence_vault", {}),
                "official_evidence_vault_items": stats.get("official_evidence_vault_items", []),
                "official_evidence_vault_verification": stats.get("official_evidence_vault_verification", {}),
                "source_health": stats.get("source_health", {}),
                "signals": [signal.to_dict() for signal in signals],
                "alerts": [signal.to_dict() for signal in alerts],
                "review_template": _review_template(config, signals),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    )
    atomic_write_text(
        alert_path,
        "\n".join(json.dumps(item.to_dict(), ensure_ascii=False, default=str) for item in alerts) + ("\n" if alerts else ""),
    )
    return str(report_path), str(json_path), str(alert_path), alerts


def _render_markdown(
    config: Dict[str, Any],
    generated_at: str,
    signals: List[AnalyzedSignal],
    price_signals: List[AnalyzedSignal],
    alerts: List[AnalyzedSignal],
    stats: Dict[str, Any],
    filing_signals: Optional[List[AnalyzedSignal]] = None,
    llm_scope_signals: Optional[List[AnalyzedSignal]] = None,
    role_scope_signals: Optional[List[AnalyzedSignal]] = None,
) -> str:
    lines: List[str] = []
    lines.append("# {0} 日报".format(config.get("project_name", "A股智能投研系统")))
    lines.append("")
    lines.append("- 生成时间: {0}".format(generated_at))
    lines.append("- 新增信号: {0}".format(stats.get("inserted", 0)))
    lines.append("- 重复信号: {0}".format(stats.get("duplicates", 0)))
    lines.append("- 预警数量: {0}".format(len(alerts)))
    lines.append("")

    lines.append("## 一句话结论")
    lines.append("")
    lines.append(_one_line_takeaway(signals, alerts))
    lines.append("")

    lines.append("## 研判框架")
    lines.append("")
    lines.extend(_render_research_framework(stats))
    lines.append("")

    lines.append("## Research Cases")
    lines.append("")
    lines.extend(_render_research_cases(stats))
    lines.append("")

    lines.append("## Institutional Research Runtime")
    lines.append("")
    lines.extend(_render_research_runtime(stats))
    lines.append("")

    lines.append("## Official Evidence Vault")
    lines.append("")
    lines.extend(_render_official_evidence_vault(stats))
    lines.append("")

    lines.append("## Research Workpapers")
    lines.append("")
    lines.extend(_render_research_workpapers(stats))
    lines.append("")

    lines.append("## Research-as-Git")
    lines.append("")
    lines.extend(_render_research_git(stats))
    lines.append("")

    lines.append("## Workspace Inbox")
    lines.append("")
    lines.extend(_render_workspace_inbox(stats))
    lines.append("")

    lines.append("## Data Source Health")
    lines.append("")
    lines.extend(_render_source_health(stats))
    lines.append("")
    lines.append("## Runtime Operations")
    lines.append("")
    lines.extend(_render_run_ops(stats))
    lines.append("")
    lines.append("## Model Operations And Quality")
    lines.append("")
    lines.extend(_render_model_operations(stats))
    lines.append("")
    lines.append("## External Dependencies")
    lines.append("")
    lines.extend(_render_external_dependencies(stats))
    lines.append("")

    lines.append("## Human Review Queue")
    lines.append("")
    lines.extend(_render_review_queue(stats))
    lines.append("")

    lines.append("## 投研团队")
    lines.append("")
    lines.extend(_render_research_team(stats))
    lines.append("")

    lines.append("## 人工决策")
    lines.append("")
    lines.extend(_render_decision_summary(stats))
    lines.append("")

    lines.append("## 优先处理")
    lines.append("")
    if not signals:
        lines.append("暂无需要处理的新信号。")
    else:
        for signal in signals:
            lines.extend(_render_signal(signal))
    lines.append("")

    lines.append("## 主题分布")
    lines.append("")
    topic_counts = Counter(topic for signal in signals for topic in signal.topics)
    if topic_counts:
        for topic, count in topic_counts.most_common():
            lines.append("- {0}: {1}".format(topic, count))
    else:
        lines.append("暂无主题命中。")
    lines.append("")

    lines.append("## 行情快照")
    lines.append("")
    if not price_signals:
        lines.append("暂无新增行情快照。")
    else:
        for signal in price_signals:
            anomaly_flags = [r for r in signal.reasons if "异常" in r or "显著" in r]
            gate = (signal.payload or {}).get("market_data_governance", {}) or {}
            gate_text = "gate={0}, session={1}, llm_eligible={2}".format(
                gate.get("status", "unknown"), gate.get("session", "unknown"), gate.get("llm_eligible", False)
            )
            reasons = gate.get("reason_codes", []) or []
            if reasons:
                gate_text += ", reasons={0}".format(",".join(str(item) for item in reasons))
            if anomaly_flags:
                lines.append("- {0} [{1}]: {2}; {3}".format(signal.title, signal.priority, "；".join(anomaly_flags), gate_text))
            else:
                lines.append("- {0}: {1}; {2}".format(signal.title, signal.summary, gate_text))
    lines.append("")

    lines.append("## 公告/财报证据")
    lines.append("")
    lines.extend(_render_filing_evidence(filing_signals if filing_signals is not None else signals, stats))
    lines.append("")

    lines.append("## 人工复核")
    lines.append("")
    review_items = [signal for signal in signals if signal.needs_review]
    if not review_items:
        lines.append("暂无强制人工复核项。")
    else:
        for signal in review_items:
            lines.append("- [{0}] {1}: {2}".format(signal.priority, signal.title, "；".join(signal.reasons)))
    lines.append("")

    lines.append("## 复盘字段")
    lines.append("")
    for field in config.get("review", {}).get("fields", []):
        lines.append("- {0}".format(field))
    lines.append("")

    # Review feedback loop section
    review_dir = project_path("data/review")
    pending_files = sorted(review_dir.glob("pending_review_*.jsonl")) if review_dir.exists() else []
    review_summary_dir = project_path("reports/review")
    summary_files = sorted(review_summary_dir.glob("review_summary_*.md")) if review_summary_dir.exists() else []
    if pending_files or summary_files:
        lines.append("## 复盘反馈")
        lines.append("")
        if pending_files:
            lines.append("- 待复核文件: {0}".format(pending_files[-1]))
        if summary_files:
            lines.append("- 最新复盘汇总: {0}".format(summary_files[-1]))
        lines.append("")

    # LLM governance and Agent sections
    llm_scope = llm_scope_signals if llm_scope_signals is not None else signals
    llm_signals = [s for s in llm_scope if s.llm_analysis]
    llm_errors = [s for s in llm_scope if s.llm_error]

    lines.append("## LLM 治理与证据安全")
    lines.append("")
    governance_summary = stats.get("llm_governance_summary", {}) or {}
    if not config.get("llm_governance", {}).get("enabled", False):
        lines.append("- LLM 治理未启用。")
    else:
        lines.append("- 状态: {0}".format(stats.get("llm_governance_status", "UNKNOWN")))
        lines.append("- 外发边界: 仅 public 数据；例外只能由人工合规或信息安全角色处理")
        lines.append("- 准入 / 拒绝: {0} / {1}".format(
            governance_summary.get("approved_count", 0), governance_summary.get("denied_count", 0)
        ))
        lines.append("- 输出证据阻断: {0}".format(governance_summary.get("output_blocked_count", 0)))
        lines.append("- 敏感信息 / 提示注入阻断: {0} / {1}".format(
            governance_summary.get("sensitive_blocked_count", 0),
            governance_summary.get("prompt_injection_blocked_count", 0),
        ))
        lines.append("- 待人工例外复核: {0}".format(governance_summary.get("pending_exception_count", 0)))
        if stats.get("llm_governance_latest_path"):
            lines.append("- 治理审计报告: {0}".format(stats.get("llm_governance_latest_path")))
        for signal in llm_scope:
            for component, decision in (signal.llm_governance or {}).items():
                if not isinstance(decision, dict):
                    continue
                if decision.get("decision") == "denied" or str(((decision.get("postflight", {}) or {}).get("status") or "")) == "blocked":
                    lines.append("- {0} / {1}: classification={2}, egress={3}, postflight={4}".format(
                        signal.signal_id,
                        component,
                        decision.get("classification", ""),
                        decision.get("decision", ""),
                        ((decision.get("postflight", {}) or {}).get("status", "not_called")),
                    ))
    lines.append("")

    lines.append("## LLM Agent 研判")
    lines.append("")
    llm_model = stats.get("llm_model", "")
    if stats.get("llm_runtime"):
        lines.append("- Agent 基座: {0}".format(stats.get("llm_runtime")))
        lines.append("")
    if llm_model:
        lines.append("- 使用模型: {0}".format(llm_model))
        lines.append("")
    if stats.get("llm_provider_used"):
        lines.append("- provider: {0}".format(stats.get("llm_provider_used")))
        lines.append("")
    if not llm_signals:
        if stats.get("llm_enabled"):
            skipped = stats.get("llm_skipped_no_key", 0)
            if skipped:
                lines.append("本轮跳过 {0} 条信号：缺少 LLM_API_KEY/DEEPSEEK_API_KEY。".format(skipped))
            else:
                lines.append("本轮无 LLM 研判结果。")
        else:
            lines.append("LLM Agent 未启用。")
    else:
        for signal in llm_signals:
            a = signal.llm_analysis
            lines.append("### [{0}] {1}".format(signal.priority, signal.title))
            lines.append("")
            lines.append("- 影响方向: {0}".format(a.get("impact_direction", "unknown")))
            lines.append("- 置信度: {0:.0%}".format(a.get("confidence", 0)))
            lines.append("- 时间窗口: {0}".format(a.get("horizon", "unknown")))
            lines.append("- 核心判断: {0}".format(a.get("key_thesis", "")))
            lines.append("- 证据校验: {0}".format(a.get("grounding_status", "legacy_unchecked")))
            if a.get("valid_evidence_refs"):
                lines.append("- 有效证据引用: {0}".format("；".join(a.get("valid_evidence_refs", []))))
            risk_points = a.get("risk_points", [])
            if risk_points:
                lines.append("- 风险点: {0}".format("；".join(risk_points)))
            review_qs = a.get("review_questions", [])
            if review_qs:
                lines.append("- 人工复核问题:")
                for q in review_qs:
                    lines.append("  - {0}".format(q))
            lines.append("")
    lines.append("")

    lines.append("## LLM 建议动作")
    lines.append("")
    action_signals = [s for s in llm_scope if s.llm_actions]
    if not action_signals:
        lines.append("暂无 LLM 建议动作。")
    else:
        for signal in action_signals:
            lines.append("- [{0}] {1}: {2}".format(signal.priority, signal.title, ", ".join(signal.llm_actions)))
    lines.append("")

    lines.append("## LLM 失败/降级说明")
    lines.append("")
    if not llm_errors:
        if stats.get("llm_enabled"):
            lines.append("本轮 LLM 调用正常，无降级。")
        else:
            lines.append("LLM Agent 未启用，无降级记录。")
    else:
        for signal in llm_errors:
            lines.append("- [{0}] {1}: {2}".format(signal.priority, signal.title, signal.llm_error))
    lines.append("")

    lines.append("## Multi-Role LLM Research Desk")
    lines.append("")
    role_scope = role_scope_signals if role_scope_signals is not None else signals
    lines.extend(_render_multi_role_llm(role_scope, stats))
    lines.append("")

    return "\n".join(lines)


def _render_signal(signal: AnalyzedSignal) -> List[str]:
    lines = []
    lines.append("### [{0}] {1}".format(signal.priority, signal.title))
    lines.append("")
    lines.append("- 来源: {0} / {1}".format(signal.source, signal.source_type))
    lines.append("- 发布时间: {0}".format(signal.published_at))
    lines.append("- 主题: {0}".format("、".join(signal.topics) if signal.topics else "未命中"))
    lines.append("- 标的: {0}".format("、".join(signal.symbols) if signal.symbols else "未识别"))
    lines.append("- 情绪分: {0:.2f}".format(signal.sentiment_score))
    lines.append("- 优先级分: {0:.1f}".format(signal.priority_score))
    if signal.summary:
        lines.append("- 摘要: {0}".format(signal.summary))
    if signal.reasons:
        lines.append("- 规则命中: {0}".format("；".join(signal.reasons)))
    if signal.research_tracks:
        track_labels = [str(track.get("label") or track.get("id")) for track in signal.research_tracks]
        lines.append("- 研判路线: {0}".format(" / ".join(track_labels)))
    if signal.research_questions:
        lines.append("- 研判问题: {0}".format("；".join(signal.research_questions[:3])))
    if signal.team_assignments:
        assignments = [
            "{0}: {1}".format(item.get("role_name", item.get("role_id", "")), item.get("status", "queued"))
            for item in signal.team_assignments
        ]
        lines.append("- 团队分工: {0}".format("；".join(assignments)))
    if signal.url:
        lines.append("- 链接: {0}".format(signal.url))
    lines.append("")
    return lines


def _render_research_framework(stats: Dict[str, Any]) -> List[str]:
    if not stats.get("research_framework_enabled", False):
        return ["研判框架未启用。"]
    lines = [
        "- 已启用 playbook 数量: {0}".format(stats.get("research_playbooks", 0)),
        "- 已分流信号数量: {0}".format(stats.get("research_signal_count", 0)),
    ]
    track_counts = stats.get("research_track_counts", {})
    if track_counts:
        lines.append("- 路线分布: {0}".format(
            "；".join("{0}={1}".format(k, v) for k, v in sorted(track_counts.items()))
        ))
    agenda = stats.get("research_agenda", [])
    if agenda:
        lines.append("- 今日研判议程:")
        for item in agenda[:5]:
            lines.append("  - {0}".format(item))
    else:
        lines.append("- 今日暂无需要分流的研判议程。")
    return lines


def _render_research_cases(stats: Dict[str, Any]) -> List[str]:
    cases = stats.get("research_cases", []) or []
    if not cases:
        return ["No active research cases in this run."]
    lines = [
        "- case_count: {0}".format(stats.get("research_case_count", len(cases))),
        "- active_count: {0}".format(stats.get("research_case_active_count", 0)),
    ]
    status_counts = stats.get("research_case_status_counts", {})
    if status_counts:
        lines.append("- status_counts: {0}".format(
            "; ".join("{0}={1}".format(k, v) for k, v in sorted(status_counts.items()))
        ))
    if stats.get("research_case_summary_path"):
        lines.append("- case_summary: {0}".format(stats.get("research_case_summary_path")))
    for case in cases[:10]:
        lines.append("- [{0}] {1} | {2} | signals={3} | top={4}".format(
            case.get("status", "active"),
            case.get("case_id", "")[:12],
            case.get("title", ""),
            case.get("signal_count", 0),
            case.get("top_priority", ""),
        ))
        analysis = case.get("case_analysis", {}) or {}
        hypotheses = analysis.get("hypotheses", []) if isinstance(analysis, dict) else []
        if hypotheses:
            hypothesis = hypotheses[0]
            lines.append("  - hypothesis: {0} ({1:.0%}) {2}".format(
                hypothesis.get("name", "case"),
                float(hypothesis.get("probability", 0)),
                hypothesis.get("thesis", ""),
            ))
        gaps = analysis.get("evidence_gaps", []) if isinstance(analysis, dict) else []
        if gaps:
            lines.append("  - evidence_gaps: {0}".format("; ".join(str(item) for item in gaps[:3])))
        questions = analysis.get("counter_questions", []) if isinstance(analysis, dict) else []
        if questions:
            lines.append("  - counter_questions: {0}".format("; ".join(str(item) for item in questions[:3])))
        decision = case.get("case_decision", {}) or {}
        if decision:
            lines.append("  - manual_decision: {0} ({1})".format(
                decision.get("decision", ""),
                decision.get("notes", ""),
            ))
    return lines


def _render_research_runtime(stats: Dict[str, Any]) -> List[str]:
    if not stats.get("research_runtime_enabled", False):
        return ["机构级投研案件运行时未启用。"]
    summary = stats.get("research_runtime", {}) or {}
    lines = [
        "- cases: {0}".format(summary.get("case_count", 0)),
        "- blocked: {0}".format(summary.get("blocked_count", 0)),
        "- pending_human_gates: {0}".format(summary.get("pending_human_count", 0)),
        "- monitoring: {0}".format(summary.get("monitoring_count", 0)),
        "- boundary: internal research only; no automatic publication or trading",
    ]
    stage_counts = summary.get("stage_counts", {}) or {}
    if stage_counts:
        lines.append("- stages: {0}".format(
            "; ".join("{0}={1}".format(key, value) for key, value in stage_counts.items())
        ))
    queue = summary.get("next_action_queue", []) or []
    if queue:
        lines.append("- next_actions:")
        for item in queue[:10]:
            actions = "；".join(str(value) for value in item.get("next_actions", [])[:3])
            lines.append("  - [{0}] {1} / {2}: {3}".format(
                item.get("priority", ""),
                item.get("case_id", ""),
                item.get("stage", ""),
                actions or "继续监控",
            ))
    if stats.get("research_runtime_latest_path"):
        lines.append("- operations_report: {0}".format(stats.get("research_runtime_latest_path")))
    return lines


def _render_official_evidence_vault(stats: Dict[str, Any]) -> List[str]:
    if not stats.get("official_evidence_vault_enabled", False):
        return ["官方原件证据库未启用。"]
    summary = stats.get("official_evidence_vault", {}) or {}
    verification = stats.get("official_evidence_vault_verification", {}) or {}
    lines = [
        "- technical / business: {0} / {1}".format(
            verification.get("technical_status", "UNKNOWN"),
            verification.get("business_status", "BLOCKED"),
        ),
        "- event_chain_integrity: {0}".format(verification.get("integrity_status", "UNKNOWN")),
        "- documents / captured / quarantined: {0} / {1} / {2}".format(
            summary.get("document_count", 0),
            summary.get("captured_count", 0),
            summary.get("quarantined_count", 0),
        ),
        "- production_archive_ready: {0}".format(
            "yes" if verification.get("production_archive_ready") else "no"
        ),
        "- raw_document_sent_to_external_llm: false",
    ]
    if stats.get("evidence_vault_latest_path"):
        lines.append("- evidence_vault_report: {0}".format(stats.get("evidence_vault_latest_path")))
    blockers = verification.get("business_blockers", []) or []
    if blockers:
        lines.append("- business_blockers:")
        lines.extend("  - {0}".format(item) for item in blockers[:8])
    items = stats.get("official_evidence_vault_items", []) or []
    for item in items[:10]:
        lines.append("- [{0}] {1} | {2} | {3} | signals={4}".format(
            item.get("status", "unknown"),
            item.get("document_id", ""),
            item.get("source_domain", ""),
            item.get("published_at", ""),
            len(item.get("signal_ids") or []),
        ))
    return lines


def _render_research_workpapers(stats: Dict[str, Any]) -> List[str]:
    if not stats.get("research_workpapers_enabled", False):
        return ["- Structured research workpapers are disabled."]
    summary = stats.get("research_workpapers", {}) or {}
    lines = [
        "- hash_chain: {0}; immutable_events={1}".format(
            stats.get("research_workpaper_integrity_status", "UNKNOWN"),
            summary.get("event_count", 0),
        ),
        "- workpapers: {0}; created={1}; unchanged={2}; preserved={3}".format(
            summary.get("workpaper_count", 0),
            stats.get("research_workpaper_created_count", 0),
            stats.get("research_workpaper_unchanged_count", 0),
            stats.get("research_workpaper_preserved_count", 0),
        ),
        "- approval_eligible: {0}; pending_human={1}; business_status={2}".format(
            summary.get("approval_eligible_count", 0),
            summary.get("pending_human_count", 0),
            summary.get("business_status", "BLOCKED"),
        ),
        "- production_archive: {0} (local hash-chain files are not treated as an approved archive)".format(
            "approved" if summary.get("production_archive_ready") else "not approved"
        ),
    ]
    if stats.get("research_workpaper_latest_path"):
        lines.append("- workpaper_report: {0}".format(stats.get("research_workpaper_latest_path")))
    items = stats.get("research_workpaper_items", []) or []
    for item in items[:10]:
        lines.append("- [{0}] {1} v{2} | stage={3} | validation={4} | sources={5} | claims={6}".format(
            item.get("priority", ""),
            item.get("case_id", ""),
            item.get("version", 0),
            item.get("workflow_stage", ""),
            item.get("validation_status", "UNKNOWN"),
            item.get("source_count", 0),
            item.get("claim_count", 0),
        ))
        if item.get("blockers"):
            lines.append("  - blockers: {0}".format("; ".join(item.get("blockers", [])[:3])))
    return lines


def _render_research_git(stats: Dict[str, Any]) -> List[str]:
    if not stats.get("research_git_enabled", False):
        return ["Research-as-Git not enabled for this run."]
    lines = [
        "- auto_staged: {0}".format(stats.get("research_git_auto_staged_count", 0)),
        "- draft_count: {0}".format(stats.get("research_git_draft_count", 0)),
        "- pending_review: {0}".format(stats.get("research_git_pending_review_count", 0)),
    ]
    status_counts = stats.get("research_git_status_counts", {}) or {}
    if status_counts:
        lines.append("- status_counts: {0}".format(
            "; ".join("{0}={1}".format(k, v) for k, v in sorted(status_counts.items()))
        ))
    draft_ids = stats.get("research_git_draft_ids", []) or []
    if draft_ids:
        lines.append("- new_drafts: {0}".format(", ".join(str(item) for item in draft_ids[:10])))
    if stats.get("research_git_summary_path"):
        lines.append("- summary: {0}".format(stats.get("research_git_summary_path")))
    if stats.get("research_git_summary_latest_path"):
        lines.append("- latest: {0}".format(stats.get("research_git_summary_latest_path")))
    errors = stats.get("research_git_errors", []) or []
    if errors:
        lines.append("- skipped_or_errors:")
        for item in errors[:5]:
            lines.append("  - {0}".format(item))
    return lines


def _render_workspace_inbox(stats: Dict[str, Any]) -> List[str]:
    counts = stats.get("workspace_object_counts", {}) or {}
    if not counts and not stats.get("workspace_latest_path"):
        return ["Workspace Inbox not generated for this run."]
    lines = [
        "- objects: signals={0}; evidence={1}; comments={2}; gaps={3}; drafts={4}".format(
            counts.get("signals", stats.get("workspace_signal_count", 0)),
            counts.get("evidence_items", stats.get("workspace_evidence_count", 0)),
            counts.get("comments", stats.get("workspace_comment_count", 0)),
            counts.get("gap_tasks", 0),
            counts.get("research_drafts", stats.get("workspace_research_draft_count", 0)),
        )
    ]
    if stats.get("workspace_latest_path"):
        lines.append("- latest: {0}".format(stats.get("workspace_latest_path")))
    if stats.get("workspace_latest_json_path"):
        lines.append("- latest_json: {0}".format(stats.get("workspace_latest_json_path")))
    if stats.get("workspace_inbox_path"):
        lines.append("- inbox: {0}".format(stats.get("workspace_inbox_path")))
    if stats.get("workspace_inbox_error"):
        lines.append("- error: {0}".format(stats.get("workspace_inbox_error")))
    return lines


def _render_source_health(stats: Dict[str, Any]) -> List[str]:
    health = stats.get("source_health", {}) or {}
    if not health:
        return ["No source health data."]
    lines = [
        "- overall_status: {0}".format(health.get("overall_status", "unknown")),
        "- overall_score: {0}".format(health.get("overall_score", 0)),
        "- coverage_gap_count: {0}".format(health.get("coverage_gap_count", 0)),
        "- source_degraded_event_count: {0}".format(health.get("source_degraded_event_count", 0)),
        "- remediation_queue_count: {0}".format(stats.get("remediation_queue_count", 0)),
    ]
    low_quality = health.get("low_quality_sources", [])
    if low_quality:
        lines.append("- low_quality_sources: {0}".format(", ".join(str(item) for item in low_quality)))
    if stats.get("source_health_path"):
        lines.append("- source_health_report: {0}".format(stats.get("source_health_path")))
    if stats.get("remediation_path"):
        lines.append("- remediation_report: {0}".format(stats.get("remediation_path")))
    degraded = health.get("source_degraded_events", []) or []
    for event in degraded[:5]:
        lines.append("- source_degraded [{0}] {1}: {2}".format(
            event.get("severity", ""),
            event.get("source_id", ""),
            event.get("detail", ""),
        ))
    remediation_queue = stats.get("remediation_queue", []) or []
    if remediation_queue:
        lines.append("- remediation_tasks:")
        for task in remediation_queue[:8]:
            lines.append("  - [{0}] {1}: source={2}, signal={3}, gap={4}".format(
                task.get("status", ""),
                task.get("task_type", ""),
                task.get("source_id", ""),
                task.get("signal_id", "") or "-",
                task.get("gap", ""),
            ))
    remediation_changes = stats.get("remediation_decision_records", []) or []
    if remediation_changes:
        lines.append("- remediation_changes:")
        for record in remediation_changes[-8:]:
            lines.append("  - [{0}] {1}: task={2}, actor={3}".format(
                record.get("status", ""),
                record.get("action", ""),
                record.get("task_id", ""),
                record.get("actor", ""),
            ))
    for item in health.get("sources", [])[:12]:
        lines.append("- [{0}] {1}: score={2}, latency_ms={3}, fields={4}, duplicate_rate={5}, gaps={6}, reasons={7}".format(
            item.get("status", ""),
            item.get("source_id", ""),
            item.get("score", 0),
            item.get("latency_ms", 0),
            item.get("field_completeness", 0),
            item.get("duplicate_rate", 0),
            ",".join(item.get("coverage_gap_symbols", []) or []) or "-",
            ",".join(item.get("anomaly_reasons", []) or []) or "-",
        ))
    return lines


def _render_run_ops(stats: Dict[str, Any]) -> List[str]:
    run_ops = stats.get("run_ops", {}) or {}
    if not run_ops:
        return ["No runtime operations data."]
    lines = [
        "- overall_status: {0}".format(run_ops.get("overall_status", "unknown")),
        "- elapsed_ms: {0}".format(run_ops.get("elapsed_ms", 0)),
        "- max_run_seconds: {0}".format(run_ops.get("max_run_seconds", 0)),
    ]
    if stats.get("run_ops_path"):
        lines.append("- run_ops_summary: {0}".format(stats.get("run_ops_path")))
    budget = run_ops.get("llm_budget", {}) or {}
    if budget:
        lines.append("- llm_budget: calls {0}/{1}, seconds {2}/{3}".format(
            budget.get("reserved_calls", 0),
            budget.get("max_calls", 0),
            budget.get("reserved_seconds", 0),
            budget.get("max_seconds", 0),
        ))
    stages = run_ops.get("stages", []) or []
    if stages:
        lines.append("- stages:")
        for stage in stages[:12]:
            lines.append("  - [{0}] {1}: {2}ms".format(
                stage.get("status", ""),
                stage.get("name", ""),
                stage.get("duration_ms", 0),
            ))
    degradations = run_ops.get("degradations", []) or []
    if degradations:
        lines.append("- degradations:")
        for item in degradations[:12]:
            lines.append("  - [{0}] {1}: {2} {3}".format(
                item.get("severity", ""),
                item.get("component", ""),
                item.get("reason", ""),
                item.get("detail", ""),
            ).strip())
    return lines


def _render_model_operations(stats: Dict[str, Any]) -> List[str]:
    snapshot = stats.get("model_operations", {}) or {}
    if not stats.get("model_operations_enabled", False):
        return ["- model operations disabled"]
    if not snapshot:
        return ["- no model operations snapshot in this run"]
    quality = snapshot.get("quality", {}) or {}
    grounding = quality.get("grounding", {}) or {}
    usage = snapshot.get("usage", {}) or {}
    cost = snapshot.get("estimated_cost", {}) or {}
    lines = [
        "- status: {0}".format(snapshot.get("status", "UNKNOWN")),
        "- main_success: {0}/{1} ({2:.1%})".format(
            quality.get("main_success", 0),
            quality.get("main_selected", 0),
            float(quality.get("main_success_rate", 1)),
        ),
        "- role_success: {0}/{1} ({2:.1%})".format(
            quality.get("role_success", 0),
            quality.get("role_selected", 0),
            float(quality.get("role_success_rate", 1)),
        ),
        "- grounding: grounded={0}, partial={1}, blocked={2}, usable={3:.1%}".format(
            grounding.get("grounded", 0), grounding.get("partial", 0), grounding.get("blocked", 0),
            float(grounding.get("usable_rate", 1)),
        ),
        "- tokens: prompt={0}, completion={1}, reasoning={2}, total={3}".format(
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
            usage.get("reasoning_tokens", 0), usage.get("total_tokens", 0),
        ),
        "- estimated_cost: {0} {1}; configuration estimate, not provider billing".format(
            cost.get("amount", 0), cost.get("currency", ""),
        ),
    ]
    if stats.get("model_operations_latest_path"):
        lines.append("- audit_report: {0}".format(stats.get("model_operations_latest_path")))
    deadline = snapshot.get("deadline_decisions", {}) or {}
    for component in ("signal_llm", "role_llm"):
        decision = deadline.get(component, {}) or {}
        if decision.get("action") in {"reduce", "skip"}:
            lines.append("- deadline_degradation: {0} action={1}, selected={2}, skipped={3}, reason={4}".format(
                component, decision.get("action"), decision.get("selected_count", 0),
                decision.get("skipped_count", 0), decision.get("reason", ""),
            ))
    breached = [item for item in snapshot.get("checks", []) or [] if item.get("status") in {"WARN", "BREACH"}]
    for item in breached:
        lines.append("- [{0}] {1}: actual={2}, target={3}".format(
            item.get("status"), item.get("name"), item.get("actual"), item.get("target"),
        ))
    lines.append("- boundary: no automatic trading, approval, assignment, or publication")
    return lines


def _render_external_dependencies(stats: Dict[str, Any]) -> List[str]:
    market = stats.get("market_resilience", {}) or {}
    governance = stats.get("market_data_governance", {}) or {}
    llm = stats.get("llm_connectivity", {}) or {}
    if not market and not governance and not llm:
        return ["No external dependency diagnostics."]
    lines: List[str] = []
    if market:
        lines.append("- market_resilience: status={0}, fallback_used={1}, cache_used={2}".format(
            market.get("status", "unknown"),
            market.get("fallback_used", False),
            market.get("cache_used", False),
        ))
        if market.get("missing_symbols"):
            lines.append("  - missing_symbols: {0}".format(",".join(str(item) for item in market.get("missing_symbols", []))))
        if market.get("failures"):
            for failure in market.get("failures", [])[:5]:
                lines.append("  - failure {0}: {1}".format(failure.get("provider", ""), failure.get("diagnosis", "")))
        if market.get("repair_guide"):
            lines.append("  - repair: {0}".format(" | ".join(str(item) for item in market.get("repair_guide", [])[:3])))
    if governance:
        lines.append("- market_data_governance: status={0}, pass={1}, degraded={2}, blocked={3}, llm_eligible={4}".format(
            governance.get("status", "unknown"), governance.get("pass_count", 0),
            governance.get("degraded_count", 0), governance.get("blocked_count", 0),
            governance.get("llm_eligible_count", 0),
        ))
        if not governance.get("approved_internal_research_providers"):
            lines.append("  - provider_attestation: none; normal external-LLM use of price signals is blocked")
        if governance.get("reason_counts"):
            lines.append("  - reasons: {0}".format(", ".join(
                "{0}={1}".format(key, value) for key, value in sorted(governance.get("reason_counts", {}).items())
            )))
    if llm:
        lines.append("- llm_connectivity: status={0}, diagnosis={1}".format(
            llm.get("status", "unknown"),
            llm.get("diagnosis", ""),
        ))
        if llm.get("repair_guide"):
            lines.append("  - repair: {0}".format(" | ".join(str(item) for item in llm.get("repair_guide", [])[:3])))
    return lines


def _render_review_queue(stats: Dict[str, Any]) -> List[str]:
    summary = stats.get("review_queue_summary", {}) or {}
    if not summary:
        return ["- no canonical review queue snapshot"]
    lines = [
        "- outstanding / overdue / at_risk / unassigned: {0} / {1} / {2} / {3}".format(
            summary.get("outstanding_count", 0), summary.get("overdue_count", 0),
            summary.get("at_risk_count", 0), summary.get("unassigned_count", 0),
        ),
        "- pending rows / unique / duplicates: {0} / {1} / {2}".format(
            summary.get("pending_rows", 0), summary.get("pending_unique", 0),
            summary.get("pending_duplicate_rows", 0),
        ),
        "- assignment_mode: suggest_only; automatic_assignment: false",
    ]
    missing = summary.get("missing_human_capabilities", []) or []
    if missing:
        lines.append("- missing verified human capabilities: {0}".format(", ".join(str(item) for item in missing)))
    if stats.get("review_queue_latest_path"):
        lines.append("- queue report: {0}".format(stats.get("review_queue_latest_path")))
    return lines


def _render_research_team(stats: Dict[str, Any]) -> List[str]:
    team = stats.get("team", {}) or {}
    if not stats.get("team_enabled", False):
        return ["投研团队层未启用。"]
    brief = team.get("supervisor_brief", {})
    lines = [
        "- 团队角色数: {0}".format(stats.get("team_role_count", 0)),
        "- 分工任务数: {0}".format(stats.get("team_assignment_count", 0)),
        "- 成员沟通数: {0}".format(stats.get("team_message_count", 0)),
    ]
    if brief:
        lines.append("- 给上司简报: {0}".format(brief.get("headline", "")))
        focus = brief.get("today_focus", [])
        if focus:
            lines.append("- 今日重点: {0}".format("；".join(focus)))
        next_actions = brief.get("next_actions", [])
        if next_actions:
            lines.append("- 下一步:")
            for action in next_actions[:5]:
                lines.append("  - {0}".format(action))
    messages = team.get("messages", [])
    if messages:
        lines.append("- 团队沟通摘录:")
        for msg in messages[:5]:
            lines.append("  - {0} -> {1}: {2}".format(msg.get("from"), msg.get("to"), msg.get("body")))
    guardrails = team.get("guardrails", [])
    if guardrails:
        lines.append("- 边界: {0}".format(", ".join(guardrails)))
    if stats.get("team_brief_path"):
        lines.append("- 团队纪要: {0}".format(stats.get("team_brief_path")))
    if stats.get("team_brief_json_path"):
        lines.append("- 团队纪要JSON: {0}".format(stats.get("team_brief_json_path")))
    return lines


def _render_decision_summary(stats: Dict[str, Any]) -> List[str]:
    summary = stats.get("decision_summary", {}) or {}
    lines = [
        "- 已记录决策: {0}".format(summary.get("total", 0)),
        "- 待上司处理: {0}".format(len(summary.get("pending", []))),
    ]
    counts = summary.get("decision_counts", {})
    if counts:
        lines.append("- 决策分布: {0}".format(
            "；".join("{0}={1}".format(k, v) for k, v in sorted(counts.items()))
        ))
    if stats.get("decision_summary_path"):
        lines.append("- 决策汇总: {0}".format(stats.get("decision_summary_path")))
    if stats.get("decision_summary_json_path"):
        lines.append("- 决策汇总JSON: {0}".format(stats.get("decision_summary_json_path")))
    return lines


def _render_multi_role_llm(signals: List[AnalyzedSignal], stats: Dict[str, Any]) -> List[str]:
    role_signals = [signal for signal in signals if signal.role_analyses or signal.role_synthesis]
    role_errors = [signal for signal in signals if signal.role_analysis_error]
    lines: List[str] = []
    model = stats.get("role_llm_model", "")
    if stats.get("role_llm_runtime"):
        lines.append("- role_runtime: {0}".format(stats.get("role_llm_runtime")))
    if model:
        lines.append("- role_model: {0}".format(model))
    if stats.get("role_llm_provider_used"):
        lines.append("- role_provider: {0}".format(stats.get("role_llm_provider_used")))
    lines.append("- role_success: {0}".format(stats.get("role_llm_success", 0)))
    lines.append("- role_failed: {0}".format(stats.get("role_llm_failed", 0)))
    if not role_signals:
        if stats.get("role_llm_enabled"):
            if stats.get("role_llm_skipped_no_key"):
                lines.append("- skipped: missing LLM_API_KEY/DEEPSEEK_API_KEY")
            else:
                lines.append("- no multi-role result in this run")
        else:
            lines.append("- multi-role agent disabled")
    for signal in role_signals:
        synthesis = signal.role_synthesis or {}
        lines.append("- [{0}] {1}: gate={2}, conflict={3}, consensus={4}".format(
            signal.priority,
            signal.title,
            synthesis.get("decision_gate", ""),
            synthesis.get("conflict_level", ""),
            synthesis.get("consensus_direction", ""),
        ))
        if synthesis.get("governance_status"):
            lines.append("  - governance: {0}; missing_roles={1}".format(
                synthesis.get("governance_status"),
                ",".join(synthesis.get("missing_role_outputs", []) or []) or "none",
            ))
        if synthesis.get("key_disagreement"):
            lines.append("  - disagreement: {0}".format(synthesis.get("key_disagreement")))
        for role in signal.role_analyses:
            lines.append("  - {0}: {1:.0%} {2} | grounding={3} | evidence={4} | missing={5}".format(
                role.get("role_id", ""),
                float(role.get("confidence", 0)),
                role.get("stance", ""),
                role.get("grounding_status", "legacy_unchecked"),
                ",".join(role.get("evidence_refs", []) or []) or "-",
                ",".join(role.get("missing_info", []) or []) or "-",
            ))
            if role.get("thesis"):
                lines.append("    - thesis: {0}".format(role.get("thesis")))
            questions = role.get("review_questions", []) or []
            if questions:
                lines.append("    - review_questions: {0}".format("; ".join(str(item) for item in questions[:3])))
        actions = synthesis.get("next_human_actions", []) or []
        if actions:
            lines.append("  - next_human_actions: {0}".format("; ".join(str(item) for item in actions[:5])))
        if synthesis.get("forbidden_actions_note"):
            lines.append("  - guardrail: {0}".format(synthesis.get("forbidden_actions_note")))
    if role_errors:
        lines.append("- errors:")
        for signal in role_errors[:10]:
            lines.append("  - [{0}] {1}: {2}".format(signal.priority, signal.title, signal.role_analysis_error))
    return lines


def _render_filing_evidence(signals: List[AnalyzedSignal], stats: Dict[str, Any]) -> List[str]:
    filing_signals = [
        signal for signal in signals
        if isinstance((signal.payload or {}).get("filing_evidence"), dict)
    ]
    if not filing_signals:
        return ["暂无公告/财报证据。"]

    lines = ["- 证据数量: {0}".format(stats.get("filing_evidence_count", len(filing_signals)))]
    status_counts = stats.get("filing_evidence_status_counts", {})
    if status_counts:
        lines.append("- 状态分布: {0}".format(
            "；".join("{0}={1}".format(k, v) for k, v in sorted(status_counts.items()))
        ))
    for signal in filing_signals[:10]:
        filing = signal.payload.get("filing_evidence", {})
        financial = signal.payload.get("financial_evidence", {})
        missing = filing.get("missing_fields", []) or []
        metrics = financial.get("metrics", {}) if isinstance(financial, dict) else {}
        parts = [
            "provider={0}".format(filing.get("provider", signal.source)),
            "status={0}".format(filing.get("status", "unknown")),
            "importance={0}".format(filing.get("importance_level") or signal.payload.get("filing_importance_level", "unknown")),
            "type={0}".format(filing.get("announcement_type", "")),
            "period={0}".format(filing.get("report_period", "") or "unknown"),
        ]
        if missing:
            parts.append("missing={0}".format(",".join(str(item) for item in missing)))
        if metrics:
            parts.append("metrics={0}".format(len(metrics)))
        lines.append("- [{0}] {1}: {2}".format(signal.priority, signal.title, "；".join(parts)))
        if signal.url:
            lines.append("  - 链接: {0}".format(signal.url))
    return lines


def _one_line_takeaway(signals: List[AnalyzedSignal], alerts: List[AnalyzedSignal]) -> str:
    if not signals:
        return "今天没有新增有效信号，继续观察既有主题。"
    high = [signal for signal in signals if signal.priority in ["P0", "P1"]]
    if high:
        titles = "；".join(signal.title for signal in high[:3])
        return "高优先级信号集中在 {0} 条事项：{1}。".format(len(high), titles)
    top_topics = Counter(topic for signal in signals for topic in signal.topics).most_common(2)
    if top_topics:
        return "新增信号主要集中在 {0}。".format("、".join(topic for topic, _ in top_topics))
    return "新增信号较分散，优先处理需人工复核的来源缺失或传闻类信息。"


def _review_template(config: Dict[str, Any], signals: Iterable[AnalyzedSignal]) -> List[Dict[str, Any]]:
    fields = config.get("review", {}).get("fields", [])
    templates = []
    for signal in signals:
        row = {field: "" for field in fields}
        if "signal_id" in row:
            row["signal_id"] = signal.signal_id
        templates.append(row)
    return templates
