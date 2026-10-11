# [80-01] pipeline.py — 主流水线编排

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 80-orchestration | 751 | 几乎全部模块（阶段调用方） | cli（run/agent-run）、scripts/run_daily_research.ps1 |

## 职责
两条编排主线的唯一实现：`run_pipeline`（纯规则，无 LLM）与 `run_agent_pipeline`（加 LLM/角色/证据链/治理）。两者都用 `project_path_override(project_path)` 把全程 IO 锁进项目根，`RunOpsContext` 提供阶段计时与预算。

## run_agent_pipeline 阶段序列（L178-380）
```
collect（9 源 + market_resilience + 行情治理评估）
→ rule_analysis（source_quality 回写 → analyze → decision context → research_framework → research_team）
→ official_evidence_vault（五态打标 + 自动补采 ≤3）
→ storage（insert-or-skip 去重，得 inserted）
→ llm_signals（仅 inserted：门槛→deadline→治理预检→调用→校验→写回 SQLite）
→ multi_role_llm（同门禁平行链）
→ evidence_chain（引用解析 + 缺口任务 + 写回）
→ llm_governance（汇总报告）
→ actions（export_review + action log，isolated 跳过）
→ research_git（auto-stage，默认关）
→ source_health → team_ops → review_queue
→ model_operations（SLO/用量）
→ research_cases → research_runtime → research_workpapers
→ decisions → run_summary → write_outputs
→ workspace_inbox + brokerage_architecture 同步（isolated 跳过）
```
`run_pipeline` 缺中间 LLM 四阶段（llm_signals/multi_role/evidence_chain/governance/model_ops），其余相同。

## 关键机制
- 采集入口 — `collect_sources()` / `collect_sources_with_health()`（源事件+健康摘要，供 `_collect_for_pipeline` 使用）。
- **isolated run**（`_is_isolated_run` L412，`run_profile.isolated`）— 不写生产投影、决策上下文置空、动作不导出：全链路测试与 UAT 的隔离保证。注意：生产配置（watchlist.local/example）**没有** `run_profile` 键，默认 false；当前只有 `llm_governance_live_test.example.json` 启用。
- **外层编排**（scripts/run_daily_research.ps1 daily 模式的 11 步，含本模块之外的部分）：market-cache-seed → agent-run（即上方阶段序列）→ evidence-vault-status --require-ready → pi-handoff-run（-SkipPi 时改 agent-handoff）→ review-queue-sync → research-ops-plan → workspace-sync → production-pilot-sync → business-onboarding-sync → dashboard → alerts；末尾写 logs/scheduled/run_*.summary.json + SLO 判定。
- LLM 仅对 inserted 新信号执行——重复信号零调用。
- `_execute_export_review`（L507）/`_execute_write_action_log`（L532）— 待复核 JSONL 与动作建议 JSONL（带禁交易注记）。
- `_llm_connectivity_from_run_ops`（L726）— 从降级记录推断连通性并给修复指南。

## 备注 / 风险
三个 `_update_signal_*` 直写 SQLite UPDATE（绕过 SignalStore）；`_LAST_*` 模块级全局传递行情诊断（单进程假设）。
