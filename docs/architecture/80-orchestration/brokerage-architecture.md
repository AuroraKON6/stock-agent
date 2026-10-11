# [80-05] brokerage_architecture.py — 事件台账与状态投影

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 80-orchestration | 1089 | config、models | 多模块 best-effort 事件转发（cases/git/runtime/remediation/decisions）、cli（event-*/architecture-sync）、dashboard |

## 职责
历史事件驱动的状态投影层：各模块把"发生了什么"追加为事件，本模块提供幂等追加、事件→对象状态投影、压缩、健康指标与审计。每条事件带 `safety_boundary: 仅用于研究、复核、补证和审计；禁止自动执行委托或仓位调整`（L12）。

## 关键构件
- `append_event()`（L25）— idempotency_key 存在时先查重（命中返回 duplicate_skipped）；事件按日追加 `data/events/events_*.jsonl`（普通 JSONL，**无哈希链**——与 workpapers/vault 的强度差异）。
- `quarantine_test_entity()`（L65）— 测试/UAT 实体的隔离投影事件；**仅接受 `_uat` 后缀或 `test_` 前缀来源**（L77），`production_data_deleted: False`。
- `load_events()`（L95）— 排序读取全部事件文件。
- 状态投影/压缩/健康/审计 — 对应 CLI 的 event-state / event-compact（去重压缩）/ event-health（重复率指标）/ event-audit；architecture-sync 从最新报告重建投影。
- 消费方（best-effort 转发）— research_cases 决策、research_git 人工 gate、research_runtime 阶段变化、source_remediation 动作、decisions 记录；**转发用 monkeypatch 临时替换本模块 project_path**（旧模式，异常静默吞掉——系统内已知最脆弱点，战略 §18.2 收敛对象）。

## 备注 / 风险
本模块是"历史 OpenClaw 架构"的留存核心，不再是生产决策路径；事件无哈希链使其定位为**可观测性投影**而非防篡改事实源（防篡改事实源在 workpapers/vault/operations 各链）。
