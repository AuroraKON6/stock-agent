# [20-06] research_team.py — 虚拟投研团队（派生层）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 311 | research_framework、models | pipeline、team_ops |

## 职责
把信号按角色规则映射到 6 个虚拟岗位，生成岗位间消息与主管简报。**纯派生、不落盘**——这些"角色"是展示与路由结构，不是真实人员（真实人员在 research_operations 名册，两者严格区分）。

## 关键构件
- `DEFAULT_ROLES`（L11-59）— chief_research_coordinator（match_all 汇总）、information_collector、market_watcher（price）、news_filing_analyst、risk_manager（P1+复核）、strategy_reviewer（P2）。
- `run_research_team()`（L69）— 每信号挂 team_assignments（needs_review 时 status=`pending_human_review`，否则 `queued`）；岗位间消息（盯盘→公告分析师、风控→策略复盘、P0/P1 直报上司）；产出 supervisor_brief（today_focus / needs_human_decision 前 10 / next_actions）与 strategy_feedback。
- **角色匹配 OR 语义**（L178）— `bool(checks) and any(checks)`，与 framework 的 AND 相反：岗位覆盖面更宽，读代码时易误判为 bug。
- GUARDRAILS（L61-66）— no_auto_trading / no_order_placement / no_position_adjustment / human_final_decision_required。

## 输入 → 输出
输入：带 research_tracks 的信号。输出：team_assignments + 团队输出 dict（交给 team_ops 写报告）。

## 备注 / 风险
strategy_feedback 明确 `output_only: True`、`config_auto_modified: False`——策略复盘只输出建议，绝不自动改关键词/阈值（复盘优化必须人工完成）。
