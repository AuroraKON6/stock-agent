# [40-05] research_git.py — Research-as-Git 草稿通道

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 40-research-core | 619 | models、config | pipeline（auto-stage，默认关）、cli（research-stage 等命令） |

## 职责
LLM 研究结论的"草稿 → 人工门禁"通道：自动暂存的草稿必须经真人 approve/reject 才能终结。独立于 workpapers 哈希链（普通 JSONL + 决定性 draft_id 幂等）。

## 关键构件
- 生命周期 — `stage_research_draft()`（L44，draft）→ `commit_research_draft()`（L84，committed，产生 8 位 commit_hash）→ `approve/reject`（L112/L127，**终态**）/ `defer`（L142，可回流 pending）。迁移约束 `_record_human_gate()`（L431）：approve/reject 要求 status ∈ {committed, deferred}；终态不可 commit。
- 事件重放 — `RESEARCH_EVENTS`（L33-39）5 种事件按日追加 `data/research_git/research_git_YYYYMMDD.jsonl`；`project_research_git_state()`（L176）重放重建状态与 history。**无哈希链**（与 workpapers 的强度差异，已在文档标注）。
- 安全过滤 `_reject_forbidden_text()`（L599）— 标题/论点/证据/问题/message 中检索执行类词（含中文"自动交易/自动下单/下单"），命中即 raise。
- 自动建档 `stage_llm_research_drafts()`（L260）— **默认关闭**（`research_git.enabled` 缺省 False）；从 llm_analysis 筛草稿（min_priority P2、须 human_decision_needed、confidence ≥0.7）；危险内容单独计数。

## 输入 → 输出
写：`data/research_git/*.jsonl`、`reports/research_git/latest.*`。

## 备注 / 风险
人工 gate 事件 `source: manual_research_gate`；同日多条决策依赖文件内顺序；战略 §18.2 将其与 monkeypatch 转发一并列入 OpenClaw 边界收敛对象。
