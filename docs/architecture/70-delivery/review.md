# [70-06] review.py — 复盘汇总与规则优化建议

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 513 | models、config | cli（review-summary）、dashboard（复盘统计） |

## 职责
消费 `data/review/completed_review_*.jsonl` 的真实人工复盘，产出有效性统计与规则优化建议（调噪声源权重、提高有效信号阈值）。**只建议，不自动改配置**。

## 关键构件
- 入口 — `export_review()`（导出待复核）/ `compute_review_summary()` / `write_review_summary()`（汇总落盘）。
- 汇总统计 — false_positive 按关键词/来源归因；有效率按优先级/主题切分；效果窗口默认 5 天（`review.effect_window_days`）。
- 原始 pending/completed/action JSONL 只追加不删除——重复项只能在下层投影合并。

## 输入 → 输出
输入：`data/review/`、`reports/review/`。输出：复盘汇总报告（供日报"复盘反馈"章节与 dashboard 复盘统计）。

## 备注 / 风险
当前 completed=0——复盘闭环尚未真实运转（154 条积压全是 pending），有效信号反推规则的战略目标依赖此层真实数据。
