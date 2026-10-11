# [70-07] review_queue.py — 人工复核队列（v23.0 规范投影）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 572 | research_operations（SLA/名册）、config | pipeline、alerts、doctor、review_workbench、dashboard、business_onboarding、production_pilot |

## 职责
把 append-only 的 pending/completed/action JSONL 投影成去重（按 signal_id 主键）的规范复核队列，带状态机、SLA、升级与"仅建议"分派。**模块 docstring 明确：历史 JSONL 不可变，队列是物化投影，无任何交易/发布/审批/自动分派**。

## 关键构件
- 状态机 `TRANSITIONS`（L38）— pending → in_review / waiting_evidence / deferred / superseded；in_review → complete_review → completed；deferred/completed/superseded → reopen_review → pending。`OPEN_STATUSES`（L28）= pending/in_review/waiting_evidence/deferred。
- 动作角色 `ACTION_ROLES`（L30）— start/request/defer/complete → lead_analyst；reopen/supersede → research_coordinator/research_supervisor；`enforce_roster` 开启时强制名册真人 + 角色匹配（CLI `review-queue-action` 与 workbench 均走 `record_review_queue_action()` 这一共用入口）。其他入口：`write_review_queue_snapshot()` / `load_latest_review_queue()` / `review_queue_config()`；异常类型 `ReviewQueuePolicyError`。
- SLA — P0 0.5h / P1 2h / P2 8h / P3 24h，**营业时间计算**（复用 research_operations 的 add_business_hours）；at_risk 比例默认 0.8；escalation_level 驱动排序（`build_review_queue` L74：升级 > 优先级 > sla_ratio > 时间）。
- 排序键（L104）— (-escalation, priority_rank, -sla_ratio, created_at, signal_id)。
- 摘要（L114）— outstanding/completed/superseded/unassigned/overdue 计数 + 缺失人工能力清单。

## 输入 → 输出
读：`data/review/pending_review_*.jsonl`、`completed_review_*.jsonl`、`data/review/queue_actions/*.jsonl`。
写：`data/review/queue_index.json`、`reports/review_queue/latest.*`。

## 备注 / 风险
当前 154 outstanding / 154 overdue / 154 unassigned / 0 completed，缺 5 类真人能力——队列正确暴露组织缺口，禁止删历史"清理"数字。
