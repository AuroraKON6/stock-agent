# [10-06] akshare_adapter.py — AkShare 可选行情

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 95 | models（akshare 为可选依赖） | market_resilience |

## 职责
AkShare 全市场快照拉取后本地按 symbols 过滤。可选依赖，未安装时返回引导性 source_error 信号（指引 `pip install -r requirements-optional.txt`）。

## 关键构件
- `collect_akshare()`（L8）— `ak.stock_zh_a_spot_em()` 全市场 → 代码或名称匹配过滤（L36-43）。
- NaN 处理 `_safe_value()`（L84）— `value != value` 识别 NaN，numpy 标量转原生类型。
- **诚实标注**：`quote_timestamp` 恒为空串（L68）——spot 表无交易所成交时间，保留观察时间，交给治理层判断。

## 输入 → 输出
输入：symbols。输出：price 类 RawSignal（quote_timestamp 为空）。

## 门禁 / 安全
无交易所时间戳的行情在 market_data_governance 中必然因"缺行情时间"被 BLOCK——这是有意的诚实降级，不是缺陷。

## 备注 / 风险
当前关闭；AkShare 新版本建议 Python 3.9+，Python 3.8 主环境下启用会得到数据源异常提示。v31 起在 `.venv311/.venv312` 隔离环境中由 python-env-audit 做 smoke 验证。
