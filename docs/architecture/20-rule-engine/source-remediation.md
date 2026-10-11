# [20-04] source_remediation.py — 补救队列与人工决策

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 739 | source_health、models、config | pipeline、review_workbench（remediation 视图）、cli（attach/resolve/reject/defer 命令） |

## 职责
把数据源降级、健康异常、信号级证据缺口汇成一个去重后的补救任务队列，并管理人工决策生命周期。核心承诺：**队列可重建，人工决策永不丢**。

## 关键构件
- `split_source_degraded_signals()`（L14）— 把 source_error 从主链路剥离成降级事件。
- 回退候选 `_fallback_candidates()`（L462）— 行情 6 源互为候选；eastmoney_filing → filing_jsonl/local_jsonl；rss↔local_jsonl。
- `build_remediation_summary()`（L61）三路合流：降级事件→repair_source；健康 WARN/FAIL→类型化任务（coverage_gap→fill_source_coverage 等）；信号 gap→信号级任务。按 (task_type, source_id, signal_id, gap_type) 四元组去重。
- **状态回放 `_apply_decision_records_to_tasks()`（L335）**— 重放 `data/remediation/decision_log_*.jsonl`，优先 task_id、回退 stable key 匹配——重启后队列重新生成但决策不丢。
- `record_remediation_action()`（L210）— 唯一写入口，action 白名单 5 种；每次决策：追加日志 → 更新任务（decision_history 保 20 条）→ 刷新 latest 报告 → 通知 brokerage_architecture（`_emit_remediation_event` L426，**monkeypatch 对方 project_path + 静默吞异常，系统内最脆弱一处**）。

## 输入 → 输出
写入：`data/remediation/decision_log_{日}.jsonl`（只追加）、`reports/remediation/latest.*`。

## 门禁 / 安全
所有任务 owner 恒为 "human"，带 allowed_actions 与禁交易注记；CLI 层动作经 `_policy_call`。

## 备注 / 风险
monkeypatch 事件转发列入战略 v30 §18.2 收敛清单；gap 归类（`_gap_type` L663）是中文关键词启发式。
