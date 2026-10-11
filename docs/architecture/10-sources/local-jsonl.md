# [10-01] local_jsonl.py — 手工情报池

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 10-sources | 49 | models、config | sources 包注册 |

## 职责
读取配置指定的本地 JSONL 文件（每行一个情报对象），是最基础、唯一默认开启且完全本地的人工情报入口。

## 关键构件
- `collect_local_jsonl()`（L11）— 遍历 list 型源配置，跳过 `enabled=False` 与不存在的路径。
- `_read_jsonl()`（L23）— 逐行解析；解析失败抛 ValueError（由上层转 source_error）；title 为空的行静默跳过。

## 输入 → 输出
输入：`data/inbox/*.jsonl`（配置 `sources.local_jsonl[]`）。输出：`RawSignal`（source_type 默认 `manual`，summary 回退 content 字段，payload 保存整行）。

## 门禁 / 安全
无本地门禁；进入外部 LLM 前由 llm_governance 按 manual 来源默认 internal 分类阻断（只有显式 public 化的新信号才可外发）。

## 备注 / 风险
一条坏行会让整个文件采集失败（降级为 source_error）；示例文件 `manual_signals.example.jsonl` 是 gitignore 白名单内的少数 data 文件之一。
