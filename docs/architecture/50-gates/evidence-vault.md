# [50-01] evidence_vault.py — 官方原件证据库（v28.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 50-gates | 1431 | audit_log、research_operations | pipeline、llm_governance、research_workpapers、business_onboarding、production_pilot、doctor、cli |

## 职责
公告/财报/备案类官方文件的内容寻址留存库，是"先留存、后外发"门禁的执行点：**P0/P1 类信号没有 RETAINED_VERIFIED 原件，不得进入外部 LLM**。

## 关键构件
- 导入 `import_official_document()`（L154）— 拒绝符号链接；读取前后比对 size/mtime 防 TOCTOU。
- 受控抓取 `fetch_official_document()`（L204）— 须显式 allow_network_fetch；`_CheckedRedirectHandler`（L1031）每一跳重定向重新校验；Content-Length 预检 + 超限截断；transport 元数据仅 6 个白名单字段。
- 摄取 `ingest_official_document_bytes()`（L277）— 角色鉴权（information_analyst、public 侧）；`validate_official_url()`（L1000）：仅 HTTPS/443、禁 userinfo、**官方域名白名单**（bse/cninfo/csrc/sac/sse/szse）、可选公网 DNS 解析校验（防 SSRF）。
- 内容寻址 — `document_id = "document:sha256:{digest}"`；对象落 `data/evidence_vault/objects/sha256/{前2位}/{digest}`；`_atomic_write_binary_once()`（L1209）temp+fsync+写后重哈希+os.replace，已存在但哈希不符即报错。
- 隔离 — 内容类型白名单外、或 HTML 疑似挑战页（`_looks_like_challenge_page` L1200：安全/人机/滑块等中文正则）→ status=quarantined 入独立目录；**不尝试绕过访问控制**。
- 信号门禁 `evidence_status_for_signal()`（L734）— 五态：NOT_REQUIRED / RETAINED_VERIFIED / QUARANTINED / INCOMPLETE_METADATA / MISSING；**仅 RETAINED_VERIFIED → llm_eligible=True**。`attach_evidence_vault_status()`（L814）批量打标 + 自动补采 ≤3 条/轮。
- 生产档案 — `record_archive_review()`（L428）compliance+infosec 双复核（互不相同、互不与激活人重叠）；`activate_production_archive()`（L472）拦截四类 blocker（**本地影子存储不可当生产档案**、缺外部档案引用、不可变性未验证、复核不齐）；新复核使既有激活失效；`archive_policy_fingerprint`（L547）绑定策略配置。
- `verify_evidence_vault()`（L660）— 全量重哈希 → technical READY/FAIL + business BLOCKED/READY。

## 输入 → 输出
写：`data/evidence_vault/`（对象+隔离区+事件链）、`reports/evidence_vault/latest.*`。原始字节**永不**发送给 DeepSeek。

## 备注 / 风险
挑战页检测是前 1MB 启发式；全量重哈希在库大时慢（性能已记录）。
