# [40-03] research_workpapers.py — 结构化研究工作底稿（事件溯源层）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 40-research-core | 1584 | research_runtime、research_cases、audit_log、llm_governance | pipeline、cli、research_runtime（gate 回调）、business_onboarding、production_pilot、doctor |

## 职责
可审计研究底稿：追加式哈希链事件（`data/research_workpapers/events.jsonl`）是**唯一事实源**，latest.json 与 Markdown 只是可重建投影。来源陈述/数据观测/模型推断/人工判断四类信息分开记录，P0/P1 强制官方一手来源。

## 关键构件
- 入口与读取 — `sync_research_workpapers()`（同步建版）/ `verify_research_workpapers()`（校验）/ `refresh_workpaper_from_runtime_action()`（动作后修订）/ `validate_workpaper_policy()` / `load_research_workpapers()` / `write_research_workpaper_reports()`；异常类型 `ResearchWorkpaperError`。
- 事件模型 — 两类事件：`research_workpaper_revision_created`（sync 建版）、`research_workpaper_runtime_action_synced`（动作后修订）；链机制来自 audit_log（链坏即拒绝追加并封锁门禁）。
- **幂等版本化** `build_research_workpaper()`（L280）— 对 core 内容算 `revision_basis_sha256`，相同跳过、不同 version+1；作者固定 `stock_agent_workpaper_service`，`may_pass_protected_gate: False`。
- 四类声明 `_claims_for_signals()`（L736）— source_statement（未经独立验证标注）/ data_observation（行情仅当治理 PASS 且供应商内部认证才 verified，否则 **blocked**）/ model_inference（按 grounding_status 映射 grounded_in_input / partial_grounding / **blocked**——无证据引用的模型结论一律 blocked）/ 人工判断。
- 来源权威 `_authority_tier()`（L1369）— csrc/sac/sse/szse/bse → official_regulator_or_exchange；cninfo → official_disclosure_platform；价格+内部认证 → approved_internal_market_data；eastmoney 等 → secondary_aggregator（**聚合站不得冒充官方来源**在此落地）。
- 挑战状态 `_challenge_state()`（L938）— 收集 agent 反证但显式标注 `agent_output_is_not_independent_human_challenge: True`；`independent_human_challenge_completed` 仅当 runtime 任务由 risk_reviewer + `risk_note:` + 非自动化 actor 完成。
- 验证 `validate_research_workpaper()`（L373）— 分 technical_errors（schema/evidence_id/content_sha256/locator 缺失）与 approval_blockers（P0/P1 无官方原件、模型声明无引用、受保护阶段缺独立人工挑战等）；`approval_eligible` = 两类皆空。
- **门禁 `validate_workpaper_gate()`（L550）** — PASS_ACTIONS（quality_approve/compliance_clear/publication_approve）前 fail-closed：链完整性失败、底稿不存在、challenge 未记录、quality 未批、production archive 未批准（publication 前）均 raise。
- 生产档案 `_production_archive_state()`（L1398）— 本地链明确是 "shadow ledger"，**不是**获批生产档案 → business_status 默认 BLOCKED。

## 输入 → 输出
写：`data/research_workpapers/events.jsonl`（只追加）、`reports/research_workpapers/latest.*`。

## 备注 / 风险
政策校验（L111）：四个 automatic_* 必须 false、保留期 ≥5 年；本地底稿的生产档案升级只能走 evidence_vault 校验或显式 evidence_ref（真人合同链）。
