# [40-04] research_operations.py — 人员名册 / 授权 / SLA（运营政策层）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 40-research-core | 937 | config | research_runtime（动作授权）、research_cases、research_git、review_queue、business_onboarding、production_pilot、cli（research-ops-*）、llm_governance |

## 职责
机构运营的三块基石：①principal 名册（真人/服务账号）与资格校验；②动作授权（角色/覆盖/限制名单/容量）；③SLA 与升级。**只建议分派，不自动分派**。

## 关键构件
- 名册 `_normalize_principals()`（L586）— principal_type 仅 human|service；service 只能持 4 个分析类角色（`SERVICE_ALLOWED_ROLES` L17）；真人强制角色 7 类（coordinator/lead/quality/compliance/approver/supervisor/data_steward）。资格 `_principal_ineligibility()`（L539）：active、角色授权、**lead_analyst/quality_reviewer 须已核验证券分析师注册**（L692）、compliance 须 compliance_authorized、approver/supervisor 须 publication_authorized、information_side=public、coverage 覆盖、受限实体上 public 侧角色全部不合格、容量 max_active_cases。
- 限制名单 `_normalize_restrictions()`（L632）— observation|restricted，active 条目必须 reason + authorized_by；**restricted 实体：除 compliance_block/close_case 外全部动作被拒**，public 侧停工，直接 L3 升级。
- 授权 `authorize_runtime_action()`（L171）— enforce 关闭 → legacy 放行；开启 → 逐项 deny（未注册/角色不符/受限名单/不合格/指派动作须 human coordinator/supervisor 且 assignee 合格+容量足/complete_task 须被指派者/受保护门禁须 human principal）。`validate_runtime_action()`（L252）把 deny 转 raise。
- SLA — `DEFAULT_STAGE_SLA_HOURS`（L72-113，P0 intake 0.5h → P3 approval 24h），**按营业时间计算**（Asia/Shanghai 周一至五 08:30-18:00 + 节假日，`add_business_hours` L369）；sla_status：on_track/at_risk(80%)/overdue；升级 L0-L3（L475）。显式标注 `sla_policy_type: internal_operating_policy_not_regulatory_deadline`、`assignment_mode: suggest_only`（不自动分派）。
- 快照 `build_research_operations_snapshot()`（L259）— 每案件各角色合格人选（按负载排序）、推荐人选（recommendation_only: True）、staffing_gaps。

## 输入 → 输出
输入：`config/research_roster.local.json` + `config/research_restrictions.local.json`。输出：内存快照供各门禁消费；`reports/research_operations/`。

## 备注 / 风险
当前名册仅 4 个 service principal、0 真人——所有真人门禁结构性 BLOCKED 是**正确控制行为**，禁止用 Agent 名填充。
