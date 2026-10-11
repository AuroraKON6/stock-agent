# [20-07] team_ops.py — 团队运营工件

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 336 | research_team、models | pipeline（team_ops 阶段）、dashboard |

## 职责
把虚拟团队的输出物化为可读工件：晨会纪要 Markdown + JSON 快照，并构造人工干预队列（附可直接复制的 CLI 决策命令）。

## 关键构件
- `build_team_ops_snapshot()`（L10）— supervisor_brief、human_intervention_queue、human_workbench、kline_watchlist、review_checklist、strategy_backlog、communication_log、guardrails，整体 `output_only: True`。
- `write_team_ops_artifacts()`（L39）— `reports/team/team_brief_*.{md,json}` + latest；Markdown 为中文"投研团队晨会纪要"（L251）。
- `_build_human_intervention_queue()`（L118）— 每条等待上司的信号列 7 个允许决策（approve_research_followup / request_more_data / mark_as_noise / defer_until_more_evidence / confirm_signal / reject_signal / defer_signal）与 3 个禁止决策。
- `_decision_commands()`（L233）— 生成可复制命令串 `python -m stock_agent decision-confirm/reject/defer --signal-id ... --evidence-ref ...`（evidence_ref 取证据链第一个 resolved ref）。
- `_build_strategy_backlog()`（L198）— strategy_feedback 转 `strategy_opt_NNN` 台账，owner=strategy_reviewer，明确"只输出、不改配置"。

## 输入 → 输出
写入：`reports/team/`。

## 备注 / 风险
决策命令串默认 actor=user——decisions 层无名册强制（与 review_queue 的 roster 校验形成对比），是已知宽松点。
