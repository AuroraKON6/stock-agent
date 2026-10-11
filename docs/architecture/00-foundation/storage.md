# [00-05] storage.py — SQLite 信号持久层

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 00-foundation | 215 | models、config | pipeline、research_runtime、research_workpapers |

## 职责
信号唯一持久化点：单表 SQLite，按 `signal_id` 主键去重写入，供 runtime/底稿回填历史信号。

## 关键构件
- 公开类 `SignalStore`（L67）— 唯一持久化门面。
- Schema（L11-47）— 单表 `signals`，`signal_id` 主键，`published_at`/`priority` 索引，30 列覆盖全生命周期（research_tracks/role_analyses/evidence_chain/llm_* 等全部以 JSON 文本列存储）。
- `_migrate_add_columns()`（L78）— `PRAGMA table_info` + `ALTER TABLE` 自动补列（`_MIGRATE_COLUMNS` L49-64），无版本号的最小迁移。
- `upsert_many()`（L87）— 实为 **insert-or-skip**：先 `exists()` 主键检查，重复即丢弃不更新；返回 `(inserted, duplicates)`。
- `fetch_many()`（L103）— 500 ID/批 IN 查询；`_stored_signal_dict()`（L191）按 `_JSON_FIELD_MAP`（L170）还原 JSON 列，坏 JSON 容错为空 dict。

## 输入 → 输出
输入：`List[AnalyzedSignal]`。写入：`data/stock_agent.sqlite3`（路径可由 `config.storage.sqlite_path` 覆盖）。

## 门禁 / 安全
无门禁；去重语义由 signal_id 决定（价格信号是供应商无关快照身份，见 analysis 卡）。

## 备注 / 风险
- `exists`+`insert` 非事务原子（单进程够用）；重复信号永不更新，重跑不会修正历史。
- pipeline 层另有三个直接的 `UPDATE signals SET ...`（`_update_signal_llm/_role/_evidence_chain`，pipeline.py L383-475）绕过本类写回 LLM 结果——schema 变更时是第二处需要同步的点。
