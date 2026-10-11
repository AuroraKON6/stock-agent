# 执行清单

## 1. 明确监控范围

输入：

- 主题、板块、标的池
- 正面关键词、负面关键词、风险关键词
- 更新频率和人工复核规则

输出：

- `config/watchlist.local.json`
- 主题到关键词的映射
- P0/P1/P2/P3 分层规则

人工确认：

- 是否有不该抓取或不该使用的数据源
- 是否有必须优先看的标的或板块
- 是否有需要屏蔽的低质量来源

## 2. 接入候选池

输入：

- `data/inbox/*.jsonl`
- RSS/Atom 源
- 可选 AkShare 行情
- 后续可接入公告、社媒、行业数据库

输出：

- 标准化 `RawSignal`
- 来源异常 `source_error`

人工确认：

- 数据源授权和使用频率
- 字段是否稳定
- 抓取失败是否要重试或降级

## 3. 去重、打标签、排序

判断规则：

- 标题、来源、URL、发布时间生成 `signal_id`
- 命中主题关键词则进入主题池
- 命中风险词提高优先级
- 命中传闻词或高优先级自动进入人工复核
- P0 只由硬风险关键词触发，避免普通高分误报

输出：

- `priority`
- `priority_score`
- `sentiment_score`
- `topics`
- `symbols`
- `needs_review`
- `reasons`

## 4. 输出日报、预警和复盘模板

输出：

- Markdown 日报: `reports/daily/`
- JSON 结构化结果: `reports/json/`
- JSONL 预警: `reports/alerts/`
- 复盘模板: JSON 里的 `review_template`

字段：

- `signal_id`
- `actual_price_move`
- `actual_news_followup`
- `decision_taken`
- `was_effective`
- `notes`

## 5. 复盘并反推规则

每 5 个交易日检查：

- 哪些 P1/P2 信号后来被验证
- 哪些来源噪声过高
- 哪些关键词误报
- 哪些风险词需要升权
- 哪些主题需要降频或拆分

调整：

- `topics[*].keywords`
- `topics[*].risk_keywords`
- `priority_rules`
- `sources[*].enabled`
- `sources[*].update_frequency`

