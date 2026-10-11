# [50-03] market_resilience.py — 行情多源弹性采集

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 50-gates | 491 | sources 各行情适配器 | pipeline（collect 阶段）、market_data_governance（observations）、doctor |

## 职责
行情采集的降级编排：按优先级链尝试多供应商，失败诊断，最后落本地缓存兜底；为治理层提供跨源观测。

## 关键构件
- `collect_market_quotes_with_resilience()`（L18）— 按 `fallback_priority`（默认 eastmoney→akshare→pysnowball→alltick→free_quote→cache）顺序采集；异常转 source_error 信号；**即使 symbol 已覆盖，仍会采集 `verification_providers` 以支持跨源一致性**（L98-104）；缓存是最后兜底且单独标记 cache_used/fallback_used。
- 缓存 `write/load_quote_cache()`（L141-201）— 仅缓存 `_quote_complete`（code+latest+pct_change 齐全）报价到 `data/cache/market_quotes/latest.json`，附 cached_from/cached_at 溯源；TTL 默认 60 分钟；读时注入 source_evidence。
- `seed_quote_cache_from_reports()`（L204）— 从历史 reports/json 回填缓存（CLI market-cache-seed）。
- 网络诊断 `diagnose_network_error()`（L246）— 六类：socket 权限/超时/DNS/TLS/凭据/外部依赖；`network_repair_guide()`（L294）给出修复建议。
- `_annotate_resilience()`（L390）— 每条报价写 payload.market_resilience（provider/fallback_used/fetched_at）+ observed_at。

## 输入 → 输出
写：`data/cache/market_quotes/latest.json`；诊断 dict 进 run stats（`_LAST_MARKET_RESILIENCE`）。

## 备注 / 风险
`load_quote_cache` 把无时区 generated_at 视为 UTC（L462），而治理层把无时区行情时间视为 Asia/Shanghai（L435）——跨模块 naive 时间戳口径差，是已记录的对齐项。
