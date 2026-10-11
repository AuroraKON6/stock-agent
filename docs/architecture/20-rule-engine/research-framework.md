# [20-05] research_framework.py — 研究剧本路由

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 188 | models、config | pipeline（rule_analysis 阶段） |

## 职责
研究链最上游的路由器：按剧本（playbook）给每个信号挂研究支线（tracks）、研究问题、证据缺口上下文。这些 track id 后续成为案件 catalyst_path 和团队角色路由键。纯内存、无持久化。

## 关键构件
- `DEFAULT_PLAYBOOKS`（L11-63）— 5 条默认剧本：`source_health`（source_error）、`market_price_action`（price）、`event_impact`（news/filing/social/manual）、`risk_challenge`（P1+需复核）、`llm_deep_dive`（P2+）。每条带中文研究问题。
- `apply_research_framework()`（L66）— 挂 research_tracks/questions/context（含 `_evidence_gaps` L178：missing_url、human_review_required、source_error、topic_not_matched）。
- `_matches()`（L120-152）— **AND 语义**：所有已配置条件须全部通过（与 research_team 的 OR 相反，是有意差异）。
- `_load_playbooks()`（L110）— mode：default_plus_config / config_only / default_only。

## 输入 → 输出
输入：AnalyzedSignal + config。输出：填充 research_* 字段的信号。

## 门禁 / 安全
无；research_questions 是"待人工回答的问题"，不是结论。

## 备注 / 风险
剧本是系统研究行为的主要配置杠杆；新增剧本属于战略 v30 暂停清单里的"新增平行能力"，需先过五问（服务什么场景/改善什么指标/减少什么风险/需要什么真实输入/阻塞哪个门禁）。
