# [60-03] value_validation.py — G0.5 配对价值验证（v31 新增）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 60-business | 1672 | release_audit（manifest 锚）、research_operations（真人授权）、audit_log | cli（11 个 value-* 命令 + status）、dashboard、doctor |

## 职责
战略 v30 的 G0.5 门禁落地：用 20-30 个真实历史 P0/P1 案件做"人工基线 vs 系统辅助"配对对照 + 独立盲评，证明 ≥20% 运营效率改善且质量不降。**防 mock 是第一设计目标**。

## 关键构件
- 事件溯源 — 11 类事件（L39-51）追加至 `data/value_validation/events.jsonl`（哈希链）；`_project_events()`（L848）重放并校验 cohort 指纹与 reveal commitment。
- **盲评 sealed mapping** — commit=sha256(mapping+nonce+ids)；sealed 写 `data/value_validation/sealed/`（chmod 0600，Windows 上效果有限需目录权限兜底）；reveal 三重比对（L601-610）；盲评 A/B 包写 `reports/value_validation/blind_assignments/`。
- **防合成样本七重闸**：①`template_only: True` 直接拒绝（L1284/L1360）；②measurement_method 仅 time_log/calendar_reconstruction 且在白名单；③segment 分钟与 active_minutes 对账 ±5%；④`_authorize_human()`（L1459）强制名册 active 真人 + 角色匹配 + public 侧，lead/quality 须已核验分析师注册；⑤盲评人独立性（不得是 baseline/assisted 分析者或制包人 + 双 attest）；⑥assisted 须 `--attest-outcome-hidden/--attest-evidence-cutoff`，输出 ref/sha256 不得与 baseline 相同，observed_manifest_id 须等于冻结 treatment；⑦输出 ref 禁含 baseline/assisted/agent 等泄盲词（L82-91）；任何偏差 → `qualifying=False` 不计业务样本。
- **12 项价值门禁** `_build_value_gates()`（L1095-1191）— event_integrity 事件链完整、study_opened、frozen_case_set（cohort 20-30 案且 ≥2 层）、policy_frozen、g0_treatment_freeze（**冻结时 release_audit 必须 READY 并绑定 manifest_id，L345-347，保证"被度量的就是被审计的版本"**）、paired_case_completeness 全量配对、blind_review_coverage 盲评覆盖、protocol_integrity 零协议偏差、human_time_improvement 人时中位改善 ≥20%、quality_non_inferiority 质量非劣、material_defect_non_increase 重大缺陷不增、unauthorized_egress 未授权外发=0。
- 工作流 API（与 11 个 value-* CLI 一一对应）— open_value_study / register_value_case / exclude_value_case / freeze_value_cohort / record_value_baseline / freeze_value_treatment / record_value_assisted / prepare_blind_assignment / record_blind_evaluation / reveal_blind_assignment / record_value_gate_decision；读取走 build_value_validation_status / load_latest_value_validation。
- 控制物 — 3 份 G0.5 模板 `config/value_baseline_metrics.example.json`、`value_assisted_metrics.example.json`、`value_blind_scores.example.json`。
- `record_value_gate_decision()`（L627）— CONTINUE 仅当系统建议为 CONTINUE，决策人须 supervisor（publication_authorized 真人）；operation_contract（L731）：无自动选案、无 LLM、无交易。

## 输出
`reports/value_validation/latest.*`；CLI `--require-ready` 全门禁 PASS 才 exit 0。

## 备注 / 风险
依赖链 value_validation → release_audit → {release_controls, python_environment, backup_restore} 是 v31 新增的证据脊柱。
