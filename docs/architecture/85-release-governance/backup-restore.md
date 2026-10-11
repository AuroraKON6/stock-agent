# [85-04] backup_restore.py — 本机备份与隔离恢复演练（v32 新增，policy 32.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 85-release-governance | 1368 | audit_log、config | release_audit（READY 门禁）、cli（backup-create/verify、restore-drill、backup-restore-status）、dashboard |

## 职责
本机可验证的备份 → 校验 → 隔离恢复演练闭环。定位诚实：**本机 PASS 只证明"选定事实源可按 manifest/SHA-256/哈希链/SQLite 完整性在隔离目录恢复"，不代表生产灾备就绪**——`production_readiness` 被两个硬编码 blocker 永久压为 BLOCKED（L547-550），软件永不自证生产 DR。

## 关键构件
- 主操作 — `create_backup()`（L139，创建+载荷扫描+manifest）/ `verify_backup()`（校验+复扫+链重放）。
- 来源选择 `_discover_sources()`（L677）— 静态恢复文件（example 配置/pi_skills 锁/pi settings；**不含任何 `*.local.json`、名册、限制清单**）、`.pi/skills`、pi_runtime 方法包、`data/**/*.jsonl`（排除 compact/bundles/drills/uat/test）、`reports/actions/*.jsonl`、evidence vault 对象、SQLite（在线 backup + quick_check）。**sealed mapping 明确不在收集范围**（manifest 显式声明 excluded_sensitive_state，L262-267）。
- 脱敏配置 — 生成 `config/watchlist.restore.json`：`_sensitive_key()`（L1112）命中 api_key/secret/password/credential/access_token/bearer_token/private_key 的值替换 `${REDACTED}`；URL 用户信息剥离。
- **加密诚实性** — bundle 不加密（`encryption.status: NOT_CONFIGURED`）；防线是①来源凭据无关②脱敏③**载荷 secret scan（含 SQLite 单元格级 `_scan_sqlite` L1017）发现 BLOCKING 即终止创建**（L214-220），verify 复扫。残余风险：正则识别不了非标准格式密钥。
- 完整性 — manifest_id=内容哈希、manifest.sha256、fact_projection_hash；`_stable_copy()`（L759）拷贝前后双哈希源文件防竞态；staging→os.replace 原子落位；verify/drill 时**重放各 JSONL 哈希链**（`_jsonl_metadata` L876）。
- 恢复演练 `run_restore_drill()`（L388）— 只解压到 `data/backup_restore/drills/`（`_controlled_root` 限项目内），绝不覆盖源（`source_overwrite_performed: False`）；度量本机 RTO；RPO 标 NOT_MEASURABLE、`cross_file_atomic_snapshot: False`。
- 审计 — 哈希链事件 `data/backup_restore/events.jsonl`，actor=`system:backup_restore`，`business_state_changed: False`。
- 门禁 `build_backup_restore_status()`（L495）— technical PASS 需 enabled + PASS backup + PASS drill 且同 manifest_id + 源指纹未变 + 未超龄；`--require-ready` 只看 technical；`--require-production-ready` 实际不可达（需获批加密/KMS、异地不可变副本、全局 lease、可恢复 Git 基线、独立人工签字）。

## 输出
`data/backup_restore/`（bundles/drills/events）、`reports/backup_restore/latest.*`。
