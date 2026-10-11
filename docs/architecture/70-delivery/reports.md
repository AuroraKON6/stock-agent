# [70-01] reports.py — 日报 / JSON / 预警输出

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 1016 | analysis、models、config | pipeline（唯一入口）、dashboard/alerts/doctor（读产物） |

## 职责
每轮运行的最终物化：Markdown 日报（约 24 个章节）、全量 JSON 快照（含 review_template）、JSONL 预警。

## 关键构件
- `write_outputs()`（L13）— 排序（-priority_score + published_at 升序平局）；四类视图子集（price/filing/llm_scope/role_scope）；`daily_top_n` 默认 20；预警按 `min_priority_for_alert`（默认 P1）。
- 三产物 — `reports/daily/{stamp}.md`（**utf-8-sig** 兼容 Windows 工具链）、`reports/json/{stamp}.json`（**全量** signals/alerts/stats/review_template——P3 与低优先级数据靠这里传给下游）、`reports/alerts/{stamp}.jsonl`。
- 日报章节序（`_render_markdown` L80-356）— 头部指标 → 一句话结论 → 研判框架 → Research Cases → Runtime → Evidence Vault → Workpapers → Research-as-Git → Workspace → Source Health → Run Ops → Model Ops → External Deps → Review Queue → 投研团队 → 人工决策 → 优先处理（逐信号）→ 主题分布 → 行情快照（含治理 gate/session/llm_eligible）→ 公告证据 → 人工复核 → 复盘字段/反馈 → LLM 治理 → LLM 研判 → 建议动作 → 失败降级 → Multi-Role。
- `_review_template()`（L1008）— 每信号空白复盘行模板。

## 备注 / 风险
主题分布/行情快照统计基于**截断后 top_n** 而非全量（JSON 是全量）——两处口径不同，读报告时需留意。
