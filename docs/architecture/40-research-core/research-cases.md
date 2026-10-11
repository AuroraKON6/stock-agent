# [40-01] research_cases.py — 研究案件聚合层

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 40-research-core | 732 | analysis、research_framework、research_operations、llm_agent、audit_log | pipeline、research_runtime、research_workpapers、cli（case-*） |

## 职责
把信号按 (实体, 主题, 催化路径) 聚合成案件，支持 LLM 案件研判与人工案件决策。本层**无角色保护**——真正的门禁保护在 runtime/workpapers 层；本层决策模型是"追加式决策日志重放"。

## 关键构件
- `_case_key()`（L409）— 三元组 (entity, topic, catalyst_path)；catalyst_path 判定（L419）：filing→`filing:{announcement_type}`、price→market_price_action、source_error→source_health、有 tracks 用首个 track id。
- `_case_id()`（L437）— sha1("entity|topic|catalyst_path".lower())[:16]，**案件对同实体/主题/催化天然幂等**。
- `update_research_cases()`（L92）— 加载→重建→可选 LLM→存 `data/research_cases/cases.json` + `reports/cases/` 摘要。
- `enrich_research_cases()`（L116）— LLM 案件分析：≤5 案/轮、ops 预算、重试退避；结果经 `_validate_case_analysis()`（L553）消毒（probability 夹取、决策枚举白名单、human_decision_needed 默认 True）。
- `record_case_decision()`（L252）— 4 种决策（continue_tracking / needs_evidence / mark_noise / archive）；**决策 JSONL 是事实源**，每次加载重放全部记录，noise/archived 为终态（排除 LLM 候选、runtime 直接投影 closed）。
- 决策事件转发 brokerage_architecture（`_emit_case_event` L659，monkeypatch 旧模式）。

## 输入 → 输出
写：`data/research_cases/cases.json`、`data/research_cases/case_decisions_YYYYMMDD.jsonl`、`reports/cases/`。

## 备注 / 风险
任何 actor 都能 mark_noise/archive（含默认 "user"）；noise 不可逆（除非改源案件状态）——治理上依赖 runtime 层受保护门禁兜底。
