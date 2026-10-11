# [30-03] model_operations.py — SLO / 截止时间 / 用量核算（v22.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 570 | run_ops（ops 协议） | llm_agent、role_analyst、pipeline、doctor、production_pilot |

## 职责
模型运营的三件事：①截止时间治理（保交付而降级，不硬扛超时）；②输出元数据脱敏（密钥/提示词/响应内容永不落盘）；③SLO 与成本核算。

## 关键构件
- `LLMResponse(str)`（L23）+ `sanitize_response_metadata()`（L76）— 仅保留 request_id/model/finish_reason/system_fingerprint/usage 六项，usage 只留 6 个非负整数字段。**API key、prompt、响应内容、推理文本一律不落盘**（doctor 有专项检查扫描敏感字段）。
- 截止时间策略（`model_operations_config` L49）— `agent_target_seconds=180`、`post_llm_reserve_seconds=45`、`main_min_start_remaining_seconds=75`、`main_reduce_below_seconds=105`（reduce 时最多 1 条）；role 侧 75/120；`retry_min_remaining_seconds=75`。
- `select_deadline_candidates()`（L101）— full / reduce / skip 三态。
- `deadline_timeout()`（L168）— min(请求超时, 剩余−45s)，≤0 放弃调用。
- SLO 快照 `build_model_operations_snapshot()`（L186）— 交付 ≤180s（BREACH）、main 成功率 ≥0.95（BREACH）、role ≥0.90（BREACH）、usable grounding ≥0.95（BREACH）、fully grounded ≥0.75（WARN）、usage 元数据覆盖 100%（WARN）、finish_reason 全 stop（BREACH）。
- `human_control` 块 — 四种自动行为硬编码 False（L271-277）。

## 输入 → 输出
写：`reports/model_operations/{latest,model_ops_ts}.{json,md}` + `data/model_operations/usage/usage_YYYYMMDD.jsonl`（每调用一行）；20 次滚动窗口汇总 + 可配置成本估算（cache hit/miss 分开计价）。

## 备注 / 风险
`_grounding_metrics()`（L423）：usable = (grounded+partial)/total；状态 IDLE 时（无调用）各项不计——production_pilot 要求"非 FAIL 且 0 breach 且有实际调用量"。
