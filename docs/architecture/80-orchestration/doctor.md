# [80-04] doctor.py — Go-Live 健康检查

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 80-orchestration | 1295 | llm_agent（私有函数）、llm_governance、model_operations、market_data_governance、pi_runtime、agent_handoff、evidence_vault、research_workpapers、business_onboarding、production_pilot、review_queue、audit_log | cli（doctor）、scripts/go_live_check.ps1 |

## 职责
约 30 项检查的一次性体检（入口 `run_doctor()`，落盘 `write_doctor_report()`），输出 OK/WARN/FAIL 计数与总体判定（任一 FAIL→FAIL）。原则：治理/审计/门禁异常宁可 WARN/FAIL 也不放行。

## 检查面（按类别）
- system/config — Python ≥3.8；配置可解析。
- sources — local_jsonl 存在性；source_health 状态映射；行情治理（OK 且无 attestation/共识/冲突注记才 OK）。
- llm — key 存在（绝不回显）；连通性探测（`_probe_llm_connectivity` L151：仅 llm_agent.enabled 时请求 `base_url/models`，8s——**系统唯一主动联网之一**）；LLM 治理策略硬校验（enforce/仅 public/四检测/审计）；**model_operations 快照递归扫描敏感字段**（api_key/prompt_content/response_content 等出现即 FAIL）+ 四个 automatic_* 非 False 即 FAIL。
- pi — 运行时安装/版本/隔离 settings/供应链；agent_handoff 三级哈希 + "0 公开信号却调用了 Pi"即 FAIL。
- reports — dashboard/notifications/workspace 产物存在性。
- runtime — **策略硬校验**（auto_trade/auto_publish/external_publication 必须 false，human_approval/role_separation 必须 true，任一不满足→FAIL）；vault/workpapers technical FAIL→FAIL、business 阻塞→WARN。
- operations — research_operations（assignment_mode 必须 suggest_only、发生过自动分派→FAIL）；onboarding/pilot（append-only 链完整性 FAIL→FAIL；decision≠GO→WARN）。
- scheduled — PowerShell 查询 4 个预期任务（未安装/disabled→WARN，这是当前正确状态）；最近计划运行 SLO。
- review — 积压阈值（backlog 20/pending 文件 30）；invalid JSONL→WARN 且 note 明确"不要删除审计文件"。
- recent — 最新报告 source_error 或 P0/P1。

## 输出
`reports/health/latest.{md,json}` + 时间戳副本。当前总体 FAIL（唯一 FAIL = Production Pilot 业务态，属预期）。

## 备注 / 风险
import llm_agent 私有函数（跨模块耦合已记录）；`_check_openclaw*` 已停用但保留 fail-closed 遗留语义。
