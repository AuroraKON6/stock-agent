# [10-04] eastmoney_adapter.py — 东方财富实时行情

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 159 | models | market_resilience（弹性编排）→ pipeline |

## 职责
从东方财富公开行情接口批量抓取 A 股快照：最新价、涨跌幅、成交量/额、主力净流入、交易所时间戳。

## 关键构件
- `collect_eastmoney()`（L32）— symbols → 一次批量 HTTP（`push2.eastmoney.com/api/qt/ulist.np/get`，带 Referer 伪装 L110-112）。`enabled` 默认 False。
- 字段映射 — f12 代码 / f14 名称 / f2 最新价 / f3 涨跌幅 / f5 量 / f6 额 / f62 主力净流入 / f124 交易所时间戳（L14-29）。
- `_to_secid()`（L123）— 6/9 开头 → `1.code`（沪），其余 → `0.code`（深）。
- `published_at` — 优先交易所 f124 时间戳（>1e11 视为毫秒），回退 `observed_at`。

## 输入 → 输出
输入：symbols 列表。输出：每股一条 `source_type="price"` 的 RawSignal（payload 带行情字段与 source_evidence）。无写盘（缓存由 market_resilience 负责）。

## 门禁 / 安全
抓取失败返回单条 source_error 信号。行情可用 ≠ 授权：进入 LLM 前必须过 market_data_governance（新鲜度/多源一致性/供应商核准）。

## 备注 / 风险
免费公开接口，无商业授权凭证——这正是当前行情治理 FAIL（provider_not_internally_attested）的直接原因；战略 v30 要求至少两家证据准入供应商后才能解除。
