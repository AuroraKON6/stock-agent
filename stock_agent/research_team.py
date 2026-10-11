from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Tuple

from .analysis import min_priority_passes
from .models import AnalyzedSignal, now_iso
from .text import unique_keep_order


DEFAULT_ROLES: List[Dict[str, Any]] = [
    {
        "id": "chief_research_coordinator",
        "name": "投研协调负责人",
        "responsibilities": ["拆分任务", "汇总冲突", "向上司汇报"],
        "match_all": True,
        "task_template": "汇总各岗位结论，形成给上司的优先处理顺序。",
    },
    {
        "id": "information_collector",
        "name": "情报采集员",
        "responsibilities": ["补来源", "查公告新闻", "标记信息缺口"],
        "source_types": ["news", "filing", "social", "manual", "source_error"],
        "task_template": "核对来源、链接、发布时间和关键事实，补齐缺口。",
    },
    {
        "id": "market_watcher",
        "name": "盯盘员",
        "responsibilities": ["盯K线", "观察量价", "识别资金异动"],
        "source_types": ["price"],
        "track_ids": ["market_price_action"],
        "task_template": "检查K-line、成交额、涨跌幅、资金流与事件催化是否共振。",
    },
    {
        "id": "news_filing_analyst",
        "name": "新闻公告分析师",
        "responsibilities": ["拆解事件", "核对公告", "判断影响路径"],
        "source_types": ["news", "filing", "social", "manual"],
        "track_ids": ["event_impact"],
        "task_template": "拆解事件类型、影响路径和需要验证的公告/财报证据。",
    },
    {
        "id": "risk_manager",
        "name": "风控复核员",
        "responsibilities": ["反证", "风险挑战", "人工复核边界"],
        "min_priority": "P1",
        "requires_review": True,
        "track_ids": ["risk_challenge", "source_health"],
        "task_template": "列出反证、误判来源和必须人工确认的事项。",
    },
    {
        "id": "strategy_reviewer",
        "name": "策略复盘员",
        "responsibilities": ["复盘有效性", "提出阈值/关键词优化", "记录策略假设"],
        "min_priority": "P2",
        "track_ids": ["llm_deep_dive", "risk_challenge", "market_price_action"],
        "task_template": "把信号沉淀为复盘问题和策略优化假设，只输出建议不改配置。",
    },
]

GUARDRAILS = [
    "no_auto_trading",
    "no_order_placement",
    "no_position_adjustment",
    "human_final_decision_required",
]


def run_research_team(
    signals: List[AnalyzedSignal],
    config: Dict[str, Any],
    stats: Dict[str, Any],
) -> Tuple[List[AnalyzedSignal], Dict[str, Any], Dict[str, Any]]:
    team_cfg = config.get("research_team", {})
    enabled = team_cfg.get("enabled", True)
    roles = _load_roles(team_cfg)
    team_stats: Dict[str, Any] = {
        "team_enabled": bool(enabled),
        "team_role_count": len(roles) if enabled else 0,
        "team_assignment_count": 0,
        "team_message_count": 0,
        "team_roles_active": {},
    }
    if not enabled:
        return signals, _empty_team(enabled=False), team_stats

    messages: List[Dict[str, Any]] = []
    active_counter: Counter = Counter()
    for signal in signals:
        assignments = [_assignment_for_role(signal, role) for role in roles if _role_matches_signal(signal, role)]
        signal.team_assignments = assignments
        for assignment in assignments:
            active_counter[assignment["role_id"]] += 1
        messages.extend(_messages_for_signal(signal, assignments))

    supervisor_brief = _build_supervisor_brief(signals, stats)
    strategy_feedback = _build_strategy_feedback(signals, stats)
    team = {
        "generated_at": now_iso(),
        "supervisor": {
            "id": "user",
            "name": str(team_cfg.get("supervisor_name") or "上司"),
            "relationship": "final_decision_maker",
        },
        "roles": roles,
        "messages": messages,
        "supervisor_brief": supervisor_brief,
        "strategy_feedback": strategy_feedback,
        "guardrails": GUARDRAILS,
    }
    team_stats["team_assignment_count"] = sum(len(signal.team_assignments) for signal in signals)
    team_stats["team_message_count"] = len(messages)
    team_stats["team_roles_active"] = dict(active_counter)
    return signals, team, team_stats


def _empty_team(enabled: bool) -> Dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "supervisor": {"id": "user", "name": "上司", "relationship": "final_decision_maker"},
        "roles": [],
        "messages": [],
        "supervisor_brief": {
            "to": "user",
            "decision_required": False,
            "headline": "投研团队未启用。",
            "today_focus": [],
            "needs_human_decision": [],
            "next_actions": [],
        },
        "strategy_feedback": {"suggestions": [], "output_only": True},
        "guardrails": GUARDRAILS,
        "enabled": enabled,
    }


def _load_roles(team_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = str(team_cfg.get("mode") or "default_plus_config")
    custom = [_normalize_role(role) for role in team_cfg.get("roles", []) if isinstance(role, dict)]
    defaults = [_normalize_role(role) for role in DEFAULT_ROLES]
    if mode == "config_only":
        return custom
    if mode == "default_only":
        return defaults
    return defaults + custom


def _normalize_role(role: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(role.get("id") or "unnamed_role"),
        "name": str(role.get("name") or role.get("id") or "Unnamed Role"),
        "responsibilities": [str(item) for item in role.get("responsibilities", [])],
        "source_types": [str(item) for item in role.get("source_types", [])],
        "track_ids": [str(item) for item in role.get("track_ids", [])],
        "priorities": [str(item) for item in role.get("priorities", [])],
        "min_priority": role.get("min_priority"),
        "requires_review": bool(role.get("requires_review", False)),
        "match_all": bool(role.get("match_all", False)),
        "task_template": str(role.get("task_template") or "处理分配给本岗位的投研任务。"),
    }


def _role_matches_signal(signal: AnalyzedSignal, role: Dict[str, Any]) -> bool:
    if role.get("match_all"):
        return True
    checks: List[bool] = []
    if role.get("source_types"):
        checks.append(signal.source_type in set(role.get("source_types", [])))
    if role.get("track_ids"):
        track_ids = {str(track.get("id")) for track in signal.research_tracks}
        checks.append(bool(track_ids.intersection(set(role.get("track_ids", [])))))
    if role.get("priorities"):
        checks.append(signal.priority in set(role.get("priorities", [])))
    if role.get("min_priority"):
        checks.append(min_priority_passes(signal.priority, str(role.get("min_priority"))))
    if role.get("requires_review"):
        checks.append(bool(signal.needs_review))
    return bool(checks) and any(checks)


def _assignment_for_role(signal: AnalyzedSignal, role: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "role_id": role["id"],
        "role_name": role["name"],
        "signal_id": signal.signal_id,
        "priority": signal.priority,
        "task": _task_for_role(signal, role),
        "status": "pending_human_review" if signal.needs_review else "queued",
    }


def _task_for_role(signal: AnalyzedSignal, role: Dict[str, Any]) -> str:
    task = role.get("task_template") or "处理分配给本岗位的投研任务。"
    if role["id"] == "market_watcher" and "K-line" not in task:
        task = "检查K-line、成交额、涨跌幅、资金流与事件催化是否共振。"
    if signal.research_questions:
        task = "{0} 重点问题：{1}".format(task, "；".join(signal.research_questions[:2]))
    return task


def _messages_for_signal(signal: AnalyzedSignal, assignments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    role_ids = {assignment["role_id"] for assignment in assignments}
    messages: List[Dict[str, Any]] = []
    if "market_watcher" in role_ids:
        messages.append(_message(
            "market_watcher",
            "news_filing_analyst",
            signal,
            "请核对K线异动是否有公告、新闻或资金面催化。",
        ))
    if "risk_manager" in role_ids:
        messages.append(_message(
            "risk_manager",
            "strategy_reviewer",
            signal,
            "该信号需要反证和复盘标签，避免把传闻或来源缺失当成确定性结论。",
        ))
    if "information_collector" in role_ids and signal.needs_review:
        messages.append(_message(
            "information_collector",
            "risk_manager",
            signal,
            "来源或证据仍需补齐，请先按人工复核处理。",
        ))
    if signal.priority in ["P0", "P1"]:
        messages.append(_message(
            "chief_research_coordinator",
            "user",
            signal,
            "高优先级事项已进入团队复核，请上司确认是否需要人工跟进。",
        ))
    return messages


def _message(sender: str, receiver: str, signal: AnalyzedSignal, body: str) -> Dict[str, Any]:
    return {
        "from": sender,
        "to": receiver,
        "signal_id": signal.signal_id,
        "priority": signal.priority,
        "subject": signal.title,
        "body": body,
        "created_at": now_iso(),
    }


def _build_supervisor_brief(signals: List[AnalyzedSignal], stats: Dict[str, Any]) -> Dict[str, Any]:
    high = [signal for signal in signals if signal.priority in ["P0", "P1"]]
    review = [signal for signal in signals if signal.needs_review]
    price = [signal for signal in signals if signal.source_type == "price"]
    focus_items: List[str] = []
    if high:
        focus_items.append("高优先级信号复核")
    if price:
        focus_items.append("K-line/量价异动观察")
    if review:
        focus_items.append("来源与证据补齐")
    focus = unique_keep_order(focus_items)
    next_actions = []
    if high:
        next_actions.append("先看P0/P1信号的来源、影响路径和反证。")
    if price:
        next_actions.append("让盯盘员继续跟踪K-line、成交额和资金流是否持续。")
    if review:
        next_actions.append("把待复核问题写入复盘队列，人工确认后再进入策略结论。")
    if stats.get("source_errors"):
        next_actions.append("检查异常数据源，必要时切换备用源或手工补录。")

    return {
        "to": "user",
        "decision_required": bool(high or review),
        "headline": _brief_headline(signals, high, review),
        "today_focus": focus,
        "needs_human_decision": [
            {"signal_id": signal.signal_id, "title": signal.title, "priority": signal.priority}
            for signal in review[:10]
        ],
        "next_actions": next_actions,
    }


def _brief_headline(
    signals: List[AnalyzedSignal],
    high: List[AnalyzedSignal],
    review: List[AnalyzedSignal],
) -> str:
    if high:
        return "今日有 {0} 条高优先级信号需要上司确认。".format(len(high))
    if review:
        return "今日主要工作是补证据和复核 {0} 条信号。".format(len(review))
    if signals:
        return "今日新增信号以常规观察为主，暂未触发强人工决策。"
    return "今日暂无新增信号，团队维持监控。"


def _build_strategy_feedback(signals: List[AnalyzedSignal], stats: Dict[str, Any]) -> Dict[str, Any]:
    suggestions: List[str] = []
    if stats.get("source_errors"):
        suggestions.append("数据源异常出现，建议在复盘中记录受影响来源和备用源表现。")
    if any(signal.source_type == "price" for signal in signals):
        suggestions.append("价格信号应结合K-line持续性和事件催化，不宜只凭单次快照提高策略权重。")
    if any(signal.needs_review for signal in signals):
        suggestions.append("人工复核信号较多，建议复盘后优化关键词、来源可信度和提醒阈值。")
    if not suggestions:
        suggestions.append("暂无策略调整建议，继续收集样本等待复盘验证。")
    return {
        "suggestions": suggestions,
        "output_only": True,
        "config_auto_modified": False,
        "forbidden_actions": ["auto_trading_forbidden", "order_placement_forbidden", "position_rebalance_forbidden"],
    }
