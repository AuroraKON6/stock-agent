# [10-03] rss.py — RSS/Atom 新闻源

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 128 | models、text | sources 包注册 |

## 职责
纯标准库（无 feedparser）的 RSS 2.0 / Atom 拉取与解析。

## 关键构件
- `collect_rss()`（L17）— 遍历源列表，UA `stock-agent/0.1`。
- `_collect_one()`（L37）— 按根节点 tag 是否以 `feed` 结尾区分 Atom/RSS。
- `_parse_rss()`（L52）/ `_parse_atom()`（L72）— 条目提取；日期统一转本地时区 ISO（L111-127）。

## 输入 → 输出
输入：配置的 RSS/Atom URL。输出：`RawSignal`（source_type 默认 `news`）。无写盘。

## 门禁 / 安全
**错误即信号**：单源异常生成 `source_type="source_error"` 的 RawSignal 进入管道（不抛出），供 source_health 统计。拉取的新闻是 llm_governance 提示注入检测的重点对象。

## 备注 / 风险
当前配置 rss 关闭。无条件并发、无速率限制——多源高频启用时需要补（战略 v30 Provider Contract v2 要求速率限制与退避）。
