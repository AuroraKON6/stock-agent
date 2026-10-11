# [85-03] release_audit.py — 只读统一发布审计（v31 新增，policy 31.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 85-release-governance | 792 | 无模块级调用（只读各子系统 latest 投影 + git） | value_validation（manifest 锚）、cli（release-audit）、dashboard |

## 职责
把"技术发布状态"收敛为单一只读审计：Git 身份 + G0 控制物 + Pi 安全 + 各子系统新鲜度。**operation_contract（L236-245）声明并执行：无网络、无采集、无 LLM、无业务账本写入、无审批**——唯一副作用是写 `reports/release_audit/`。

## 关键构件
- 入口 — `build_release_audit()`（评估）/ `write_release_audit()`（唯一写副作用）/ `render_release_audit_markdown()`。
- 投影读取 `LATEST_ARTIFACTS`（L43-57）— 约 13 个子系统 `latest.json`（evidence_vault、research_workpapers、review_queue、production_pilot 等）做存在性 + 新鲜度（CURRENT ≤24h / AGING ≤72h / STALE）+ 来源状态告警；**adverse 业务状态只 warning 不 blocker**（BLOCKED 是控制结果不是发布故障）。
- READY 门禁（L117-153）— git 仓库 + HEAD 有效 + worktree 干净；必填本地配置存在（roster/restrictions，仅取 sha256）；**5 个 G0 控制物存在且 PASS**（secret_scan/sbom/license_report/python_environment/backup_restore）；**backup_restore 的 production_readiness 必须 READY**；secret_scan/python_environment/backup_restore 三控制物指纹须等于当前源指纹（防陈旧控制物）；Pi settings 隔离策略逐键校验（L532）+ 包版本匹配 + skill 供应链 READY。
- `manifest_id = object_hash(identity_projection)`（L214）— value_validation 冻结 treatment 时引用它，形成"被度量的即被审计的"。
- 技术与业务分离 — `business_enablement` 恒 `NOT_ASSERTED`，`human_approval_required: True`。
- `--require-ready` 不 READY exit 2。

## 输出
`reports/release_audit/latest.{json,md}`。

## 备注 / 风险
当前无 Git HEAD → release_audit 必然 NOT READY——这正是战略 v30 把"Git 首基线"列为架构 P0 的机械原因。
