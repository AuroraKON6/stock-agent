# [30-04] role_analyst.py — 多角色研究台（role_llm）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 765 | llm_agent（私有函数）、llm_governance、model_operations、market_data_governance、pi_runtime | pipeline |

## 职责
一次批量调用产出 4 个角色视角（基本面/资金流/风控/反方挑战）+ 综合结论。**注意：四角色来自同一个 DeepSeek 模型**——战略 v30 §19 明确这是"多角色虚假独立"风险，输出必须标记为同模型相关视角，不能冒充独立风险复核（真正的独立 challenge 是人工 risk_reviewer）。

## 关键构件
- `DEFAULT_ROLES`（L18-39）— fundamental_analyst、capital_flow_analyst、risk_guard、contrarian_challenger。
- `enrich_signals_with_role_analysts()`（L58）— 门禁流程与 llm_agent 平行：market_data（L95）→ deadline（L106）→ `prepare_llm_batch(..., "role_llm")`（L126），统计前缀 `role_llm_*`。
- `_build_role_prompt()`（L596）— 单 JSON prompt：roles + signals（治理 payload + 上阶段 rule_llm 结果）+ required_schema + guardrails（research_only / no_auto_trading / no_order_placement / no_position_rebalance，L652-658）。
- 响应映射 `_normalize_role_response_items()`（L407）— 五级容错：signal_id 精确 / 包裹展开 / dict 映射 / 单条 / **等长按行序对齐（positional，乱序会静默错位）**。
- 校验 — `_validate_role_views()`（L662，角色别名归一含中文）；`_validate_synthesis()`（L726，任一角色要求人工决策 → decision_gate 强制 human_review_required）；`validate_role_analyses()` 治理后验（缺角色 → blocked）。
- Pi 路径 — `call_pi_agent(role_id="research_desk")`。
- 配置段 — `config.multi_role_agent`（providers 未定义时继承 llm_agent 的 providers，覆盖 timeout/temperature/max_attempts；另有 base_url/model/json_mode 等键）。

## 输入 → 输出
输出：signal.role_analyses / role_synthesis（各附 model_execution 元数据）；pipeline 写回 SQLite。

## 备注 / 风险
JSON 解析失败**无重试**（与 signal_llm 不对称）；同模型多视角是 Model Gateway 收敛（战略 §18.2）的主要合并对象。
