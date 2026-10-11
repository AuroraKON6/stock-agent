# [10-08] alltick_adapter.py — AllTick 逐笔行情（可选）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 198 | models | market_resilience、free_quote（协议复用） |

## 职责
AllTick trade-tick 接口逐 symbol 拉取最新成交。

## 关键构件
- `collect_alltick()`（L19）— token 环境变量 `ALLTICK_TOKEN`；`max_symbols_per_run` 默认 5。
- `_fetch_ticks()`（L48）— GET trade-tick，token + 嵌套 JSON query 拼接；`ret` 不在 (None, 0, 200) 抛错。
- `_to_alltick_code()`（L132）— 代码映射。
- 时间解析（L181-198）— 同时支持数值 epoch（毫秒自动换算）和 ISO 文本。

## 输入 → 输出
输入：symbols + token。输出：price 类 RawSignal；**payload 的 `pct_change`/`main_net_inflow` 恒为 None**——即 AllTick 数据永远不会触发 analysis 的价格异常/资金流规则（有意的字段诚实性）。

## 门禁 / 安全
token 缺失/错误 → source_error；行情进 LLM 仍受 market_data_governance 约束。

## 备注 / 风险
当前关闭；字段缺失意味着该源数据在规则层"只见价格不见异动"，多源一致性对账时它只能贡献 latest 一项。
