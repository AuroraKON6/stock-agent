# [10-02] filing_jsonl.py — 公告类 JSONL 回放

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 193 | models、text | sources 包注册 |

## 职责
消费 eastmoney_filing 落地的公告 JSONL（或手工整理的公告文件），重建结构化公告/财务证据。支持单文件与目录模式（扫描直接子级 `*.jsonl` 按文件名排序）。

## 关键构件
- `collect_filing_jsonl()`（L11）— 入口；`_collect_jsonl_path()`（L23）目录模式扫描。
- `_row_to_signal()`（L56）— 字段别名兼容（company/name、symbol/code、announcement_type/filing_type、report_period/period、url/source_url 等）；无 title 时自动合成；**published_at 缺省回退当前时间（可能掩盖时间缺失）**。
- `_build_filing_evidence()`（L113）— 必填字段 `["symbol","title","announcement_type","report_period","url"]`，任一缺失 → `status="partial"` + `human_review_required=True`。
- `_build_financial_evidence()`（L159）— metrics 为空 → `status="missing"`。

## 输入 → 输出
输入：data/inbox 落地文件。输出：带 `filing_evidence`/`financial_evidence` payload 的 RawSignal。无写盘。

## 门禁 / 安全
公告/财报类信号进入外部 LLM 前必须过证据库门禁（evidence_vault RETAINED_VERIFIED），本模块产出的 partial 证据会一路抬高优先级并强制人工复核。

## 备注 / 风险
当前配置中本源关闭；eastmoney_filing 落地的文件等待本源或人工消费。
