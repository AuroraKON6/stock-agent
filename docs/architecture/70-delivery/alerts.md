# [70-02] alerts.py — 预警/通知生成

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 485 | review_queue、production_pilot、config | review_workbench（动作后刷新）、dashboard（链接） |

## 职责
聚合五~六类最新产物（报告/预警/待复核/动作/runtime/pilot），按约 13 类触发条件生成本地通知 Markdown。**无推送通道**——只写 `reports/notifications/`，纯本地。

## 关键构件
- `generate_notifications()`（L281）— `_load_latest_json_report/alerts/pending_review/actions/runtime`（坏行静默跳过，不中断）；生产试点启用时再读 pilot 快照。
- 触发条件 `_check_triggers()`（L126-278）— high_priority_alerts（P0/P1）、llm_human_decision（confidence≥0.75 且需人工）、source_errors、pending_review / review_sla（overdue/at_risk，注明"仅升级提醒不自动分派"）、source_health / runtime_ops / market_data_governance（WARN/FAIL）、external_dependencies、research_runtime_human_gate（案件停在 quality/compliance/approval）、compliance_block、**pilot 三项**（technical 阻断 / 非 GO / open critical-high 事件）。
- **无论是否触发都写文件**——无触发写"状态： 正常"（L321-327），保证产物存在性可被 doctor 检查。

## 输出
`reports/notifications/latest.md` + 时间戳副本。

## 备注 / 风险
docstring 说"无触发返回 None"但实现总返路径（文档与实现不一致，已记录）；`actions` 参数未参与触发判定只进统计。
