# [00-03] models.py — 核心数据模型

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 00-foundation | 71 | 无 | sources、analysis、storage、pipeline、reports 等几乎全部模块 |

## 职责
定义贯穿全系统的三个数据契约：原始信号、分析后信号、管道结果。

## 关键构件
- `RawSignal`（L8）— 采集层统一输出：source/source_type/title/summary/url/published_at/payload。
- `AnalyzedSignal`（L20）— 全生命周期载体，约 30 个字段，逐阶段被填充：topics/symbols/priority → research_tracks/questions → team_assignments → role_analyses/synthesis → evidence_chain/gap_tasks → llm_governance/llm_analysis/llm_actions/llm_error。`to_dict()` 用 `asdict()`。
- `PipelineResult`（L57）— generated_at + signals + alerts + 三产物路径 + stats。
- `now_iso()`（L70）— 全系统唯一时间戳生成器（本地时区 ISO，秒精度）。

## 输入 → 输出
纯数据类，无 IO。`signal_id` 不在此生成（在 analysis 层）。

## 门禁 / 安全
无门禁逻辑；但 AnalyzedSignal 的字段集是 SQLite 30 列 schema 与 JSON 报告的隐式契约，增删字段必须同步 storage.py 的 `_JSON_FIELD_MAP` 与迁移列表。

## 备注 / 风险
payload 是自由 dict，各层向其中塞约定键（source_quality、filing_evidence、market_resilience、data_classification 等）——这是系统内最强的隐式契约面，无 schema 校验，靠约定与测试维持。
