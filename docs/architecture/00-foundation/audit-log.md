# [00-04] audit_log.py — 哈希链原语

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 00-foundation | 169 | 无 | research_workpapers、evidence_vault、pi_runtime、provider_registry、control_evidence、backup_restore、value_validation、production_pilot |

## 职责
全系统唯一的事实溯源原语：规范化 JSON、对象/文件哈希、追加式哈希链 JSONL 的读校验与写保护。所有"不可篡改历史"的承诺最终都落到这 169 行。

## 关键构件
- `canonical_json()`（L17）— sort_keys + 紧凑分隔符，哈希可复现性的前提。
- `object_hash()`（L21）/ `file_hash()`（L25）— sha256。
- `load_hash_chained_jsonl()`（L33）— 逐条重算 `event_hash`、校验 `previous_hash` 链接、event_id 唯一性；任何断链 → 后续全部丢弃，`integrity_status: FAIL`。
- `append_hash_chained_jsonl()`（L111）— **追加前先整链校验，链坏即 raise 拒写**（fail-closed，L115-118）；写后 `flush + fsync`；文件锁用 `O_CREAT|O_EXCL` + 5s 超时（`_append_lock` L149）。
- `atomic_write_text()`（L132）— 临时文件 + fsync + `os.replace` 原子落位。

## 输入 → 输出
输入：任意 event dict。输出：带 `event_id/sequence/previous_hash/event_hash` 的记录，落各模块自己的哈希链 JSONL（workpapers 的 events.jsonl、vault 的 vault_events.jsonl、pi 的 calls_*.jsonl、backup 的 events.jsonl、value_validation 的 events.jsonl 等）。

## 门禁 / 安全
链完整性是多个上层门禁的输入：链 FAIL → 底稿拒绝写入（AuditLogIntegrityError）、活跃供应商清空、审批/激活清空、影子证据作废。

## 备注 / 风险
本地哈希链只能证明"所存字节未被改动"，**不能证明没有整链回滚、截尾或离机篡改**——战略 v30 §19（P0 风险"审计截尾/整链回滚"）要求远端/WORM 锚点，当前未实现。锁文件异常退出会残留 `.lock`，下次追加 5 秒后 TimeoutError，需人工清理。
