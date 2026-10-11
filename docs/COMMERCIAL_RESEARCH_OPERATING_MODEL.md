# 机构级投研运行模型

## 适用边界

本项目是本地内部研究辅助系统，不是持牌证券研究报告发布平台，也不连接真实交易执行。
所有输出均为内部研究材料。自动交易、下单、调仓和自动对外发布永久禁用。

运行模型参考证券研究业务的职责分离、工作底稿、质量控制、合规审查和档案要求，
但不能替代持牌机构的制度、人员资质或法律意见。

## 商业运行顺序

| 阶段 | 主要责任人 | 必要输入 | 必须产出 | 不通过时去向 |
| --- | --- | --- | --- | --- |
| intake | research_coordinator | 新信号、覆盖范围、研究委托 | entity、topic、case_id、研究边界 | 留在 intake 补齐标的与主题 |
| triage | research_coordinator | 优先级、事件类型、时效 | P0-P3、研究路线、任务分工 | 退回 intake 或关闭噪声案件 |
| evidence_collection | information_analyst | 公告、财报、行情、新闻、调研记录 | 可追溯来源、工作底稿、未解决缺口 | 继续补证或切换备用数据源 |
| analysis | lead/fundamental/market analysts | 已核实证据 | 基准假设、影响路径、市场背景、模型或研究笔记 | 退回补证或重做分析 |
| challenge | risk_reviewer | 分析稿、假设、风险项 | 反证、证伪问题、下行情景 | 退回 evidence_collection 或 analysis |
| quality_review | independent quality_reviewer | 完整工作底稿与分析稿 | 独立质量审核意见 | 退回 analysis，审核人不得与主分析师相同 |
| compliance_review | compliance_officer | 质量通过稿、利益冲突和披露信息 | 合规清单、放行或阻断意见 | 留在合规门或退回补证 |
| human_approval | research_supervisor | 质量和合规均通过 | 人工内部发布批准 | 退回 quality_review 或关闭 |
| published | system after approval | 人工批准记录 | 内部研究版本、发布时间、审计记录 | 不允许系统自动对外发布 |
| monitoring | lead_analyst | 新公告、价格、财务和复盘数据 | 论点变化、触发条件、后续复盘 | 重大新证据触发重新补证 |
| closed | research_supervisor | 关闭理由 | 完整归档 | 新重大事实可人工 reopen |

## 并行工作流

进入 evidence_collection 后，案件不是固定串行执行。满足依赖后可以并行运行：

- source_verification: 核验来源、链接、发布时间和原始文件。
- fundamental_analysis: 拆解公告、财务、经营和事件影响。
- market_context: 核对量价、资金、行业和市场环境。
- hypothesis_analysis: 形成基准假设、关键假设和可证伪条件。
- independent_challenge: 独立提出反证、风险和缺失信息。

这些任务全部完成后，才能进入独立质量审核。LLM 可以执行分析工作流，不能通过质量、
合规或发布审批。

## 工作底稿

工作底稿引用使用 `kind:value` 形式。运行时支持：

- `official_filing:<url-or-id>`: 官方公告、监管文件或法定披露。
- `source_document:<url-or-id>`: 可追溯新闻、数据文档或第三方研究。
- `meeting_note:<path-or-id>`: 调研纪要或访谈笔记。
- `analysis_model:<path-or-id>`: 估值、财务或情景模型。
- `research_note:<path-or-id>`: 分析过程和结论草稿。
- `risk_note:<path-or-id>`: 独立风险挑战和证伪记录。
- `market_data:<path-or-id>`: 行情、成交、资金或行业数据快照。
- `conflict_disclosure:<path-or-id>`: 利益冲突与披露记录。

P0/P1 案件必须至少有一个 `official_filing` 或系统识别的官方一手来源。

## 受保护门禁

质量、合规和发布审批满足以下约束：

1. lead_analyst、quality_reviewer、compliance_officer、publication_approver 必须分别指派。
2. 四个受控角色不能使用同一 actor 标识。
3. system、LLM、Agent、DeepSeek、OpenClaw 和 bot 账号不能批准受保护门禁。
4. 合规放行必须逐项确认：公开信息、内幕信息、利益冲突、静默期和披露完整性。
5. 新证据请求会自动使质量、合规和发布批准失效，案件退回补证。
6. 发布批准只允许生成内部研究版本，外部发布和自动发布默认关闭。

## Research Operations Control v20

Research Runtime 描述案件“做到了哪一步”，Research Operations 描述“谁有资格做下一步、何时超时、是否允许接触该标的”。两者分开，避免把流程状态、身份权限和任务调度混成一条不可审计的脚本。

### 身份名册

- 生产名册：`config/research_roster.local.json`
- 模板：`config/research_roster.example.json`
- `principal_type=service` 只允许 `information_analyst`、`fundamental_analyst`、`market_analyst`、`risk_reviewer`。
- `research_coordinator`、`lead_analyst`、`quality_reviewer`、`compliance_officer`、`publication_approver` 和 `research_supervisor` 必须是真实人工。
- 主分析师与质量审核人必须填写已核验的证券分析师登记编号；合规和发布人员必须填写对应内部授权。
- 覆盖范围、个人限制标的、信息墙侧别和最大在办案件数都会参与分派校验。
- 当前本地名册只登记服务 Agent，没有虚构人工身份，因此业务就绪检查应当返回 `BLOCKED`。

### 限制名单与信息墙

观察/限制名单保存在 `config/research_restrictions.local.json`。每个生效条目必须包含 `reason` 和 `authorized_by`。命中 `restricted` 的案件停止公开侧研究并升级到 L3；命中 `observation` 的案件至少升级到 L1。系统不允许 Agent 自行修改名单或跨墙。

### SLA 与升级

配置中的 SLA 是内部运营目标，不是法律或监管时限。仅按配置工作日和工作时间累计：

| 等级 | 触发条件 | 处理要求 |
| --- | --- | --- |
| L0 | 正常、未接近 SLA | 按队列处理 |
| L1 | 达到 SLA 的 80% 或观察名单 | 协调员确认资源和信息边界 |
| L2 | 超过内部 SLA | 主管处理阻塞、补人或缩小范围 |
| L3 | P0/P1 未配齐人员，或命中限制名单 | 立即停止不合规动作并由人工主管/合规处理 |

运营快照使用原子写入，同时保留带时间戳的历史版本：

- `data/research_operations/latest.json`
- `reports/research_operations/latest.json`
- `reports/research_operations/latest.md`
- `reports/research_operations/operations_*.json/.md`

### 分派规则

`assignment_mode` 固定为 `suggest_only`。系统按角色、资格、授权、覆盖范围、限制名单和容量生成候选人，但不会自动接受任务。人工协调员或主管必须在工作台明确选择候选人并提交：

1. 先分派主分析师、独立质量审核人、合规人员和发布批准人。
2. 再按依赖分派 `source_verification`、基本面、市场、假设和独立挑战工作流。
3. 只有被分派的负责人可以完成对应工作流。
4. 独立挑战必须由 `risk_reviewer` 完成，并保留 `risk_note:`；LLM 生成的风险点不能替代挑战记录。
5. 所有授权结果、动作前后阶段和 principal 类型写入 `action_history`。

### 上线顺序

```powershell
# 1. 生成运营队列，不做自动分派
python -m stock_agent research-ops-plan --config config/watchlist.local.json

# 2. 校验真实人员是否齐备；BLOCKED 会返回退出码 2
python -m stock_agent research-ops-check --config config/watchlist.local.json

# 3. 人员齐备后启动本地工作台
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1
```

技术健康和业务上线是两个状态：测试、LLM、数据源和计划任务全部正常，只代表技术可运行；只有真实人员名册、职责分离、限制名单责任人和备份/访问控制就绪后，才能称为业务上线。

## 数据与审计

- 运行时快照：`data/research_runtime/runtime.json`
- 运行时事件：`data/research_runtime/events_YYYYMMDD.jsonl`
- 操作报告：`reports/research_runtime/latest.md`
- 结构化报告：`reports/research_runtime/latest.json`
- 全局审计事件：`data/events/events_YYYYMMDD.jsonl`

快照使用原子替换写入。人工动作、阶段变化和审批状态同时写入运行时事件账本与全局事件账本。
配置规定工作底稿与审批记录至少保留五年，但实际生产部署仍需配置备份、只读归档和访问控制。

## 常用命令

同步运行时：

```powershell
python -m stock_agent research-runtime-sync --config config/watchlist.local.json
```

查看案件状态：

```powershell
python -m stock_agent research-runtime-status
```

记录动作：

```powershell
python -m stock_agent research-runtime-action `
  --case-id CASE_ID `
  --action attach_workpaper `
  --actor ANALYST_NAME `
  --actor-role lead_analyst `
  --reference official_filing:https://example.test/filing.pdf
```

## 无人值守运行节奏

| 时段 | 计划任务 | 生产目的 | 禁止事项 |
| --- | --- | --- | --- |
| 工作日 08:30 | `StockAgent_DailyResearch` | 刷新行情缓存，执行一次 DeepSeek Agent 流程，生成公开最小化 handoff；仅有合规公开信号时调用隔离 OpenClaw Agent，再刷新工作区、看板和通知 | 零公开信号不得调用 OpenClaw；不得把内部运营状态送入外部模型 |
| 工作日 08:45 | `StockAgent_PilotReconcile` | 对已完成日报、公开 handoff、治理记录和输出做独立哈希对账 | 不采集、不调用 LLM、不补造影子样本 |
| 工作日 18:00 | `StockAgent_DailyDashboard` | 从持久化状态刷新 Research Runtime、工作区、看板和通知 | 不采集、不调用 LLM |
| 周六 09:00 | `StockAgent_WeeklyReview` | 汇总已完成复盘，并刷新运行时和管理视图 | 不重新采集、不自动修改配置 |

计划任务是否存在不是健康依据，Windows `LastTaskResult` 才是运行依据。
`python -m stock_agent doctor --config config/watchlist.local.json` 会检查每个任务的退出码、
最近计划摘要、LLM 连通性、Research Runtime 安全策略、Research Operations 业务就绪度、
OpenClaw 注册状态和最新产物。名册或限制名单损坏、自动分派越界属于 `FAIL`；必备人工岗位缺失、
案件未分派、SLA 超时或存在受限标的属于 `WARN`。即使其他技术项全部为 `OK`，业务就绪度显示
`BLOCKED` 时也不得视为正式上线，更不得用 Agent 或虚构人员消除该状态。
任何非零结果都必须先检查任务计划程序历史和 `logs/scheduled/`，再允许无人值守运行。

三个计划模式都会在展示层刷新前执行 `research-ops-plan`，只更新 SLA、升级和候选分派快照，不调用 LLM、不接受任务。

## 规则依据

- 中国证券业协会《发布证券研究报告执业规范》：工作底稿、发布前质量控制、独立质量审核和审核意见落实。
  https://www.sac.net.cn/sjb/flfg_949/zlgz/202005/t20200525_14376.html
- 中国证券业协会《证券分析师执业行为准则》：独立、客观、审慎，核实来源，避免内幕信息和利益冲突。
  https://www.sac.net.cn/sjb/zlgl/zlgz/202512/t20251231_69840.html
- 中国证券业协会《证券公司合规管理有效性评估指引》：内部审批、工作底稿和不少于五年的档案保存检查。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69856.html
- CFA Institute Standard V(A)：研究结论需要勤勉、独立、充分调查和合理依据。
  https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a
- CFA Institute Standard V(C)：保留足以支持分析、建议和研究沟通的记录。
  https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-c
- 中国证监会现行证券研究报告相关规则汇编：专门质量审核、合规审查、信息隔离、静默期、发布留痕和至少五年档案。
  https://www.csrc.gov.cn/csrc/c101950/c1048010/1048010/files/%E9%99%84%E4%BB%B61%EF%BC%9A%E3%80%8A%E5%85%B3%E4%BA%8E%E4%BF%AE%E6%94%B9%E9%83%A8%E5%88%86%E8%AF%81%E5%88%B8%E6%9C%9F%E8%A7%84%E8%8C%83%E6%80%A7%E6%96%87%E4%BB%B6%E7%9A%84%E5%86%B3%E5%AE%9A%E3%80%8B.pdf
- 中国证券业协会《证券公司信息隔离墙制度指引》：需知原则、跨墙审批、观察/限制名单和未发布研究报告保密。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69829.html
