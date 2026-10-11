# [10-00] sources/ 包 — 采集适配器注册表

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | `__init__.py` 21 | models、text、analysis（仅 eastmoney_filing 反向用） | pipeline（唯一调用方） |

## 职责
统一导出 9 个 `collect_*` 函数，每个适配器把外部数据转成统一的 `RawSignal`。pipeline 的 `collect_sources_with_health` 按 source_plan/price_plan 引用它们。

## 适配器清单与启用状态（config/watchlist.local.json）
| 适配器 | source_type | 默认启用 | 会写盘 |
|---|---|---|---|
| local_jsonl | manual/news | 是 | 否 |
| filing_jsonl | filing | 否 | 否 |
| eastmoney_filing | filing | 是 | **是**（落地 JSONL） |
| rss | news | 是 | 否 |
| eastmoney | price | 是（经 market_resilience） | 否 |
| akshare / pysnowball / alltick / free_quote | price | 否 | 否 |

## 统一错误语义
任何采集异常都由 pipeline `_collect_one_source` 捕获，转成一条 `source_type="source_error"` 的 RawSignal（不中断管道），随后被 source_remediation 剥离进健康/补救链路。错误一定可见，但绝不进入分析主链。

## 备注 / 风险
- eastmoney_filing_adapter 反向 import `analysis._score_filing_importance`（sources→analysis 的唯一逆向依赖，方向单一但耦合）。
- `_source_error`/`_num` 等辅助函数在 5 个适配器中复制粘贴约 4 份，维护风险。
- 适配器存在 ≠ 商业授权：生产行情必须过 provider_registry 准入（层 50）。
