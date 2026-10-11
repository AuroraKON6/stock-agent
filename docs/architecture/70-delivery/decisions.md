# [70-09] decisions.py — 上司决策台账

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 258 | models、config、brokerage_architecture | cli（decision-list/record/summary/confirm/reject/defer）、team_ops（命令串）、pipeline（decision context） |

## 职责
人工对信号的研究决策台账：7 种合法决策、6 种结果枚举、7 种禁止决策（自动交易类）硬编码拒绝。**无角色强制**（actor 默认 "user"）——与 review_queue 的名册校验相比是宽松层，属快速人工通道。

## 关键构件
- `VALID_DECISIONS`（L12）— approve_research_followup / request_more_data / mark_as_noise / defer_until_more_evidence / confirm_signal / reject_signal / defer_signal。
- `FORBIDDEN_DECISIONS`（L31）— auto_trade / place_order / order_placement / rebalance_position / position_rebalance / buy / sell → **ValueError 拒绝**（L52）。
- `record_decision()`（L42）— 追加 `data/decisions/decisions_YYYYMMDD.jsonl`（record_id 带时间+uuid8）+ 发事件给 brokerage_architecture。
- `compute_decision_summary()`（下游）— total/决策家族/结果计数 + latest_by_signal + pending 清单；pipeline 注入 `apply_decision_context` 给信号（isolated run 时用空 context 隔离）。

## 输入 → 输出
写：`data/decisions/`、`reports/decisions/`。

## 备注 / 风险
evidence_refs 由调用方传入（team_ops 生成的命令串会带第一个 resolved 证据引用）；决策与 review_queue 状态机是两条平行通道，长期应收敛（战略 §18.2"减少平行入口"）。
