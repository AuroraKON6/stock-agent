# [50-06] control_evidence.py — 内部控制证据登记（v29.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 50-gates | 507 | audit_log | provider_registry、business_onboarding、production_pilot、evidence_vault（复核资格）、cli（control-evidence-*） |

## 职责
整个门禁体系的"证据底座"：组织/人员/供应商/档案四类内部控制证据的登记、独立复核、失效。**原件永不入库**——只存本地 SHA-256 + 外部受管档案 URI + 复核事件。

## 关键构件
- 16 类证据白名单（L24-41）— 治理授权、人员雇佣/保密/培训/资质、供应商法律/数据权利/安全/运营、生产档案合同/安全/保留、审批支持；每类有默认复核角色。
- 登记 `register_control_evidence()`（L88）— 只算本地文件哈希 + 登记外部档案 URI（`raw_file_copied_to_project: False`，L157）；`evidence_id = "control:" + object_hash({category, subject_id, sha256, external_archive_ref})[:24]`（同文件同主体同档案幂等）；到期类（资质/培训/供应商三类）必须有 valid_to 且 > valid_from。
- 档案引用校验 `_validate_archive_ref()`（L451）— scheme 白名单 {archive, dms, records, https}；**禁密钥、禁 Windows/绝对路径、禁凭据、禁 query/fragment；https 不得指向 localhost**。
- 复核 `review_control_evidence()`（L166）— 角色必须是该类别要求；**复核人 ≠ 登记人 ≠ 证据主体本人**；多角色复核人互不相同；必须 notes + 复核记录档案引用。
- 状态机 `_evidence_status()`（L393）— revoked > not_yet_valid > expired > rejected > **verified**（全部要求角色 approve 且人互不相同）> under_review > registered。
- `resolve_verified_control_evidence()`（L267）— 链完整 + 存在 + status=verified + 类别匹配 + subject_id 匹配五重校验，任一不满足 fail-closed。
- 吊销 — 仅 compliance/infosec/data_steward。

## 输入 → 输出
写：哈希链事件（`data/control_evidence/` 或配置路径）。

## 备注 / 风险
过期/撤销/哈希变化自动使上层批准失效（business_onboarding 的 input_fingerprint 聚合了本链）。
