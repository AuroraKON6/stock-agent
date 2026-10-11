# [10-07] pysnowball_adapter.py — 雪球行情（可选）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 245 | models（pysnowball 为可选依赖） | market_resilience |

## 职责
通过 pysnowball 包拉取雪球行情快照，带源级证据构造与批量上限。

## 关键构件
- `collect_pysnowball()`（L14）— 入口；Token 环境变量 `XUEQIU_TOKEN`（回退 `PYSNOWBALL_TOKEN`），缺失返回 source_error（L22-25）。
- `max_symbols_per_run` 默认 10（L32）。
- 代码转换 `_to_xueqiu_symbol()`（L176）— `XXX.SS/.SH/.SZ/.BJ` 或裸 6 位（6/9→SH，0/2/3→SZ，4/8→BJ）。
- `_extract_rows()`（L65）— 兼容 5 种返回结构（list / data.list / data.items / data / payload 自带 symbol）。
- 源证据 `_build_source_evidence()`（L135）— required `["current","percent"]`，缺失 → partial + 注释"LLM 需要降低置信度并要求人工复核"。

## 输入 → 输出
输入：symbols + token。输出：price 类 RawSignal。

## 门禁 / 安全
`del timeout`（L50）显式声明 pysnowball 不支持每请求超时——已知限制被文档化。

## 备注 / 风险
当前关闭；作为第二行情供应商候选时，仍需走 provider_registry 的 13 项证据准入（法律授权独立于技术可达）。
