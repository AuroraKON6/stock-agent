# [50-02] evidence_vault_uat.py — 证据库实弹 UAT

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 50-gates | 200 | evidence_vault、config | cli（evidence-vault-uat --live） |

## 职责
在完全隔离的临时目录里做一次真实网络抓取验证，证明"受控 fetch 链路能工作"且"生产库零污染"。不污染、不留原件。

## 关键构件
- `run_evidence_vault_uat()`（L20）— 无 `--live` 直接拒绝（L41-44，fail-closed）。
- 生产指纹 `_production_inventory_fingerprint()`（L145）— 事件文件与索引的 SHA-256 + 计数 + 完整性；UAT 前后各取一次，**不一致即 FAIL**（证明零污染）。
- 隔离执行 — deepcopy 配置强制 enabled+allow_network_fetch；`_pin_operations_inputs_to_current_project()`（L160）把 roster 路径钉回当前项目；`tempfile.TemporaryDirectory` + `project_path_override` 重定向全部写路径。
- PASS 判定（L80）— document status=captured 且临时库 technical READY + integrity OK 且生产指纹前后不变；原始文档随临时目录删除（`raw_document_retained_after_uat: False`）。

## 输入 → 输出
写：`reports/evidence_vault/uat/latest.{json,md}` + 时间戳历史。

## 备注 / 风险
这是系统少数合法联网路径之一（另一个是 llm 连通性探测）；两者都不产生业务数据。
