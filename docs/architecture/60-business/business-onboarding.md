# [60-01] business_onboarding.py — 业务接入八阶段（v29.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 60-business | 747 | research_operations、provider_registry、control_evidence、evidence_vault、research_workpapers、production_pilot | cli（onboarding-pack/sync/status）、dashboard、doctor、production_pilot（preflight） |

## 职责
把全部门禁汇总为 8 个**有序**阶段的人工启用流程：每阶段有责任人、完成判据、真实证据要求。`current_phase` = 第一个非 PASS 阶段；**顺序不可跳**。

## 八阶段与完成判据
| # | 阶段 | 责任人 | 判据（判定函数） |
|---|---|---|---|
| 1 | governance_scope | compliance_officer | 真实法人 organization_id（拒绝 replace/pending 字样，L374）+ mandate/scope/prohibited_actions 三控制以组织为主体 verified |
| 2 | human_roster | research_coordinator | 每必需角色 ≥1 名 baseline 合格真人（`_baseline_role_eligible` L673：human+active+角色+分析师注册+授权位+public 侧）+ 逐条 `resolve_verified_control_evidence` + **角色-人可互异指派（二分图回溯 `_distinct_assignment_possible` L690）** |
| 3 | provider_authorization | data_steward | ≥2 个活跃且运营合格供应商 |
| 4 | official_evidence_backfill | information_analyst | 每个 P0/P1 底稿有 primary_source_eligible + original_document_retained + vault_document_id |
| 5 | governance_remediation | compliance_officer | 行情治理与 LLM 治理 OK/PASS/READY 且待决例外=0 |
| 6 | production_archive | information_security_officer | vault+底稿 archive ready + 组织名下 3 项档案控制证据 verified |
| 7 | shadow_readiness | research_coordinator | 合格影子运行 ≥5 次、跨 ≥3 交易日、0 未对账（**只认计划任务触发，手工重跑不算**） |
| 8 | go_no_go | publication_approver | pilot overall=GO + 全部指纹绑定审批 approve + 当前指纹已手动激活 |

## 关键构件
- `build_business_onboarding()`（L139）— 重排、算 current_phase、生成人工行动队列、**input_fingerprint**（L186：控制证据/roster/provider/证据库/底稿/pilot 指纹聚合哈希——任何上游变化即失效）。
- `generate_business_onboarding_pack()`（L229）— 输出 5 份人工填写模板（00-04），**不改变生产状态**。

## 输出
`reports/business_onboarding/latest.*` + intake_pack。

## 备注 / 风险
当前 0/8 停在 governance_scope；human_roster 阶段对每 principal × 每证据类别重复全链解析，O(N×M×链长) 性能已记录。
