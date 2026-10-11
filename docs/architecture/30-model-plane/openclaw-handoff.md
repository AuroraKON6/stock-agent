# [30-08] openclaw_handoff.py — 最小化公共 handoff 构造（schema 24.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 228 | llm_governance、market_data_governance、audit_log | agent_handoff（主要消费者）、cli（openclaw-handoff 遗留命令） |

## 职责
handoff 的实际构造与校验引擎：从当轮 JSON 报告筛出可外发信号、最小化、打哈希、验完整性。名为 openclaw，实为 runtime 无关的公共 handoff 核心（agent_handoff 叠加 Pi 语义后复用）。

## 关键构件
- 五道放行闸 `_handoff_allowed()`（L156-172）：①signal_llm 治理 classification=public 且 decision=approved；②sensitive_categories 与 prompt_injection 均空；③postflight ∈ {grounded, partial}；④price 信号须 market_data_governance llm_eligible；⑤已有非空 llm_analysis。
- 最小化 `_public_signal()`（L175）— 固定字段集 + llm_analysis 仅 10 个白名单键（L24-35）+ 压缩 governance 摘要；上限 `max_public_signals`（默认 10）；构造后再 `_contains_sensitive_or_unsafe_text()`（L199）全文扫 SECRET/TRADING/LOCAL_PATH。
- 哈希 — `source_report_sha256 = file_hash(最新报告)`（L81）；`payload_sha256` 为去除哈希键自身的对象哈希（自校验）。
- `validate_openclaw_handoff()`（L130）— 计数一致、哈希重算一致、全文安全扫描、逐条 governance 复核；payload 内嵌 9 项禁令（L89-99）与四个 `automatic_*_executed=False`。
- `write_openclaw_handoff()`（L110）— 校验失败 raise；写 `reports/handoff/` 与 `openclaw/public_handoff_workspace/handoff.json`。

## 输入 → 输出
输入：`reports/json` 最新报告。输出：handoff JSON。

## 备注 / 风险
`_public_signal` 的 url 未剥查询串（治理 payload 路径剥了，此路径没剥）——已被记录的收紧点。OpenClaw 主调度已退役，本模块因被 agent_handoff 复用而保留；战略 §18.2 要求历史命名集中到 compat 边界。
