# [20-03] source_health.py — 数据源健康检查

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 482 | text、models、config | pipeline、doctor、dashboard/reports（经 latest 投影） |

## 职责
对每个数据源的采集事件打分（成功率/字段完整度/覆盖度/信任度/去重），生成 OK/WARN/FAIL 状态与异常标签，并把源质量写回每条信号。

## 关键构件
- 信任基线 `DEFAULT_TRUST`（L11-22）— eastmoney 系 0.85；filing_jsonl/pysnowball/alltick/akshare 0.75；local_jsonl/free_quote 0.70；rss 0.65；market_cache 0.55；未知 0.60（可被 `source_health.trust_scores` 覆盖）。
- 打分 `_score_event`（L171）— `score = 100 × (0.35 成功 + 0.25 字段完整 + 0.15 覆盖 + 0.15 信任 + 0.10 去重)`。
- 字段完整 `_field_completeness`（L320）— 按 source_type 必填字段表（price 6 项、filing 5 项、news/manual 4 项）；filing_evidence 缺字段每项再扣 0.15。
- 覆盖度 — SH600000/600000.SH/1.600000 归一为同一 6 位代码比对期望 symbols。
- 状态机（L237-277）— 异常标签：collector_exception / source_error / fallback_resolved / empty_enabled_source / field_missing / coverage_gap / duplicates / low_trust(<0.6)。**source_error 在 warn_only 白名单 → WARN；fallback 已解决 → WARN；其余 exception/FAIL 分 → FAIL；low_trust 显式不阻断**。
- 信号级回写 `attach_source_quality()`（L121）— 源指标写入 `payload["source_quality"]` + 信号级 gaps/needs_human_review。
- 落盘 — `reports/source_health/source_health_{stamp}.json/.md` + `latest.*`（md 用 utf-8-sig 兼容 Excel）。

## 输入 → 输出
输入：采集事件（pipeline `_collect_one_source` 产出）。输出：健康摘要 + 信号 payload 回写 + 报告。

## 备注 / 风险
`_build_source_degraded_events()`（L477）函数内延迟 import source_remediation 以打破循环依赖；评分权重为配置化硬编码，调整时历史报告不可比。
