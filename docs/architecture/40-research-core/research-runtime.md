# [40-02] research_runtime.py — 机构级案件运行时（核心状态机）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 40-research-core | 1831 | research_cases、research_operations、research_workpapers、storage | pipeline、cli（research-runtime-*）、review_workbench、business_onboarding、production_pilot、doctor |

## 职责
全系统的研究核心：11 阶段案件状态机 + fail-closed 同步 + 受保护动作执行。阶段是**由门禁反推的派生值**，不是增量迁移。

## 关键构件
- 入口与读取 — `sync_research_runtime()`（同步主入口）/ `load_research_runtime()` / `get_runtime_case()` / `summarize_research_runtime()` / `write_research_runtime_report()`。
- `STAGE_ORDER`（L21-33）— intake → triage → evidence_collection → analysis → challenge → quality_review → compliance_review → human_approval → published → monitoring → closed。
- `_derive_stage()`（L638）— 由 gates+approvals 反推：closed_at/源案件 noise→closed；取第一个非 PASS gate 对应阶段；quality reject→退回 analysis；publication reject→退回 quality_review；publication PASS+monitoring_started_at→monitoring。
- 门禁 `_evaluate_gates()`（L555）— intake（实体/主题）、triage（优先级）、evidence（可溯源来源>0；**P0/P1 必须 official primary**；source_error 未清/未决补证阻塞）、analysis（论点+基本面[公告类]+市场[price 或 P0/P1]+风险）、challenge（须已记录的**独立**挑战）、三个 approval gate。
- 受保护动作 `PROTECTED_ACTION_ROLES`（L35-45）— quality approve/reject→quality_reviewer；compliance clear/block→compliance_officer；publication approve/reject、start_monitoring、close/reopen→research_supervisor。`_require_protected_actor()`（L1136）三重校验：角色匹配 + **自动化前缀排除**（system/llm/agent/deepseek/pi/openclaw/bot/stock_agent，L82-91）+ 必须是案件中已指派的人。`_validate_assignment_separation()`（L1156）— lead/quality/compliance/publication 四指派互不相同。
- **fail-closed 三原则**（L121-128）— ①上游案件输入为空绝不清空已有 runtime；②缺失信号先 SQLite 回填，仍缺则保留上次已验证状态（不用残缺证据重投影）；③所有降级写结构化 degradation 诊断。
- `record_runtime_action()`（L275）— 三重授权枢纽：`validate_workpaper_gate`（workpapers 层）→ `validate_runtime_action`（operations 层）→ handler → action_history（**截断至 100 条**，更早在 events JSONL）→ `refresh_workpaper_from_runtime_action` 回写底稿。
- `_complete_task()`（L947）— 禁止用 complete_task 绕过三个受保护门禁；`independent_challenge` 必须由已分派 risk_reviewer 完成、不得是 lead、必须附 `risk_note:` 引用（L966-973）。
- 补证/重开 → `_invalidate_downstream_approvals()`（L1168）清空全部下游审批。

## 输入 → 输出
写：`data/research_runtime/runtime.json`、`data/research_runtime/events_YYYYMMDD.jsonl`、`reports/research_runtime/latest.*`。`auto_publish` 恒 False、`external_publication_allowed` 默认 False（L550-551）。

## 备注 / 风险
自动化前缀含 "bot"——真人名以 Bot 开头会被误判；monkeypatch 事件转发（L1340-1352）是待迁移旧模式。
