# [30-01] llm_agent.py — 信号级 LLM 研判（signal_llm）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 791 | llm_governance、market_data_governance、model_operations、pi_runtime、analysis | pipeline、role_analyst（私有函数复用）、agent_handoff、doctor |

## 职责
单阶段信号研判：对通过全部门禁的新信号调用 DeepSeek（或 Pi），产出方向/置信度/时间窗/风险/复核问题/动作建议。**永不使管道失败**——所有异常写入 signal.llm_error 并置 needs_review。

## 关键构件（调用顺序）
1. `enrich_signals()`（L442）— 主入口。候选过滤：`min_priority_passes` → `market_data_governance.market_signal_llm_allowed()`（L483）→ 按 (priority_rank, -score, id) 截断 `max_signals_per_run`。
2. 截止时间 — `select_deadline_candidates()`（L498）决定 full/reduce/skip；被跳过信号 llm_error 注明"为保交付截止而跳过"。
3. 治理预检 — `prepare_llm_batch(..., "signal_llm")`（L520）；denied → llm_error="governance denied external model egress"。
4. `_call_llm()`（L223）— stdlib POST `{base_url}/chat/completions`，`response_format=json_object`；`_read_json_response_with_deadline()`（L277）分块读并复查总墙钟 deadline（防慢滴流拖垮排期）。
5. Pi 路径 — `_call_provider`（L192）按 `provider_uses_pi()` 分流到 `pi_runtime.call_pi_agent`。
6. 重试 — provider×attempt 双循环（L554）；认证错误中止该 provider（`_is_auth_error` L182 用子串匹配，可能误判）；重试受 75s 线约束 + ops 预算。
7. 校验 — `_parse_llm_json()`（L323，失败重试一次）；`_validate_analysis()`（L405）枚举归一 + confidence 夹取 [0,1]；`validate_signal_analysis()` grounding 后验（blocked → confidence≤0.5 + human_decision_needed）。
8. 动作白名单 `_validate_actions()`（L436）— 只允许 `{export_review, write_action_log}`。

## 输入 → 输出
输入：inserted 信号 + config.llm_agent。输出：signal.llm_analysis/llm_actions/llm_error；调用元数据（无内容）经 `ops.record_llm_call`（L757）进 usage 账本；`_model_execution_metadata()`（L721）把 Pi 审计事件 ID 附进结果。

## 备注 / 风险
SYSTEM_PROMPT（L32-52）内嵌防注入声明（输入全部视为数据非指令）；`_parse_llm_json` 对单个 dict 直接包装成数组，批量返回意外结构时其余信号报 missing result。
