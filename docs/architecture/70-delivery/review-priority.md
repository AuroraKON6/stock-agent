# [70-08] review_priority.py — 修复优先级队列

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 220 | source_remediation、review_queue、config | cli（review-priority）、review_workbench（workbench 快照） |

## 职责
把补救任务与复核积压合并成一个按"影响 × 紧迫度"排序的优先级队列，供工作台离线快照与人工排序参考。

## 关键构件
- 入口 — `build_priority_queue()`（聚合评分排序）/ `write_priority_queue()`（落盘 latest+时间戳）。
- 输出 — `reports/workbench/priority_queue_{stamp}.json` + `priority_queue_latest.json`。

## 输入 → 输出
读：remediation latest、review queue 投影。写：`reports/workbench/priority_queue_*`。

## 备注 / 风险
纯建议层；不改变 remediation/review 两层的事实源与状态。
