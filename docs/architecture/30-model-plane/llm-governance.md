# [30-02] llm_governance.py — LLM 外发治理（v28.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 1077 | evidence_vault、research_operations、audit_log | llm_agent、role_analyst、openclaw_handoff、doctor、cli（governance-review） |

## 职责
外部 LLM 数据外发的唯一裁决者：数据分类、敏感检测、最小化、证据引用后验、审计与例外处置。原则：**只放行 public；每条结论必须引用实际输入证据；例外只能由真人合规/信安处置且永不自动放行**。

## 关键构件
- 分类体系 — `CLASSIFICATIONS = (public, internal, confidential, restricted)`（L17），外部只允许 public。推导链（`_evaluate_preflight` L575）：显式标记 → 可信域名/来源+public 类型（cninfo/sse/szse/bse/csrc/sac/eastmoney）→ market_cache 需上游溯源完整 → **默认 internal**。敏感升级：密钥键名→restricted；PII/客户敏感→confidential；非公开/内幕词→restricted；限制名单命中→restricted。
- 阻断面 — 分类不符、`SECRET_PATTERNS`（L75）、个人数据、非公开信息、限制名单、`PROMPT_INJECTION_PATTERNS`（L86，中英双语）、公告/财报/财务类**必须证据库 RETAINED_VERIFIED**（L618-623）、最小化后全空。
- 最小化 `build_governed_prompt_payload()`（L261）— 字段白名单 + URL 剥查询串 + 6000 字符/50 项/深度 6 截断。
- 例外 `record_governance_exception_decision()`（L508）— 仅 compliance_officer/information_security_officer；三处置（deny_and_close / request_sanitized_reingest / confirm_false_positive）；actor 必须名册内 active 真人且角色匹配；处置不自动重送/授权/发布。
- 后验 `_validate_grounding()`（L658）— evidence_refs 归一后对照 allowed refs → grounded/partial/blocked；blocked → confidence≤0.5 + 强制人工复核。
- 审计 — 每次预检/后验写哈希记录到 `data/llm_governance/audit/audit_YYYYMMDD.jsonl`；`raw_document_sent_to_external_llm=False` 恒定。
- `validate_governance_policy()`（L171）— enforce 必须 true、仅 public、四检测全开、必须要求 evidence_refs、例外 human_only。

## 输入 → 输出
写：`data/llm_governance/exceptions/pending_*.jsonl`、audit 链、`reports/llm_governance/latest.*`。

## 备注 / 风险
enforce=false 时只记录不阻断（策略自检在启动时兜底）；当前 6 条历史例外等待真人处置——这是 Business Onboarding 阶段 5 的前置条件。
