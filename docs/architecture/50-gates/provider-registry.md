# [50-05] provider_registry.py — 数据供应商生命周期（v24.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 50-gates | 630 | audit_log、control_evidence、research_operations | market_data_governance、business_onboarding、production_pilot、cli（provider-*） |

## 职责
供应商从候选到激活的治理状态机，把"商业授权"变成可验证的事件链：draft → due_diligence_submitted → approved_pending_activation / rejected → active → suspended / retired。

## 关键构件
- 尽调提交 `submit_provider_due_diligence()`（L125）— 仅 data_steward/research_coordinator；`_validate_evidence()`（L522）**13 项必填证据全非空**（legal_entity、合同、数据权利、保密、安全评估、数据字典、时间戳语义、SLA、事件响应、退出计划、外部模型使用授权等，L27-41）；引用不得含密钥；enforce 时即时校验控制证据。
- 复核 `record_provider_review()`（L163）— 默认 compliance+infosec 双角色；**复核人 ≠ 尽调提交人**（L189）；复核角色之间互不相同（L191）；引用须为可解析 `control:` 证据或受管档案 URI。
- 激活 `provider_activation_eligibility()`（L335）— 未退役、13 项证据齐、scope 覆盖 `internal_research + external_llm`、全部复核 approve、复核人为不同真人、提交人不在复核人内；`_provider_control_evidence_reasons()`（L552）把每个证据字段映射到控制证据类别逐条 `resolve_verified_control_evidence`（subject_id 绑定该 provider）；**激活人独立于全部复核人**。
- fail-closed `load_provider_registry()`（L284）— 链完整性非 OK → `active_provider_ids` 直接为空。
- `load_active_provider_ids()`（L322）— 供行情治理按 scope 过滤。

## 输入 → 输出
写：哈希链事件（`data/provider_registry/`）+ 投影。

## 备注 / 风险
当前 active 供应商 0/2（production_pilot 阻断项之一）；重新尽调会清空全部 reviews（重新走双人复核）。
