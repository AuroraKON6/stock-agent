# 人工复核与行情数据运营手册 v23

版本：23.0  
适用范围：`E:\stock agent` 的内部研究辅助、人工复核、行情质量控制和计划运行。  
边界：本系统不自动交易、不自动调仓、不自动分派人员、不自动批准治理例外、不自动发布研究结论。

## 1. 运营目标

系统不是一条“抓取后直接给结论”的直线。生产流程由可并行的证据采集、规则分析、LLM 研判和反证任务组成，并在行情、证据、身份、质控、合规和发布节点汇合。任一门禁不满足时，系统应生成待办、降级或阻断，而不是伪造成功。

```text
来源采集 ─┬─ 公告/新闻证据 ─┬─ 基本面分析 ─┐
          ├─ 行情多源校验 ─┼─ 市场背景 ───┼─ 综合研判 ─ 反证 ─ 质控 ─ 合规 ─ 人工批准
          └─ 来源健康检查 ─┴─ 异常与风险 ─┘       │
                                                    ├─ 补证/退回
                                                    └─ 监控/复盘
```

规则层负责可重复的筛选和门禁；DeepSeek V4 Pro 负责解释、提出假设和复核问题；真实人员负责身份受限的判断、批准和责任承担。

## 2. 角色与职责

| 角色 | 最低职责 | 系统权限 |
|---|---|---|
| `research_coordinator` | 分诊、确定范围、协调 SLA、提出分派候选 | 可协调和升级；不得代替质控/合规/发布批准 |
| `lead_analyst` | 核验材料、形成分析底稿、完成信号复核 | 可开始、补证、延期、完成复核 |
| `risk_reviewer` | 独立反证、证伪问题和 `risk_note:` | 不得与主分析师使用同一 actor |
| `quality_reviewer` | 检查证据、逻辑、引用和可复现性 | 仅真实且已授权人员可批准 |
| `compliance_officer` | 信息侧别、限制名单、利益冲突和披露检查 | 仅真实且已授权人员可放行 |
| `information_security_officer` | 外部 LLM 数据外发和安全例外复核 | 可处置治理记录；不能触发自动重送 |
| `publication_approver` | 批准内部发布范围和版本 | 不得由 Agent、service 或 bot 代替 |
| `data_steward` | 核准行情供应商、字段、授权范围和质量阈值 | 对供应商内部适用性留档，不改变交易所规则 |

`research_operations.enforce_roster=true` 时，actor 必须来自真实 active 名册，并通过角色、覆盖范围、信息侧别、限制名单和容量校验。候选分派始终为 `suggest_only`。

## 3. 每日运行顺序

### 3.1 交易日前

1. 数据管理员确认行情源授权、服务状态和交易日历。
2. 值班协调员检查真实名册、休假、限制名单和高优先级案件容量。
3. 信息安全或合规人员处理到期的 LLM 治理例外；未处理项保持阻断。
4. 运行 Doctor；出现 `FAIL` 时不得宣称无人值守生产就绪。

```powershell
python -m stock_agent doctor --config config/watchlist.local.json
python -m stock_agent research-ops-check --config config/watchlist.local.json
python -m stock_agent llm-governance-status --config config/watchlist.local.json
python -m stock_agent market-data-governance-status --config config/watchlist.local.json
```

### 3.2 计划任务主链

`daily` 的唯一生产顺序：

```text
market-cache-seed
-> agent-run
-> evidence-vault-status --require-ready（原件对象与事件链技术验真）
-> pi-handoff-run（仅治理批准的 public 信号；零条时零模型调用）
-> review-queue-sync
-> research-ops-plan
-> workspace-sync
-> production-pilot-sync
-> dashboard
-> alerts
```

`agent-run` 只对数据库判定为新增且通过治理门禁的候选调用 LLM。原件库技术验真失败时不得生成外部 handoff。
Python handoff 会再次执行公开分类、证据落地、行情门禁、密钥/路径/交易指令和完整性检查；零条时不调用 Pi。
隔离 Pi 只能消费最小公开 handoff，动态工具和 skills 保持关闭。`dashboard` 模式只刷新视图；`review` 模式只
汇总已完成人工复核，二者都不得重复调用 LLM。

人工运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode daily
```

计划摘要位于 `logs/scheduled/run_<时间戳>.summary.json`，逐步骤 stdout/stderr 位于同目录。任何步骤失败都必须保留已生成证据并进入人工排查，不能通过删除历史或重复采集掩盖。

### 3.3 盘中

1. 行情信号先通过新鲜度、交易时段、缓存来源、供应商核准和多源一致性门禁。
2. 规则层生成优先级、异常原因和人工复核标记。
3. 仅新信号和通过外发治理的数据进入 DeepSeek。
4. P0/P1、传闻、来源缺失、行情冲突和限制项始终进入人工队列。
5. 任何仓位、止损、止盈、下单或调仓表述只能作为待人工判断的信息，不得执行。

### 3.4 收盘后

1. 核对当日计划摘要、Dashboard、Notifications、行情治理和来源健康。
2. 完成或补证当日高优先级复核；未完成项按 SLA 老化并升级。
3. 周度运行汇总 completed review，形成阈值和数据源改进建议；建议不得自动改配置。

## 4. 复核队列

### 4.1 数据模型

- 输入：`data/review/pending_review_*.jsonl` 和 `completed_review_*.jsonl`，均视为不可变历史证据。
- 动作：`data/review/actions/review_actions_*.jsonl`，只追加，不覆盖。
- 投影视图：`reports/review_queue/latest.json`、`latest.md` 和时间戳版本。
- 主键：`signal_id`。历史重复行只在投影层去重，不删除原文件。
- SLA：从首次进入队列时间计算；`at_risk`、`overdue` 和升级级别是内部运营控制，不是监管期限。

状态迁移：

```text
pending -> in_review -> waiting_evidence -> in_review -> completed
   |           |               |
   +--------> deferred <-------+
   +--------> superseded
```

所有写动作均校验当前状态、目标状态、actor、角色和名册。非法跳转、service actor、未登记角色和交易类动作会失败关闭。

### 4.2 值班操作

```powershell
python -m stock_agent review-queue-sync --config config/watchlist.local.json
python -m stock_agent review-queue-status --config config/watchlist.local.json
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1
```

工作台入口：

- 案件运行时：`http://127.0.0.1:8767/?view=runtime`
- 信号复核：`http://127.0.0.1:8767/?view=review&review_status=outstanding`
- 补救队列：`http://127.0.0.1:8767/?view=remediation&status=open`

信号队列每页 25 条；点击信号直接定位到详情。Actor ID 不预填，避免把上一次操作者或 Agent 身份误作当前责任人。

### 4.3 积压处理顺序

1. 先 P0/P1，再 P2/P3；同优先级按升级级别和到期时间排序。
2. 先清理明显的测试/已被后续事件取代项，但必须用 `supersede_review` 留痕，不能删文件。
3. 对缺一手来源的项使用 `request_more_data`，附官方公告或监管文件后再判断。
4. 只有真实主分析师可完成分析复核；质控、合规和发布仍走各自独立门禁。
5. 每日核对 `outstanding`、`overdue`、`unassigned`、重复历史行和真实人工主体数。

## 5. 行情数据治理

### 5.1 每条行情必须保留

- `quote_at`：供应商声称的行情时间。
- `observed_at`：本系统实际观察时间。
- `cache_written_at`：缓存写入时间，仅缓存数据必填。
- `provider` 和 `source_url`：来源及取证入口。
- 字段完整性、供应商状态、缓存证据、交易时段和跨源比对结果。

不能用抓取完成时间冒充行情时间。缓存只能作为明确标注的降级证据，不能被包装成实时行情。

### 5.2 门禁状态

| 状态 | 含义 | LLM 处理 |
|---|---|---|
| `PASS` | 新鲜、供应商已内部核准、字段完整，且满足要求的跨源一致性 | 可按其他治理规则进入 |
| `DEGRADED` | 可供人工参考，但存在缓存、时效或一致性警告 | 默认不作为正常行情研判依据 |
| `FAIL/BLOCKED` | 陈旧、冲突、无可验证时间、未核准供应商或来源数不足 | 禁止进入正常 LLM 行情研判 |

生产配置要求至少两个独立行情来源形成共识。`approved_internal_research_providers` 只能由数据管理员在确认合同/授权、字段定义、延迟、故障响应、保存要求和外部模型使用范围后填写。Agent 不得自行把东方财富、AkShare、雪球、AllTick 或任何免费接口标成已核准。

上交所公开页面列明交易日和竞价时段；深交所 2026 年交易规则也列明 9:15-9:25、9:30-11:30、13:00-14:57、14:57-15:00 等时段。系统用这些公开时段判断上下文，但节假日、临时停市和规则变更必须由交易所日历或人工运营更新，不能只靠“周一至周五”的代码推断。

## 6. LLM 与外发治理

- 生产主模型：`deepseek-v4-pro`；`deepseek-v4-flash` 只作已配置回退。
- 主研判和多角色研判均有总墙钟截止时间，慢响应会失败关闭并产生降级记录。
- 外部 DeepSeek 仅处理 `public` 分类；内部研究、客户/个人信息、凭据、内幕信息、限制名单材料和提示注入在请求前阻断。
- 模型输出必须能回指实际输入证据；无法落地引用的结论不能成为通过项。
- 治理例外只能由真实 active 的合规或信息安全人员处置，处置记录不触发自动重送、交易或发布。
- Live UAT 使用独立数据库、review 目录、审计目录和报告目录，不读取或污染生产队列。

## 7. 故障与降级

| 故障 | 系统行为 | 人工动作 |
|---|---|---|
| 主行情源不可达 | 尝试备用源和带时间戳缓存；门禁重新评估 | 核对供应商状态和网络，不把缓存称为实时 |
| 行情跨源冲突 | 阻断正常行情 LLM 研判 | 核对交易所、复权、币种、单位和时间点 |
| LLM 超时/非法 JSON | 有界重试或记录失败，规则流程继续 | 查看模型运营报告，必要时人工分析 |
| 官方原件库技术验真失败 | 停止外部 handoff，保留对象、事件链和逐步日志 | 保全现场并核对对象哈希，不改写事件历史 |
| Pi handoff 失败 | 保留 Python 产物和逐步日志，计划任务失败 | 只重跑明确失败的受控 handoff/展示步骤，避免重复主流水线 LLM |
| 计划任务网络异常 | 子步骤隔离运行，记录标准代理兼容来源和日志 | 检查用户/任务环境变量；不得在仓库硬编码代理或密钥 |
| 名册缺人 | Research Ops 保持 `BLOCKED` | 由机构登记真实人员和授权，Agent 不得代填 |
| 工作台异常 | 回环健康检查失败，不对外监听 | 用严格 PID 脚本重启；不得按进程名批量结束 |

## 8. 当前生产就绪判定

技术健康和业务许可必须分开：

- 技术可运行：全量测试、固定版本 Pi、DeepSeek UAT、原件对象/事件链、计划脚本和工作台均通过最新验收。
- 业务启用：必须另行满足真实人工名册、行情供应商准入、官方原件覆盖、治理例外清零、复核积压、正式生产档案、合格影子样本和当前指纹人工批准。最新状态以 Doctor 和 Production Pilot 为准，不能从本手册推断。

因此当前允许“有人监督的内部研究试运行”，不允许宣称“无人值守机构生产就绪”，更不允许自动交易或自动发布。

## 9. 上线前人工清单

1. 机构负责人登记真实 `research_coordinator`、`lead_analyst`、`risk_reviewer`、`quality_reviewer`、`compliance_officer`、`information_security_officer`、`publication_approver` 和 `data_steward`。
2. 数据管理员核准至少两个独立行情源，并记录授权、字段、延迟、时区、复权和故障责任。
3. 合规/信息安全人员处理最新报告中的全部治理例外，不合规项应拒绝或改为隔离人工研究。
4. 协调员按优先级处理历史复核积压，使用状态迁移留痕，不删历史。
5. 数据、合规和信息安全人员验收外部不可变生产档案；本地内容寻址库不得冒充正式档案。
6. 运行 `evidence-vault-status --require-ready`、Doctor 和 Production Pilot；所有剩余 WARN/BLOCKED 必须有具名责任人、截止时间和书面接受记录。

## 10. 依据与适用说明

- 上交所股票投资页面：<https://one.sse.com.cn/onething/gptz/>
- 上交所 2026 年交易规则：<https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml>
- 深交所 2026 年交易规则 PDF：<https://docs.static.szse.cn/www/lawrules/rule/trade/W020260424690713155663.pdf>
- 证监会《证券基金经营机构信息技术管理办法》：<https://www.csrc.gov.cn/csrc/c106256/c1653896/content.shtml>
- 《证券期货业数据安全管理与保护指引》：<https://www.csrc.gov.cn/csrc/c101954/c6550642/6550642/files/%E9%99%84%E4%BB%B63%EF%BC%9A%E8%AF%81%E5%88%B8%E6%9C%9F%E8%B4%A7%E4%B8%9A%E6%95%B0%E6%8D%AE%E5%AE%89%E5%85%A8%E7%AE%A1%E7%90%86%E4%B8%8E%E4%BF%9D%E6%8A%A4%E6%8C%87%E5%BC%95.pdf>

上述监管文件用于工程控制设计参考；项目是否属于具体监管适用范围、保存期限和审批责任，应由具备权限的法律、合规和机构管理人员确认。本手册不构成法律意见。
