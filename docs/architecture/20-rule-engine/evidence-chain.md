# [20-02] evidence_chain.py — 证据链构建

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 183 | models、text | pipeline（agent-run 的 evidence_chain 阶段） |

## 职责
为每条信号收集证据引用并解析归类，把解析不了的引用变成"缺口任务"（owner=human），给出信号级证据质量分。这是"每个结论可回溯证据"原则在规则层的执行点。

## 关键构件
- `attach_evidence_chains()`（L16）/ `build_evidence_chain()`（L31）。
- 引用收集 `_collect_refs`（L84）— signal.url、summary、role_analyses[].evidence_refs、llm_analysis.evidence_refs，保序去重。
- 引用解析 `_resolve_ref`（L107）— 三级回退：RawSignal 属性 → payload 点路径 → to_dict() 点路径；分类 `_category_for_ref`（L137）：raw_signal / market / financial / filing / news。
- 缺口任务 `_gap_task()`（L151）— task_id `{signal_id}:gap:{n}`，**owner="human"、status="open"、allowed_decisions=[confirm/reject/defer/request_more_data]**。
- 质量分 `_quality`（L163）— 已解析/总数，每 gap 扣 min(0.4, 0.05×gap)；≥0.8 且零 gap → OK，≥0.3 → WARN，否则 FAIL。
- chain 内嵌 `forbidden_actions_note`（L53，禁止自动交易）。

## 输入 → 输出
输入：LLM 增强后的 AnalyzedSignal。输出：signal.evidence_chain + evidence_gap_tasks + 汇总统计（avg_score/status_counts 进 run stats）；结果由 pipeline 写回 SQLite。

## 门禁 / 安全
缺口任务只能人工处理；未解析引用不影响门禁判定（真正的批准门禁在 workpapers/runtime 层做）。

## 备注 / 风险
本模块校验的是"引用 ID 能否解析到输入"，不是"声明是否被原文支持"——声明级 grounding 是战略 v30 §6.4 的升级目标（value_validation 已开始按此度量）。
