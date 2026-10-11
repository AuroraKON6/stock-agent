# [10-05] eastmoney_filing_adapter.py — 东方财富公告采集（最复杂采集器）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 326 | models、**analysis**（反向用 `_score_filing_importance`） | sources 包注册 |

## 职责
按 symbol 拉取东方财富公告列表，做公告分类、报告期推断、证据分级，并**落地 JSONL 供 filing_jsonl 回放**。是唯一会写盘的采集器。

## 关键构件
- `collect_eastmoney_filings()`（L20）— 每源最多 `max_announcements_per_symbol`（默认 5）条；`write_jsonl` 默认 True 时追加写 `data/inbox/filings/YYYYMMDD.eastmoney.jsonl`（L218-231，先读旧行再拼接重写）。
- `_classify_announcement()`（L253）— 12 类关键词**顺序优先**：annual_report → semiannual → quarterly → performance_forecast → dividend → buyback → shareholder_reduction → financing → regulatory_inquiry → major_contract → governance → legal_opinion；未命中回退栏目名。
- `_infer_report_period()`（L275）— 正则年份 + 标题词 → `{年}A/H1/Q1/Q3/E`。
- 证据分级 — 年报/半年报/季报/业绩预告才把 report_period 列为必填（`_requires_report_period` L293）；缺失 → `filing_evidence.status="partial"` + 人工复核。`financial_evidence` 恒 missing（标题级抓取无财务数字）。
- 采集期预评分（L140）— 直接调用 analysis 的公告重要性评分写进 payload。

## 输入 → 输出
输入：公告 API。输出：filing 类 RawSignal + 落地 JSONL。

## 门禁 / 安全
URL 缺失时用 art_code 拼详情页链接；公告原件必须人工导入证据库后才能外发 LLM。

## 备注 / 风险
sources→analysis 的反向依赖（方向单一、无循环，但耦合）；落地文件的追加重写非原子。
