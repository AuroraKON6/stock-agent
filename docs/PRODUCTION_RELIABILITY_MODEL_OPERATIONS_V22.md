# 生产可靠性与模型质量运营 v22

## 1. 目的与边界

本版本把 LLM 从“能调用”升级为受交付时间、质量、证据和人工门禁约束的生产服务。系统只辅助内部研究，不自动交易、下单、调仓、分派人员、批准研究或对外发布。

业务上的正确结果不是“每个模型都跑完”，而是：在规定时间内交付可追溯的研究材料；无法完成的部分明确降级并进入人工队列；任何降级均不能放宽数据外发、证据引用、质量审核、合规审核和发布批准。

## 2. 真实日常运行顺序

| 顺序 | 责任域 | 系统动作 | 失败时动作 | 人工门禁 |
|---|---|---|---|---|
| 1 | 数据准备 | 行情缓存、公告/新闻/本地情报采集 | 使用已授权回退源或缓存；记录 source_error | 数据缺失、来源冲突需人工核验 |
| 2 | 规则初筛 | 去重、主题、优先级、异常、限制项检查 | 规则结果仍可交付，不依赖 LLM | P0/P1 和传闻必须人工复核 |
| 3 | 主研判 | DeepSeek V4 Pro 对新增高价值信号给出结构化判断 | 缺 key、超时、治理拒绝或截止时间不足时转人工 | 结论不得替代研究员判断 |
| 4 | 多角色挑战 | 基本面、资金、风险、反向观点并行挑战 | 临近截止时间先缩到最高优先级 1 条，再跳过 | challenge 不能替代独立人工 risk_note |
| 5 | 证据与治理 | 解析 evidence_refs、阻断无依据内容、记录外发审计 | fail closed；不因赶时间绕过治理 | 例外只能由登记的人类合规/信息安全角色处理 |
| 6 | 研究案件 | intake -> triage -> evidence -> analysis -> challenge | 证据不足可退回补证，保留上一验证状态 | 质量、合规、发布批准须角色分离 |
| 7 | 交付 | 日报、JSON、review、actions、Workspace、Dashboard、Alerts | 研究内容降级但必须产出错误说明和人工待办 | 不自动发布到外部渠道 |
| 8 | 复盘 | completed review、命中率、误报、阈值建议 | 只生成建议，不自动改配置 | 规则和阈值变更由人工批准 |

运行契约：

- 结构化行情快照的身份由证券代码和实际报价向量生成，不使用抓取时间、来源名、缓存年龄或展示标题；同一闭市报价从实时源切到缓存时不得重复入库、重复调用 LLM 或重复制造人工待办。价格、成交量、成交额、资金流等实质字段变化后仍形成新观察。
- `agent-run` 是每日链中唯一允许调用主研判和多角色研判的步骤；计划脚本不得再次调用 `export-review`。
- Python 先生成公开、最小化且带哈希的 handoff；仅在存在治理批准的公开信号时调用隔离的 `stock_agent_public_handoff`，零条时不得产生 OpenClaw 模型调用。
- Research Ops、Workspace、Dashboard、Alerts 由计划脚本在条件式 OpenClaw handoff 后各执行一次，步骤退出码、耗时、公开信号数、调用状态和错误写入同一计划摘要。

## 3. 服务等级目标

### 3.1 Agent 主链

- 目标：`agent-run <= 180s`。
- 主 LLM 预警：`signal_llm > 60s`。
- 多角色 LLM 预警：`role_llm > 90s`。
- 完整计划任务目标：从行情缓存开始，到 OpenClaw handoff、Research Ops、Workspace、Dashboard、Alerts 全部完成，`<= 300s`。
- SLO 违约不删除已生成产物，也不触发重复采集；状态写为 `BREACH`，由 doctor、Dashboard 和计划日志暴露。

### 3.2 质量目标

- 主研判成功率 `>= 95%`。
- 多角色研判成功率 `>= 90%`。
- 可用证据引用率（grounded + partial）`>= 95%`。
- 完全 grounded 比例 `>= 75%`；不足标记 `ATTENTION`，不伪装成完全核验。
- 成功 API 调用的 usage 元数据覆盖率 `100%`。
- `finish_reason` 只能为空或 `stop`；`length`、`content_filter`、`insufficient_system_resource` 均视为不可用结果，不进入正常研判。

## 4. 截止时间降级顺序

1. 主研判优先于多角色挑战。
2. 每次调用前按 P0、P1、P2、P3 和优先级分数重新排序。
3. 主研判剩余时间低于 105 秒时缩到 1 条；低于 75 秒时跳过。
4. 多角色剩余时间低于 120 秒时缩到 1 条；低于 75 秒时跳过。
5. 始终保留 45 秒给证据、治理、案件、报告和持久化。
6. 剩余时间低于 75 秒时不再发起模型重试。
7. 被缩减或跳过的信号设置 `needs_review=true`，记录明确原因，不执行空白兜底结论。

该顺序确保低价值的解释性工作先降级，规则分析、治理和可审计交付最后保留。

## 5. 遥测与成本

每次调用只记录：

- component、provider、model、request_id、attempt、item_count；
- success、duration_ms、error_class、精简 error；
- finish_reason、system_fingerprint；
- prompt、completion、total、cache hit、cache miss、reasoning token 数量。

严禁记录：

- API key、Authorization 值；
- 原始 prompt、外发 payload、模型回答正文；
- reasoning_content 或思维链；
- 未脱敏个人信息和非公开信息。

成本是按配置价格和 usage 的估算，不是账单。生产价格基准日期为 `2026-08-12`；价格变更后必须人工更新配置的 `effective_date` 和费率。缓存拆分缺失时，输入 token 按 cache miss 保守估价。

## 6. 产物与审计

- 最新模型运营报告：`reports/model_operations/latest.md`、`latest.json`。
- 按次报告：`reports/model_operations/model_ops_*.md/.json`。
- 追加式调用用量：`data/model_operations/usage/usage_YYYYMMDD.jsonl`。
- Agent 运行耗时：`logs/run_ops/latest.json`。
- 完整计划耗时：`logs/scheduled/run_*.summary.json`。
- 独立真实 UAT：`reports/uat/model_operations/`、`data/model_operations/uat_usage/`。

审计产物保留请求元数据和质量结论，不保留敏感内容。证券研究业务工作底稿和审批记录的保留周期由 `research_runtime.retention_years` 控制，最低 5 年。

## 7. 日常命令

```powershell
python -m stock_agent agent-run --config config/watchlist.local.json
python -m stock_agent model-ops-status --config config/watchlist.local.json
python -m stock_agent llm-governance-status --config config/watchlist.local.json
python -m stock_agent research-runtime-status
python -m stock_agent research-ops-check --config config/watchlist.local.json
python -m stock_agent doctor --config config/watchlist.local.json
```

完整日常计划链：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_daily_research.ps1 -Mode daily
```

真实模型隔离 UAT：

```powershell
python -m stock_agent agent-run --config config/llm_governance_live_test.example.json
```

## 8. 告警与处置

| 事件 | 状态 | 当班动作 | 禁止动作 |
|---|---|---|---|
| API 鉴权失败 | WARN/BREACH | 检查环境变量和供应商状态，保留规则报告 | 不把 key 写入配置或日志 |
| 429/供应商拥塞 | WARN | 按预算重试或走已授权 fallback | 不无限重试，不重复采集 |
| finish_reason 非 stop | BREACH | 丢弃该 LLM 结果，转人工 | 不把截断内容当完整结论 |
| usage 缺失 | ATTENTION | 保留研判，标记计量不可审计 | 不伪造 token 或成本 |
| grounding 不足 | ATTENTION/BREACH | 补一手证据、退回分析 | 不放宽 evidence_refs 校验 |
| 交付超 300 秒 | BREACH | 查逐步骤耗时，优先优化 OpenClaw/多角色阶段 | 不因超时绕过质量和合规 |
| 人工名册不完整 | BLOCKED | 登记真实人员与授权 | 不用 agent 或虚构姓名补位 |
| OpenClaw `plugins.allow` 为空 | WARN | 审计插件来源和 ID 后由管理员显式加入白名单 | 不由 Agent 自动信任第三方插件 |
| 相同闭市行情重复出现 | WARN/BREACH | 核对结构化行情身份、缓存来源和数据库重复数 | 不用关闭去重或提高 LLM 预算掩盖问题 |
| 人工复核积压 | WARN | 按 `signal_id` 去重并扣除 completed，分派真实复核人；关注文件碎片和无闭环状态 | 不按原始行数夸大队列，不删除历史审计文件 |

## 9. 当班验收顺序

1. 运行 `doctor`，确认技术项无 `FAIL`；`WARN` 必须有明确责任人和处置说明。
2. 运行 `research-ops-check`。若缺少真实人工角色，状态保持 `BLOCKED`，系统只能生成内部研究草稿。
3. 运行 `llm-governance-status`。`pending_exceptions` 只能由登记的人工合规或信息安全人员处置。
4. 执行完整日常计划链，要求 7 个步骤退出码均为 0、`errors=0`、`scheduled_slo_status=PASS`。
5. 立即重复一次相同闭市输入，要求 `inserted=0`、主 LLM 和多角色 LLM 调用均为 0。
6. 检查 Dashboard 同时显示本轮状态、滚动 token/成本、计划交付 SLO 和安全边界。
7. 检查 `data/model_operations/usage/*.jsonl` 不包含 API key、Authorization、原始 prompt、回答正文或 reasoning content。

## 10. 外部依据

- DeepSeek Chat Completions 定义 usage、cache token、reasoning token、finish_reason、JSON Output 和 user_id。
- DeepSeek 当前模型价格和并发上限必须按官方页面定期核对。
- 证券基金经营机构信息技术管理要求生产/测试隔离、日志审计、数据分类、供应商尽调和持续监测。
- 证券研究报告发布前需要独立质量控制，研究底稿与业务记录至少保存 5 年。

参考：

- https://api-docs.deepseek.com/api/create-chat-completion/
- https://api-docs.deepseek.com/quick_start/pricing/
- https://api-docs.deepseek.com/quick_start/rate_limit
- https://www.csrc.gov.cn/csrc/c106256/c1653896/content.shtml
- https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69856.html
