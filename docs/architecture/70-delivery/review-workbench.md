# [70-04] review_workbench.py — 本地回环人工复核工作台

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 1901 | source_remediation、research_runtime、research_operations、review_queue、production_pilot、pi_runtime、alerts、dashboard | scripts/start|stop_research_workbench.ps1、cli（review-workbench） |

## 职责
回环地址 HTTP 服务（默认 127.0.0.1:8767）承载四视图人工操作：remediation（数据源修复）、runtime（案件受控动作）、review（复核队列）、pilot（只读）。**安全模型 = 单机单用户 + 回环绑定 + CSRF/Origin 校验，无用户认证**——actor/role 为表单自报，roster 强制模式（enforce_roster）下从合格人选下拉选择。

## 关键构件
- 四视图 — `load_workbench_state/submit_workbench_action`（L75/L139，动作白名单 5 个）；`load/submit_runtime_workbench_action`（L220/L302，action ∈ research_runtime.VALID_ACTIONS 且非 FORBIDDEN；case_id/actor/role 必填，核心写路径 record_runtime_action）；`load/submit_review_queue_workbench_action`（L346/L513，action ∈ TRANSITIONS，complete_review 强制附复盘字段）；pilot 只读（L436）。
- 表单按阶段出现（L1268-1322）— quality_review→approve/reject；compliance_review→clear/block（必选合规检查项）；human_approval→publication approve/reject；published→start_monitoring；monitoring→close_case；closed→reopen_case。
- HTTP 安全（`_WorkbenchHandler` L1391 起）— **绑定严格解析** `_parse_loopback_authority()`（L667）：仅 127.0.0.1/localhost/::1，偏差 fail-closed；CSRF token（secrets.token_urlsafe(32)）+ `hmac.compare_digest`；**Origin/Host 校验**（`_is_loopback_origin` L724）防跨站与 DNS rebinding；body ≤64KiB + 先消费再路由（防 WinError 10054）；CSP（script-src 'none'）+ nosniff + DENY + no-referrer；≥400 强制关连接。
- 关停 — POST `/shutdown`：Content-Length=0 + loopback + `X-Workbench-Instance` 与服务端 instance_id（token_urlsafe(24)）常量时间比对；通过后后台线程 shutdown；stop 脚本另有 CIM 命令行四重身份校验防误杀。
- 动作后 best-effort 刷新 dashboard/notifications（失败不推翻已落盘动作）；`_contains_trading_term`（L1825）动作名黑名单。

## 备注 / 风险
remediation 路径的 dashboard 刷新无 try/except（L164，语义与其他路径不一致）；每动作全量重建 dashboard。
