# [50-04] market_data_governance.py — 行情数据门禁（v23.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 50-gates | 513 | market_resilience、provider_registry | pipeline（collect 后立即评估）、llm_agent/role_analyst（llm_allowed 查询）、doctor、dashboard、business_onboarding、production_pilot |

## 职责
price 类信号的放行裁决：新鲜度、交易时段、缓存证据、供应商内部核准、多源一致性。定位声明（L1-6）：**内部研究运营控制，非交易所监管时限**。

## 关键构件（_assess_one L208-321）
1. **供应商核准优先** — 未核准 → BLOCKED `provider_not_internally_attested`（L258）；核准来源动态决定（L34-48）：pilot enforce 时**只用** provider_registry（evidence_approved），否则静态配置与注册表并集；缓存件穿透后仍按 cached_from 追溯原始供应商。
2. 交易时段 `_market_session()`（L382）— Asia/Shanghai 固定 +8 硬编码 A 股时段（9:15-9:25 集合竞价 / 9:30-11:30 / 13:00-14:57 / 14:57-15:00 收盘竞价）；节假日周末 closed。**不处理临时停牌/半日市**。
3. 盘中 — 缺 quote 时间戳（含 raw.f124 兜底）→ BLOCKED；age > 120s → BLOCKED `stale_live_quote`。
4. 盘后 — 缺时间戳 → BLOCKED（缓存件单独 `cache_missing_quote_timestamp`）；>36h → BLOCKED；否则 DEGRADED `off_hours_snapshot_not_live`。
5. 缓存件非 BLOCK 一律 DEGRADED `cached_quote`（L284）。
6. 多源一致性 `_consistency()`（L360）— ≥2 源价差 (max-min)/median ≤0.5%，超限 → CONFLICT → BLOCKED；<2 源 UNVERIFIED（默认放行，`require_consensus_for_llm` 可收紧）。
7. **LLM 资格**（L295）— PASS 放行；DEGRADED 仅"盘后 + allow_degraded_offhours_llm"放行，否则 `human_review_required=True`；`market_signal_llm_allowed()`（L135）供 llm_agent/role_analyst 直接查询。

## 输入 → 输出
写：`reports/market_data_governance/latest.*`；评估结果进 run stats。

## 备注 / 风险
当前 FAIL：3 个价格信号全阻断（0 核准供应商 + 共识不足）——解除路径是 provider_registry 两家证据准入，不是调阈值。
