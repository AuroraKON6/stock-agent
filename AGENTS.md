# A股智能投研系统 Agent 说明

本工作区用于持续运行 A 股投研情报流水线。Agent 的职责是辅助采集、整理、分析和输出研究材料，不直接给出自动交易指令。

## 核心目标

1. 围绕配置中的主题、标的、风险词持续更新候选信号。
2. 对候选信号做去重、标签、情绪、优先级和人工复核判断。
3. 输出日报、预警 JSONL、结构化 JSON 和复盘模板。
4. 根据复盘结果更新关键词、阈值、数据源权重和人工复核规则。

## 默认命令

离线测试优先使用隔离入口：

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
```

该入口在测试发现前禁止写入真实工作区（包括 SQLite）、外网连接及无保护子进程。
需要 PowerShell/进程生命周期的测试会被阻断，必须单独在受控环境验收，不得为追求全绿关闭守卫。
它是 Python 意外写入防护，不是原生代码安全沙箱。测试线程须各自建立临时路径上下文。
浏览器验收不得使用正式 local 配置创建任务；`tests/browser_board_isolated.cjs` 使用临时工作区，
由锁定 Python 启动真实服务，并通过独立浏览器页面验证 SSE。历史业务账本及事故证据不得清理。

所有命令使用锁定解释器 `.\.venv312\Scripts\python.exe`（Python 3.12 隔离环境）。不要用裸 `python`：
PATH 上的 python 可能解析到未受控环境（如 Anaconda 3.8），不属于已锁定的 3.11/3.12 验收基线。

执行一次：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent run --config config/watchlist.local.json
```

如果本地配置还不存在：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent init-config --target config/watchlist.local.json
```

持续运行：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent watch --config config/watchlist.local.json --interval-minutes 30
```

扫描 skills：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent list-skills --skills-path skills
```

检查可信发布基线：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent release-controls --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent python-env-audit --run-tests --require-pass
.\.venv312\Scripts\python.exe -m stock_agent release-audit --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent release-audit --config config/watchlist.local.json --require-ready
```

`release-controls` 默认离线生成脱敏 secret scan、CycloneDX SBOM 和许可证清单。只有显式传入
`--fetch-python-metadata` 时才允许访问 `pypi.org` 的精确版本元数据；扫描结果不得写出密钥值或上下文片段。
许可证 `REVIEW_REQUIRED` 必须保留为阻断项，Agent 不得代替法务或合规批准 copyleft、复合或未知许可证。

`python-env-audit` 不负责创建环境或安装依赖，只验证 `requirements.lock.txt` 与 Python 3.11/3.12
隔离环境、关键包导入和完整测试结果。未显式执行 `--run-tests`、缺少任一版本、锁不一致或测试失败时
不得报告 `PASS`。

`release-audit` 只能读取 Git、配置、锁文件、Pi 安全设置和已有 `latest` 投影，并写
`reports/release_audit/`。不得借此采集、调用 LLM、写业务事件、批准 provider、安装计划任务或改变
Go/No-Go。技术发布状态与业务启用状态必须分开报告。

检查运行租约、检查点账本和崩溃恢复状态（只读，不采集、不调用 LLM）：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent run-coordination-status --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent run-coordination-status --config config/watchlist.local.json --require-ready
```

`run`/`agent-run` 在 `run_coordination.enabled=true` 时按模式持有唯一租约，并在主要阶段写入
`data/run_coordination/checkpoints.jsonl`。同模式并发会在采集前失败；过期租约不得自动接管。只有确认
原进程已退出后，才允许由人工明确提供理由执行：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent run-recover-stale --config config/watchlist.local.json --mode agent-run --reason "确认上一个进程已退出且租约已过期"
```

该命令只归档过期租约并写审计事件，不会重跑、分派、交易、发布或修改配置。技术协调 `PASS` 不代表
生产启用或业务 Go。

执行本机备份与隔离恢复演练：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent backup-create --config config/watchlist.local.json --require-pass
.\.venv312\Scripts\python.exe -m stock_agent backup-verify --config config/watchlist.local.json --backup latest --require-pass
.\.venv312\Scripts\python.exe -m stock_agent restore-drill --config config/watchlist.local.json --backup latest --require-pass
.\.venv312\Scripts\python.exe -m stock_agent backup-restore-status --config config/watchlist.local.json --require-ready
```

本机 `PASS` 只证明选定事实源可按 manifest、SHA-256、JSONL/哈希链和 SQLite 完整性在隔离目录恢复，
不代表生产灾备就绪。原始 local 配置、名册、限制名单、凭据和 sealed mapping 不得进入未加密本机包；
没有获批加密/KMS、异地不可变副本、全局 lease、可恢复 Git 基线和独立人工签字时，生产状态必须保持
`BLOCKED`。不得把 ZIP 当作加密，也不得自动安装备份计划任务。完整操作见 `docs/BACKUP_RESTORE_V32.md`。

检查 G0.5 真实价值验证状态：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent value-validation-status --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent value-validation-status --config config/watchlist.local.json --require-ready
```

价值验证必须使用 20-30 个真实 P0/P1 历史案件、真实人工时间日志、冻结的 `READY` release manifest 和
独立 A/B 盲评。`data/value_validation/events.jsonl` 只追加，sealed mapping 只能由协调员在评分后揭盲。
测试、mock、示例 JSON、合成或重复案件不得计入业务样本；Agent 不得伪造人员、时间、评分、外发数量或
最终决定。系统建议不会自动批准、启用、发布或交易。完整操作见 `docs/VALUE_VALIDATION_V31.md`。

## 计划运行顺序

- `daily`: 行情缓存 -> 一次 `agent-run`（案件 -> 官方原件状态 -> Research Runtime -> Research Workpapers -> 决策/报告）-> `evidence-vault-status --require-ready` 技术验真 -> 生成公开最小化 handoff -> 仅在有合规公开信号时调用隔离 Pi Agent -> Review Queue -> Research Ops -> Workspace -> Production Pilot 快照 -> Business Onboarding 快照 -> Dashboard -> Alerts。原件账本或对象验真失败时不得生成外部 handoff；业务归档未获真实人工批准时保持 `BLOCKED`，但不冒充技术故障。
- `pilot`: 每个工作日 08:45 对已完成的 daily summary 做独立哈希对账 -> Dashboard -> Alerts；不得重复采集或调用 LLM。
- `dashboard`: 仅同步 Research Runtime、Workspace、Dashboard、Alerts，不重复调用 LLM。
- `review`: 汇总 completed review -> 同步 Research Runtime、Workspace、Dashboard、Alerts，不重复采集。
- `agent-run` 已为新增信号导出 review，计划脚本不得再调用 `export-review` 制造重复待办。
- `agent-handoff` 只能输出 `public` 且已通过 LLM Governance、证据落地和行情门禁的信号；`pi-handoff-run` 只能消费该快照。零条时必须跳过 Pi 模型调用；不得把内部名册、积压、异常、供应商审批、治理例外或本地路径发送给外部模型。

同步机构级投研案件运行时：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent research-runtime-sync --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent research-runtime-status
```

同步并验证结构化研究工作底稿：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent research-workpaper-sync --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent research-workpaper-status --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent research-workpaper-verify --config config/watchlist.local.json --require-ready
```

`data/research_workpapers/events.jsonl` 是追加式事实源，`latest.json` 和 Markdown 是可重建投影；
不得编辑、覆盖或删除事件历史。底稿必须分开记录来源陈述、数据观测、模型推断和人工判断。
P0/P1 必须有官方一手来源；聚合站点不得冒充官方来源；行情门禁未通过的数值不得进入批准链。
本地哈希链不是获批生产档案，`production_archive_approved` 只能基于真实人工审查和留存证据设置。

采集并验证官方原始文档：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent evidence-vault-import --config config/watchlist.local.json --file <本地原件> --source-url <官方HTTPS原文URL> --title <标题> --published-at <ISO时间> --actor <名册actor> --actor-role information_analyst --signal-id <signal_id>
.\.venv312\Scripts\python.exe -m stock_agent evidence-vault-status --config config/watchlist.local.json --require-ready
```

默认禁止网络自动采集。`evidence-vault-fetch` 必须同时由配置开启且显式带 `--live`，只能访问已审阅的
精确官方 URL。验证码和访问控制页必须隔离；不得绕过网站控制。公告、财报和财务类信号没有已留存原件时，
不得进入外部 LLM。原始文件字节不得发送给 DeepSeek。正式档案复核和激活只能由真实且相互独立的人工角色执行。

生成人员、SLA、限制名单和升级队列（只建议、不自动分派）：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent research-ops-plan --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent research-ops-check --config config/watchlist.local.json
```

`research-ops-check` 必须使用真实人工名册。不得为消除 BLOCKED 状态而虚构人员、登记编号或内部授权。

同步并检查去重后的人工复核队列（只建议、不自动分派）：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent review-queue-sync --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent review-queue-status --config config/watchlist.local.json
```

原始 pending/completed JSONL 和动作日志都是审计历史，不得删除或覆盖；重复项只能在以 `signal_id`
为主键的投影视图中合并。所有状态迁移必须校验真实 active actor、角色和当前状态。

检查行情新鲜度、交易时段、缓存证据、内部供应商核准和多源一致性：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent market-data-governance-status --config config/watchlist.local.json
```

陈旧、冲突、无可验证行情时间、未内部核准或来源数不足的价格信号不得进入正常 LLM 研判。
Agent 不得自行把任何免费或第三方行情源标记为已获内部授权。

检查外部 LLM 数据外发和证据引用：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent llm-governance-status --config config/watchlist.local.json
```

启用 `llm_governance.enforce` 后，外部 DeepSeek 只允许处理 `public` 数据。Agent 不得绕过或关闭
数据分类、密钥/个人信息/内幕信息检测、限制名单、提示注入检测和证据引用校验；不得自动批准治理例外。
`llm-governance-review` 只能由真实 active 人工合规或信息安全角色执行；Agent/service 账号不得代填 actor，
且处置记录不得触发自动重采、外发、交易或发布。

检查 Pi 研究方法供应链：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent pi-skills-status --config config/watchlist.local.json --require-ready
```

生产 Pi 始终使用 `--no-skills --no-tools`。`.pi/skills` 中的方法只能在通过
`config/pi_skills.json` 的固定来源、许可证、零权限和角色映射审查，并与
`config/pi_skills.lock.json` 的内容哈希一致后，由 Python 编译进固定角色提示词。Agent 不得自动更新
来源 commit、重写 lock、安装远程 skill、启用动态发现，或把普通 `skills/` 扫描结果视为生产批准。
任何方法、来源、权限、角色映射或 lock 变化都必须使旧生产候选指纹失效。

检查真实机构商业接入顺序和内部控制证据：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent business-onboarding-pack --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent control-evidence-status --config config/watchlist.local.json --require-ready
.\.venv312\Scripts\python.exe -m stock_agent business-onboarding-sync --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent business-onboarding-status --config config/watchlist.local.json
```

原始合同、身份、资质、培训和安全文件只能保存在获批的外部档案系统。项目只登记外部档案 URI、SHA-256、
有效期和人工复核事件，不复制原件。登记人与复核人必须是名册中的真实 active 人工，且复核人不得是登记人或
证据主体；多角色复核必须使用不同人工。证据过期、撤销、哈希链损坏或主体不匹配时必须立即失效。
Business Onboarding 的八阶段必须按 `governance_scope -> human_roster -> provider_authorization ->
official_evidence_backfill -> governance_remediation -> production_archive -> shadow_readiness -> go_no_go`
执行。Agent 不得新增真实人员、批准供应商、填写授权、安装计划任务或把 `BLOCKED/NO_GO` 改写为上线。
完整操作见 `docs/BUSINESS_ONBOARDING_V29.md`。

检查受控生产试运行和 Go/No-Go：

```powershell
.\.venv312\Scripts\python.exe -m stock_agent production-pilot-sync --config config/watchlist.local.json
.\.venv312\Scripts\python.exe -m stock_agent production-pilot-status --config config/watchlist.local.json
```

技术健康、生产候选就绪和业务启用必须分别报告。供应商尽调、合规复核、信息安全复核、生产门禁批准与启用
只能由真实 active 人工执行；Pi、LLM、Agent、service、system 或 bot 不得代填。任何配置、名册或供应商
状态变化必须使旧候选指纹的批准失效。影子运行账本只能记录完成后的真实计划任务，不得为达到数量重复执行或伪造。

## 机构级案件顺序

每个 Research Case 使用以下运行顺序，并允许补证和退回分支：

1. `intake`：确认覆盖标的、主题和内部研究边界。
2. `triage`：确定优先级、研究范围和并行任务。
3. `evidence_collection`：核验一手来源并形成工作底稿；P0/P1 必须有官方一手来源。
4. `analysis`：基本面、事件、市场背景和假设分析可以并行，但必须分别留存工作底稿。
5. `challenge`：由独立风险复核角色提出反证和证伪问题。
6. `quality_review`：由独立质量审核人批准或退回。
7. `compliance_review`：核对公开信息、内幕信息、利益冲突、静默期和披露完整性。
8. `human_approval`：由授权人工批准内部发布。
9. `published`、`monitoring`、`closed`：发布后持续跟踪，重大新证据可退回补证。

LLM、Pi、Agent、system 或 bot 账号不得执行质量批准、合规放行或发布批准。主分析师、质量审核人、合规人员和发布批准人必须使用不同 actor 标识。
任何受保护门禁动作还必须通过对应案件的结构化底稿验证；模型生成的反证问题不能替代已分派
`risk_reviewer` 的独立人工挑战。完整操作模型见 `docs/RESEARCH_WORKPAPERS_V27.md`。

启用 `research_operations.enforce_roster` 后，每次运行时写动作都必须通过身份、角色、覆盖范围、信息侧别、限制名单和容量校验。独立挑战必须由已分派的 `risk_reviewer` 留存 `risk_note:`，不能由一般 LLM 风险字段自动通过。

运行时文件：

- `data/research_runtime/runtime.json`
- `reports/research_runtime/latest.md`
- `reports/research_runtime/latest.json`

## 人工确认边界

以下内容必须交给人工确认：

- P0/P1 信号
- 传闻、匿名、未证实、来源缺失的信息
- 会影响仓位、止损、止盈、调仓的建议
- 数据源异常或字段缺失导致的判断
- 与公告、财报、监管文件不一致的新闻或社媒信号

## 输出规范

- 日报放在 `reports/daily/`
- 结构化结果放在 `reports/json/`
- 预警放在 `reports/alerts/`
- 原始手工情报放在 `data/inbox/*.jsonl`
- 复盘字段使用 JSON 结果中的 `review_template`

## 免责声明

本系统输出仅作为研究辅助材料，不构成投资建议。任何交易、仓位或风控动作都需要人工最终判断。
