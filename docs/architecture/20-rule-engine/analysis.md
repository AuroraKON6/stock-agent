# [20-01] analysis.py — 规则评分引擎

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 20-rule-engine | 371 | text、models、config | pipeline、sources/eastmoney_filing（反向）、reports（min_priority_passes） |

## 职责
把 RawSignal 规则化为 AnalyzedSignal：主题/标的匹配、情绪、优先级定级、公告重要性与价格异常评分、人工复核标记。规则层职责是**筛选、约束和路由**，不是研究判断。

## 关键构件
- `analyze_signals()/analyze_one()`（L15/L19）— 归一化 → `_match_topics`（L128）→ `_score_sentiment`（L153，±0.25/词封顶 1）→ `_score_priority` → `_needs_review`（L354）。
- **`signal_id()`（L56，全系统最精巧设计）** — price 类走 `_price_snapshot_identity()`（L72）：symbol + 11 个行情字段（排除抓取时间/缓存元数据/供应商名）排序 JSON 后 sha1 前 16 位 → **跨供应商去重同一时刻行情**；其它类型 sha1(source|title|url|published_at)。
- `_score_priority()`（L170）— P0 词 +90；P1 词 +55；主题 +12/个；风险词 min(24×n, 48)；类型加分；|情感|≥0.5 +10；再加价格异常与公告分。定级：P0 命中→P0；≥55→P1；≥25→P2；否则 P3。
- `_score_filing_importance()`（L225）— high 类 +70/bonus 60；medium 45；low 10；**证据异常 → unknown +65 + bonus 30（证据不完整反而加分——"宁可多审不可漏报"）**；结果回写 payload。
- `_score_price_anomaly()`（L303）— |涨跌幅|≥3% 或主力净流入越 ±1 亿记异常；双异常+主题 +60（P1）；单异常 +30（P2）。
- `min_priority_passes()`（L124）— 供预警阈值与 LLM 候选过滤复用。

## 输入 → 输出
输入：RawSignal 列表 + config（topics/priority_rules）。输出：AnalyzedSignal。无写盘。

## 门禁 / 安全
`_needs_review` 触发条件：review_keywords 命中 / P0、P1 / 无 URL / source_error / 公告证据异常。

## 备注 / 风险
关键词是去标点子串匹配（见 text 卡），中文无分词；`signal_id` 算法一旦改动会破坏全部历史去重语义。
