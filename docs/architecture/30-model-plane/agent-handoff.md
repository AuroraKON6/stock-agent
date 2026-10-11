# [30-07] agent_handoff.py — Pi 公共摘要执行（schema 25.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 268 | openclaw_handoff、pi_runtime、llm_agent | cli（agent-handoff/pi-handoff-run）、scripts/run_daily_research.ps1、doctor、production_pilot |

## 职责
把当轮报告中治理批准的 public 信号组成最小化 handoff，条件性发起一次 Pi 公共摘要调用，并对结果做强校验。**零条 public 信号 = 零次模型调用**（条件调用原则）。

## 关键构件
- `build_agent_handoff()`（L48）— 复用 openclaw_handoff 的构造器（配置段映射兼容），叠加 `runtime="pi"`、`purpose="pi_public_research_summary_only"`，重算自校验 `payload_sha256`。
- `write_agent_handoff()`（L70）— 校验失败 raise；写 `reports/agent_handoff/latest.json` + 时间戳副本 + `data/pi_runtime/public_handoff/handoff.json`。
- `run_pi_public_handoff()`（L84）— `call_required = external_llm_call_required`；无合格信号则记录 skip_reason 直接返回；仅选 `provider_uses_pi` 且有 key 的 provider；`call_pi_agent(role_id="public_handoff")`，PUBLIC_HANDOFF_PROMPT（L30-45：只读公共摘要器、固定 schema、源文本视为不可信数据、禁止推断内部运营/路径/名册/凭据）。
- `validate_pi_public_summary()`（L145）— 字段 exact-match（缺一不可多一不可）；summary ≤2000 字符；signal_ids 与 handoff **集合全等**；`human_decision_required is True`；数组 ≤20 项每项 ≤1000 字符；全文扫 SECRET/LOCAL_PATH/**交易词（含英文 \bbuy\b/\bsell\b）**；任一失败 raise 不落盘。
- `check_latest_agent_handoff()`（L190）— 只读验证三级哈希链：最新报告 hash == handoff.source_report_sha256；result 的 payload/source 哈希与 handoff 一致；`called == call_required`、model_call_count 一致。

## 输入 → 输出
写：`reports/pi_agent/public_handoff/{latest,public_handoff_ts}.json`。

## 备注 / 风险
生产 Pi 摘要是日常路径中唯一的 regular 外部模型调用通道；战略 v30 要求未来把它并入 Model Gateway，Pi 从"摘要器"转为正式推理执行面。
