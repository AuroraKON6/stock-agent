# [10-09] free_quote_adapter.py — AllTick 协议兼容网关

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 22 | alltick_adapter | market_resilience |

## 职责
纯兼容层：让同一套 AllTick 协议可接自建/第三方网关，而不污染源统计。

## 关键构件
- `collect_free_quote()`（L9）— 复制 config，`token_env` 默认 `FREE_QUOTE_TOKEN`、base_url 默认 AllTick，直接委托 `collect_alltick`，再把 `signal.source` 与 `source_evidence.provider` 改写为 `"free_quote"`。

## 输入 → 输出
输入：网关配置。输出：price 类 RawSignal（provider 重写为 free_quote）。

## 门禁 / 安全
无独立逻辑，安全语义继承 alltick。

## 备注 / 风险
存在意义是"多一个独立源候选"，但战略 v30 §7.2 明确：**多个免费接口拼起来不算两家供应商**——技术来源独立与法律授权独立必须同时成立。本适配器当前关闭，且不得用于绕过双供应商门禁。
