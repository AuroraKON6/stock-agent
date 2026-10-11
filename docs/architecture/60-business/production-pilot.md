# [60-02] production_pilot.py — 受控生产试运行（v25.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 60-business | 1563 | business_onboarding、provider_registry、market_data_governance、llm_governance、evidence_vault、research_workpapers、review_queue、pi_runtime/skills、audit_log | cli（pilot-*）、dashboard、doctor、business_onboarding（互查）、alerts |

## 职责
Go/No-Go 的最终裁决器：19 类门禁评估、候选指纹、指纹绑定审批与激活、影子运行对账、事件管理。**GO = 门禁 1-17 全 PASS + 6 类真人审批 + publication_approver 手动激活**。

## 关键构件
- **19 类门禁** `_build_gates()`（L579-775）— safety_policy、audit_integrity（5-6 条哈希链全 OK）、business_onboarding_preflight、human_roster（审批门角色+coordinator 可互异指派）、provider_authorization（≥2）、market_data_governance（OK 且 0 阻断）、external_llm_governance（OK 且 0 例外）、official_evidence_vault、official_evidence_coverage（P0/P1 全覆盖）、research_workpapers、research_workpaper_archive、human_review_capacity（0 逾期 + 0 critical unassigned）、scheduled_operations（10 命令按序、agent-run 恰 1 次、Pi 条件外呼一致）、artifact_reconciliation（14-16 类产物存在性+SHA-256+新鲜度+handoff 三级哈希对账）、model_operations、incident_control、shadow_run_evidence（≥5 次/≥3 日/0 未对账，**trigger 必须 scheduled**）、human_approvals（6 门 6 人互异、30 天有效期）、production_activation（当前指纹 active）。
- **指纹机制** — `candidate_fingerprint = object_hash({version, critical_input_hashes, active_providers, required_approval_gates})`（L485）；critical inputs（L1332-1389）含 13 段关键配置 + config/roster/restrictions/控制证据账本文件哈希 + pi registry/lock + 供应商投影 + 证据库摘要。**任何关键输入变化 → 审批/激活/影子证据全部作废**（防"先审批后改配置"）。
- 审批 `record_pilot_approval()`（L137）— 门-角色严格匹配；审批人须经 onboarding preflight 证明合格（L1301）；证据引用绑定当前指纹；不同门不得同一人。事件侧：`record_pilot_incident()/record_pilot_incident_action()` 上报与处置；`revoke_production_activation()`（L233）由 publication/compliance/infosec 执行；同步入口 `sync_production_pilot()`。
- 激活 `activate_production_candidate()`（L188）— 仅 publication_approver，且**必须是此前记录 publication_readiness 审批的同一人**；吊销（L233）由 publication/compliance/infosec 执行。
- 影子对账 — 账本按 reconciliation key（report_path+run_id+scheduled_path+fingerprint）+ 状态哈希去重，带 supersedes 修订；`_shadow_qualification_reasons()`（L945）**14 项**全过才合格（mode=shadow、trigger=scheduled、已对账、scheduled 摘要匹配、名册/onboarding/供应商/行情/LLM/model/复核积压/日跑/外呼一致性/高危事件）。
- 事件 — critical/high 处理仅限 compliance+infosec、上报人不得自行 resolve、需两名不同角色双重确认才能 resolved（L1240）；`_safe_references()`（L1525）用交易词正则拒绝任何含交易指令的证据文本。

## 输出
`reports/production_pilot/latest.*`。当前 NO_GO（12 类阻断，fingerprint `4c6f7624…`）。
