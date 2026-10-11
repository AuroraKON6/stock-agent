from __future__ import annotations

import json
from typing import Any, Dict, List

from .config import project_path
from .models import AnalyzedSignal, now_iso


def build_team_ops_snapshot(
    signals: List[AnalyzedSignal],
    team: Dict[str, Any],
    stats: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    generated_at = now_iso()
    supervisor_brief = team.get("supervisor_brief", {}) if isinstance(team, dict) else {}
    return {
        "generated_at": generated_at,
        "supervisor_brief": supervisor_brief,
        "human_intervention_queue": _build_human_intervention_queue(signals, supervisor_brief),
        "human_workbench": _build_human_workbench(signals, supervisor_brief),
        "kline_watchlist": _build_kline_watchlist(signals),
        "review_checklist": _build_review_checklist(signals, supervisor_brief),
        "strategy_backlog": _build_strategy_backlog(team),
        "communication_log": team.get("messages", []) if isinstance(team, dict) else [],
        "guardrails": team.get("guardrails", []) if isinstance(team, dict) else [],
        "stats": {
            "inserted": stats.get("inserted", 0),
            "alerts": stats.get("alerts", 0),
            "team_assignment_count": stats.get("team_assignment_count", 0),
            "team_message_count": stats.get("team_message_count", 0),
        },
        "output_only": True,
        "config_auto_modified": False,
    }


def write_team_ops_artifacts(
    signals: List[AnalyzedSignal],
    team: Dict[str, Any],
    stats: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, str]:
    team_cfg = config.get("research_team", {})
    if team_cfg.get("write_artifacts", True) is False:
        return {}

    snapshot = build_team_ops_snapshot(signals, team, stats, config)
    stamp = snapshot["generated_at"].replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    team_dir = project_path("reports/team")
    team_dir.mkdir(parents=True, exist_ok=True)

    md_path = team_dir / "team_brief_{0}.md".format(stamp)
    json_path = team_dir / "team_brief_{0}.json".format(stamp)
    latest_md_path = team_dir / "latest.md"
    latest_json_path = team_dir / "latest.json"

    markdown = _render_team_ops_markdown(snapshot)
    json_text = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    md_path.write_text(markdown, encoding="utf-8-sig")
    latest_md_path.write_text(markdown, encoding="utf-8-sig")
    json_path.write_text(json_text, encoding="utf-8")
    latest_json_path.write_text(json_text, encoding="utf-8")

    return {
        "team_brief_path": str(md_path),
        "team_brief_json_path": str(json_path),
        "team_brief_latest_path": str(latest_md_path),
        "team_brief_latest_json_path": str(latest_json_path),
    }


def _build_kline_watchlist(signals: List[AnalyzedSignal]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for signal in signals:
        has_market_assignment = any(
            item.get("role_id") == "market_watcher" for item in signal.team_assignments
        )
        if signal.source_type != "price" and not has_market_assignment:
            continue
        rows.append({
            "signal_id": signal.signal_id,
            "title": signal.title,
            "priority": signal.priority,
            "latest": signal.payload.get("latest"),
            "pct_change": signal.payload.get("pct_change"),
            "main_net_inflow": signal.payload.get("main_net_inflow"),
            "watch_task": _first_assignment_task(signal, "market_watcher"),
        })
    return rows


def _build_review_checklist(
    signals: List[AnalyzedSignal],
    supervisor_brief: Dict[str, Any],
) -> List[Dict[str, Any]]:
    review_ids = {
        item.get("signal_id")
        for item in supervisor_brief.get("needs_human_decision", [])
        if isinstance(item, dict)
    }
    rows: List[Dict[str, Any]] = []
    for signal in signals:
        if not signal.needs_review and signal.signal_id not in review_ids:
            continue
        rows.append({
            "signal_id": signal.signal_id,
            "title": signal.title,
            "priority": signal.priority,
            "reasons": signal.reasons,
            "questions": signal.research_questions,
            "assigned_roles": [item.get("role_name") for item in signal.team_assignments],
        })
    return rows


def _build_human_intervention_queue(
    signals: List[AnalyzedSignal],
    supervisor_brief: Dict[str, Any],
) -> List[Dict[str, Any]]:
    decision_ids = {
        item.get("signal_id")
        for item in supervisor_brief.get("needs_human_decision", [])
        if isinstance(item, dict)
    }
    queue: List[Dict[str, Any]] = []
    for signal in signals:
        if not signal.needs_review and signal.signal_id not in decision_ids:
            continue
        queue.append({
            "signal_id": signal.signal_id,
            "title": signal.title,
            "priority": signal.priority,
            "decision_owner": "user",
            "status": "waiting_for_supervisor",
            "reason": "人工最终判断 required before any strategy conclusion",
            "allowed_decisions": [
                "approve_research_followup",
                "request_more_data",
                "mark_as_noise",
                "defer_until_more_evidence",
                "confirm_signal",
                "reject_signal",
                "defer_signal",
            ],
            "forbidden_decisions": [
                "auto_trade",
                "order_placement",
                "position_rebalance",
            ],
            "questions": signal.research_questions,
            "role_review_questions": _role_review_questions(signal),
            "role_synthesis": signal.role_synthesis,
            "evidence_gap_tasks": signal.evidence_gap_tasks,
            "evidence_quality": (signal.evidence_chain or {}).get("quality", {}),
            "assigned_roles": [item.get("role_name") for item in signal.team_assignments],
        })
    return queue


def _build_human_workbench(
    signals: List[AnalyzedSignal],
    supervisor_brief: Dict[str, Any],
) -> List[Dict[str, Any]]:
    decision_ids = {
        item.get("signal_id")
        for item in supervisor_brief.get("needs_human_decision", [])
        if isinstance(item, dict)
    }
    rows: List[Dict[str, Any]] = []
    for signal in signals:
        if not signal.needs_review and signal.signal_id not in decision_ids and not signal.evidence_gap_tasks:
            continue
        llm_questions = []
        if signal.llm_analysis:
            llm_questions = [str(item) for item in signal.llm_analysis.get("review_questions", []) or []]
        rows.append({
            "signal_id": signal.signal_id,
            "title": signal.title,
            "priority": signal.priority,
            "decision_gate": signal.role_synthesis.get("decision_gate", ""),
            "role_conflict": {
                "conflict_level": signal.role_synthesis.get("conflict_level", ""),
                "key_disagreement": signal.role_synthesis.get("key_disagreement", ""),
                "consensus_direction": signal.role_synthesis.get("consensus_direction", ""),
            },
            "evidence_quality": (signal.evidence_chain or {}).get("quality", {}),
            "evidence_gap_tasks": signal.evidence_gap_tasks,
            "review_questions": signal.research_questions + llm_questions,
            "role_review_questions": _role_review_questions(signal),
            "allowed_decision_commands": _decision_commands(signal),
            "forbidden_decisions": ["auto_trade", "order_placement", "position_rebalance"],
        })
    return rows


def _build_strategy_backlog(team: Dict[str, Any]) -> List[Dict[str, Any]]:
    feedback = team.get("strategy_feedback", {}) if isinstance(team, dict) else {}
    suggestions = feedback.get("suggestions", []) if isinstance(feedback, dict) else []
    backlog: List[Dict[str, Any]] = []
    for idx, suggestion in enumerate(suggestions, 1):
        backlog.append({
            "id": "strategy_opt_{0:03d}".format(idx),
            "suggestion": str(suggestion),
            "owner_role": "strategy_reviewer",
            "status": "proposed",
            "output_only": True,
            "config_auto_modified": False,
        })
    return backlog


def _first_assignment_task(signal: AnalyzedSignal, role_id: str) -> str:
    for item in signal.team_assignments:
        if item.get("role_id") == role_id:
            return str(item.get("task", ""))
    return ""


def _role_review_questions(signal: AnalyzedSignal) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for role in signal.role_analyses:
        role_id = str(role.get("role_id") or "")
        for question in role.get("review_questions", []) or []:
            rows.append({"role_id": role_id, "question": str(question)})
    synthesis = signal.role_synthesis or {}
    for action in synthesis.get("next_human_actions", []) or []:
        rows.append({"role_id": "synthesis", "question": str(action)})
    return rows


def _decision_commands(signal: AnalyzedSignal) -> List[str]:
    evidence_ref = _first_evidence_ref(signal)
    suffix = " --evidence-ref {0}".format(evidence_ref) if evidence_ref else ""
    return [
        "python -m stock_agent decision-confirm --signal-id {0} --outcome effective{1}".format(signal.signal_id, suffix),
        "python -m stock_agent decision-reject --signal-id {0} --outcome false_positive{1}".format(signal.signal_id, suffix),
        "python -m stock_agent decision-defer --signal-id {0} --outcome pending{1}".format(signal.signal_id, suffix),
    ]


def _first_evidence_ref(signal: AnalyzedSignal) -> str:
    for item in (signal.evidence_chain or {}).get("resolved_refs", []) or []:
        ref = str(item.get("ref") or "").strip()
        if ref:
            return ref
    return ""


def _render_team_ops_markdown(snapshot: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# 投研团队晨会纪要")
    lines.append("")
    lines.append("- 生成时间: {0}".format(snapshot.get("generated_at", "")))
    lines.append("- 输出边界: 仅作为研究辅助，不自动交易、不下单、不调仓。")
    lines.append("")

    brief = snapshot.get("supervisor_brief", {})
    lines.append("## 给上司简报")
    lines.append("")
    lines.append("- {0}".format(brief.get("headline", "暂无简报。")))
    for action in brief.get("next_actions", []):
        lines.append("- 下一步: {0}".format(action))
    lines.append("")

    lines.append("## 人工介入队列")
    lines.append("")
    if snapshot.get("human_intervention_queue"):
        for item in snapshot["human_intervention_queue"]:
            lines.append("- [{0}] {1}: owner={2}, status={3}, allowed={4}".format(
                item.get("priority"),
                item.get("title"),
                item.get("decision_owner"),
                item.get("status"),
                ",".join(item.get("allowed_decisions", [])),
            ))
            role_questions = item.get("role_review_questions", []) or []
            for q in role_questions[:6]:
                lines.append("  - role_question[{0}]: {1}".format(q.get("role_id", ""), q.get("question", "")))
    else:
        lines.append("- 暂无需要上司介入的事项。")
    lines.append("")

    lines.append("## K线盯盘清单")
    lines.append("")
    if snapshot.get("kline_watchlist"):
        for item in snapshot["kline_watchlist"]:
            lines.append("- [{0}] {1}: latest={2}, pct_change={3}, task={4}".format(
                item.get("priority"),
                item.get("title"),
                item.get("latest"),
                item.get("pct_change"),
                item.get("watch_task") or "继续观察K-line、量价和资金流",
            ))
    else:
        lines.append("- 暂无需要盯盘员单独跟踪的K线事项。")
    lines.append("")

    lines.append("## 复盘清单")
    lines.append("")
    if snapshot.get("review_checklist"):
        for item in snapshot["review_checklist"]:
            questions = "；".join(item.get("questions", [])[:3]) or "补来源并确认影响路径"
            lines.append("- [{0}] {1}: {2}".format(item.get("priority"), item.get("title"), questions))
    else:
        lines.append("- 暂无新增人工复核事项。")
    lines.append("")

    lines.append("## 策略优化台账")
    lines.append("")
    if snapshot.get("strategy_backlog"):
        for item in snapshot["strategy_backlog"]:
            lines.append("- {0} [{1}]: {2}".format(
                item.get("id"),
                item.get("status"),
                item.get("suggestion"),
            ))
    else:
        lines.append("- 暂无策略优化建议。")
    lines.append("")

    lines.append("## 团队沟通")
    lines.append("")
    if snapshot.get("communication_log"):
        for msg in snapshot["communication_log"]:
            lines.append("- {0} -> {1}: {2}".format(msg.get("from"), msg.get("to"), msg.get("body")))
    else:
        lines.append("- 暂无团队沟通记录。")
    lines.append("")

    lines.append("## 边界")
    lines.append("")
    for item in snapshot.get("guardrails", []):
        lines.append("- {0}".format(item))
    return "\n".join(lines)
