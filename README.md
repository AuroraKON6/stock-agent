# A股智能投研系统

可信发布基线用于回答“本次运行究竟使用哪份代码、配置、Pi 方法、模型和依赖”。按以下顺序生成
供应链控制、双版本 Python 环境证据和 canonical release manifest；这些命令不会采集行情、调用 LLM、
改写业务账本或执行任何审批：

```powershell
python -m stock_agent release-controls --config config/watchlist.local.json
python -m stock_agent python-env-audit --run-tests --require-pass
python -m stock_agent release-audit --config config/watchlist.local.json
python -m stock_agent release-audit --config config/watchlist.local.json --require-ready
```

报告写入 `reports/release_audit/latest.md` 和 `latest.json`。`--require-ready` 在缺少 Git HEAD、
工作树不干净、Python 依赖锁缺失、Pi 固定版本/隔离设置不一致，或 G0 的 secret scan、SBOM、
许可证报告、Python 3.11/3.12 环境审计未通过时返回非零退出码。许可证中的 copyleft、复合或未知表达式
必须由真实人工完成法律/合规复核，系统不得自动放行。技术发布 `READY` 也不代表业务获批；供应商、原件、
合规和真实人工 Go/No-Go 继续由各自门禁决定。完整操作见 `docs/G0_RELEASE_BASELINE_V31.md`，manifest
定义见 `docs/RELEASE_AUDIT_V31.md`。

G0.5 价值验证用于证明系统是否真的节省人工研究时间且不牺牲质量。它以 20-30 个真实 P0/P1 历史案件、
冻结 release manifest、同案人工/系统辅助观测和独立 A/B 盲评为证据，系统只给出继续、收窄或停止建议：

```powershell
python -m stock_agent value-validation-status --config config/watchlist.local.json
python -m stock_agent value-validation-status --config config/watchlist.local.json --require-ready
```

完整 SOP、角色分离、指标模板和停止条件见 `docs/VALUE_VALIDATION_V31.md`。没有真实案件、真实计时和独立
人工盲评时，正确状态是 `NOT_STARTED` 或 `BLOCKED`；mock 和示例数据不能算业务验收。

真实机构接入、内部控制证据、人员职责分离、供应商授权、生产档案、影子运行和指纹绑定的人工
Go/No-Go 见 `docs/BUSINESS_ONBOARDING_V29.md`。先生成不改变生产状态的接入包，再查看有序行动队列：

```powershell
python -m stock_agent business-onboarding-pack --config config/watchlist.local.json
python -m stock_agent control-evidence-status --config config/watchlist.local.json --require-ready
python -m stock_agent business-onboarding-sync --config config/watchlist.local.json
python -m stock_agent business-onboarding-status --config config/watchlist.local.json
```

内部合同、身份、资质和安全材料只保存在机构批准的外部档案系统。项目仅留存 SHA-256、外部档案 URI、
有效期和独立人工复核事件，不复制原件。测试通过只表示技术可运行；缺少真实人员、供应商合同、档案批准、
计划任务影子样本或六类独立人工批准时，业务必须保持 `BLOCKED`，生产试运行必须保持 `NO_GO`。

官方原始文档证据库、现实业务顺序、人工职责分离、失败处理和统一 Go/No-Go 见
`docs/BUSINESS_ENABLEMENT_V28.md`。快速检查：

```powershell
python -m stock_agent evidence-vault-status --config config/watchlist.local.json --require-ready
python -m stock_agent doctor --config config/watchlist.local.json
```

`READY` 只表示原件对象和哈希链技术完整。当前本地内容寻址库是审计影子库；未完成真实生产档案合同、
合规/信息安全复核和独立人工激活前，业务状态必须保持 `BLOCKED`。

结构化研究工作底稿、证据/推断分离、Pi/DeepSeek 方法留痕、不可变版本事件和人工门禁见
`docs/RESEARCH_WORKPAPERS_V27.md`。快速检查：

```powershell
python -m stock_agent research-workpaper-status --config config/watchlist.local.json
python -m stock_agent research-workpaper-verify --config config/watchlist.local.json --require-ready
```

`READY` 只表示技术底稿链完整；原始文件、真实人工挑战、质量/合规/发布批准和获批生产档案未齐备时，
业务状态必须保持 `BLOCKED`。

受控生产试运行、供应商证据准入、影子运行对账、事件升级和指纹绑定的人工 Go/No-Go 见
`docs/CONTROLLED_PRODUCTION_PILOT_V24.md`。当前状态必须分开检查：

```powershell
python -m stock_agent production-pilot-sync --config config/watchlist.local.json
python -m stock_agent production-pilot-status --config config/watchlist.local.json
```

`technical_readiness=READY` 只表示技术链可运行；只有真实名册、两家已完成证据准入的独立行情源、
清零的治理例外与逾期复核、足够的合格影子样本、六名不同人工批准和最终人工启用都通过后，
`go_live_decision` 才能为 `GO`。系统不会自动分派、批准、启用、发布或交易。

生产 Agent 基座已迁移为项目内固定版本的 Pi Agent。Python 仍是确定性的控制与治理平面，Pi 只承担
受控 LLM 推理。`agent-handoff` 从当轮 JSON 报告中只保留治理批准的 `public` 信号，并写入带来源哈希
的最小快照；公开信号为 0 时 `pi-handoff-run` 必须保持零模型调用。Pi 运行时关闭工具、会话、项目
信任、扩展和第三方包，模型输出不能执行分派、批准、发布、交易、下单或调仓。

```powershell
python -m stock_agent pi-runtime-status --config config/watchlist.local.json --require-ready
python -m stock_agent pi-runtime-uat --config config/watchlist.local.json --live
python -m stock_agent pi-handoff-run --config config/watchlist.local.json
```

`pi-runtime-uat --live` 会产生一次明确标记的合成公开输入模型调用；日常健康检查不会自动调用它。

生产研究方法采用“审核注册表 + 内容锁 + 按角色编译”的方式接入。Pi 的动态 skill 加载仍关闭，
Python 只把已固定来源、已审查权限且哈希匹配的方法文本加入对应角色提示词。检查命令：

```powershell
python -m stock_agent pi-skills-status --config config/watchlist.local.json --require-ready
python -m stock_agent pi-skills-uat --config config/watchlist.local.json --live
```

来源评估、许可证、被拒候选、角色映射、更新流程和完整性门禁见
`docs/PI_SKILLS_SUPPLY_CHAIN_V26.md`。

安装、隔离边界、供应链选择、计划任务切换和回滚步骤见 `docs/PI_AGENT_MIGRATION_V25.md`。
旧 `openclaw/` 目录和 v24 产物仅作只读审计与回滚证据，不再属于生产调用路径。

生产可靠性、模型质量 SLO、截止时间降级和 DeepSeek 用量审计见
`docs/PRODUCTION_RELIABILITY_MODEL_OPERATIONS_V22.md`。最新状态可运行：

```powershell
python -m stock_agent model-ops-status --config config/watchlist.local.json
```

人工复核队列、SLA、真实名册边界、行情新鲜度/缓存/多源一致性门禁和日常值班顺序见
`docs/HUMAN_REVIEW_MARKET_DATA_OPERATIONS_V23.md`。快速检查：

```powershell
python -m stock_agent review-queue-status --config config/watchlist.local.json
python -m stock_agent market-data-governance-status --config config/watchlist.local.json
python -m stock_agent doctor --config config/watchlist.local.json
```

这是一个面向一线分析、投研和投资岗位的本地可运行 MVP：把新闻、公告、手工情报、可选行情数据接成一条连续流水线，并输出日报、预警和复盘字段。

系统先保留人工把关，自动化高频重复动作：

- 明确长期跟踪主题、板块、股票、风险关键词和更新频率
- 接入新闻、公告、价格、社媒、行业数据等候选池
- 对候选信息做去重、打标签、情绪判断和优先级排序
- 输出 Markdown 日报、JSON 结构化结果和 JSONL 预警
- 记录复盘字段，用真实有效信号反推规则和阈值

## 快速开始

在 `E:\stock agent` 目录执行：

```powershell
python -m stock_agent run --config config/watchlist.example.json
```

生成结果会写入：

- `reports/daily/`：日报 Markdown
- `reports/json/`：结构化结果
- `reports/alerts/`：高优先级预警
- `data/stock_agent.sqlite3`：去重与历史信号库

查看已安装 skills：

```powershell
python -m stock_agent list-skills --skills-path "E:\stock agent\skills"
```

持续运行：

```powershell
python -m stock_agent watch --config config/watchlist.example.json --interval-minutes 30
```

## Research Runtime Workbench（本地人工复核工作台）

机构级案件的人工复核工作台是只监听回环地址的本地 HTTP 服务（默认 `127.0.0.1:8767`），不对外暴露。

启动（成功后会打开浏览器到运行视图）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1
```

不打开浏览器、改用其他配置或端口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1 -NoBrowser
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_research_workbench.ps1 -Config config\watchlist.local.json -Host 127.0.0.1 -Port 8767
```

访问地址：`http://127.0.0.1:8767/?view=runtime`

信号复核入口：`http://127.0.0.1:8767/?view=review&review_status=outstanding`。队列按 `signal_id`
去重投影并分页展示，点击条目会直接定位到人工详情；原始 JSONL 和动作历史保持只追加，不删除。

生产试运行只读入口：`http://127.0.0.1:8767/?view=pilot`。它展示技术/业务状态、供应商准入、
影子样本、人工容量、事件和全部阻断门禁；受保护动作仍只能由名册中的真实授权人工通过 CLI 留痕。

工作台同时显示 Research Operations 控制层：人员名册就绪状态、限制名单、内部 SLA、升级级别、下一责任角色和候选分派。候选仅供人工选择，系统不会自动分派。

```powershell
python -m stock_agent research-ops-plan --config config/watchlist.local.json
python -m stock_agent research-ops-check --config config/watchlist.local.json
```

`research-ops-check` 返回 `BLOCKED` 并不表示程序坏了；它表示真实协调员、主分析师、独立质控、合规或发布批准人尚未按资格和授权登记。禁止用 Agent 名称或虚构登记编号消除这个状态。详细运行制度见 `docs/COMMERCIAL_RESEARCH_OPERATING_MODEL.md`。

外部 DeepSeek 调用受 LLM Governance v21 控制：生产只允许经核验的 `public` 数据外发，内部研究、
客户/个人信息、凭据、内幕或限制名单材料和提示注入会在调用前阻断；模型结论必须引用实际输入证据。

```powershell
python -m stock_agent llm-governance-status --config config/watchlist.local.json
```

治理审计位于 `reports/llm_governance/` 和 `data/llm_governance/`。人工例外不能直接放行受限数据，
只能重新核实公开来源、最小化后形成新信号，或改为隔离环境人工研究。完整制度见
`docs/LLM_GOVERNANCE_OPERATING_MODEL.md`。

例外处置使用 `llm-governance-review`，并强制校验 `research_operations` 中的真实 active 人工名册和
`compliance_officer` / `information_security_officer` 角色；命令只留痕，不会自动重送或授权外发。

停止（重复执行无副作用）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_research_workbench.ps1
```

在桌面创建启动/停止快捷方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_research_workbench_shortcuts.ps1
```

PID 与元数据记录在 `logs/workbench/`，stdout/stderr 写入 `logs/workbench/workbench-<时间戳>.out.log` / `.err.log`。启动/停止脚本只操作自己记录的 PID，且必须通过 CIM 命令行校验（包含 `-m stock_agent review-workbench` 以及本项目配置、host、port）才会动作；PID 被复用或已失效时只清理元数据，绝不误杀无关进程。

## 推荐目录

```text
E:\stock agent
├─ .pi
├─ config
├─ data
│  └─ inbox
├─ openclaw                 # 历史只读兼容资料
├─ pi_runtime               # 固定版本、隔离的 Pi Agent 基座
├─ reports
├─ scripts
├─ skills
├─ stock_agent
└─ tests
```

## 配置入口

主配置是 `config/watchlist.example.json`。你可以复制成自己的配置，例如：

```powershell
Copy-Item config/watchlist.example.json config/watchlist.local.json
```

配置里最重要的字段：

- `topics`：长期跟踪主题、关键词、股票和风险词
- `sources.local_jsonl`：手工或其他工具落地的 JSONL 情报
- `sources.rss`：RSS/Atom 新闻源
- `sources.akshare`：可选 A 股行情适配器
- `priority_rules`：预警和优先处理规则
- `review`：复盘字段和有效性判断窗口
- `skills`：本地 skills 目录和流程角色映射

## Skills 接入方式

你后续把 skills 下载到：

```text
E:\stock agent\skills
```

旧 `skills/` 目录可继续用于离线发现和历史兼容；它不会被生产 Pi 自动加载。生产研究方法位于：

```text
.pi/skills/
```

这些 skill 只提供研究方法和输出约束，不授予文件、网络、Shell、写文件、交易、审批、发布、分派或
改配置权限。`config/pi_skills.json` 固定来源和角色，`config/pi_skills.lock.json` 固定内容哈希；生产 Pi
仍使用 `--no-skills --no-tools`。未经许可证、代码、数据边界和提示注入审查的第三方 package、extension
或 skill 不会进入生产运行时。

项目级代理说明和工具映射在：

```text
AGENTS.md
TOOLS.md
docs/execution-checklist.md
```

生产 skill 必须满足 v26 注册表、固定 commit、允许许可证、零权限和内容锁要求；仅有 `SKILL.md` 已不
足以进入生产。普通扫描结果不代表批准或启用。

## 数据输入格式

手工情报池使用 JSONL，每行一个对象：

```json
{"source":"manual","source_type":"news","title":"某公司披露大额订单","summary":"订单金额占去年营收 18%，需要核验交付周期。","url":"https://example.com","published_at":"2026-04-29T09:30:00+08:00"}
```

字段可以少填，系统会补默认值。建议至少提供：

- `source`
- `source_type`
- `title`
- `summary`
- `url`
- `published_at`

## 行情数据

本地配置默认启用 `sources.eastmoney`，使用东方财富公开行情接口抓取 A 股快照，不需要额外安装依赖。示例配置默认关闭，复制到本地配置后可以按需开启。

AkShare 仍作为可选备用数据源。`sources.akshare.enabled` 默认关闭。启用前安装可选依赖。AkShare 当前版本建议使用 Python 3.9+；如果仍用 Python 3.8，系统会保持本地/RSS/东方财富流程可用，并在启用 AkShare 时给出数据源异常提示。

```powershell
pip install -r requirements-optional.txt
```

然后在配置里打开：

```json
"eastmoney": {
  "enabled": true,
  "symbols": ["600519", "300750", "000001"]
}
```

东方财富行情适配器会输出最新价、涨跌幅、成交额和主力净流入。生产行情还必须通过
`market_data_governance` 的行情时间、观察时间、缓存证据、内部供应商核准和多源一致性门禁；
未核准、陈旧或冲突行情不会进入正常 LLM 行情研判。

## 人工把关点

日报里会单独列出 `needs_review=true` 的信号。通常包括：

- 重大监管、停牌、处罚、诉讼、业绩预告、减持、质押等关键词
- 影响多个主题或多个标的的交叉信号
- 来源缺失、URL 缺失或内容不足的信号
- 高优先级但情绪方向与资金/价格表现不一致的信号

## 48 小时落地路线

第 1 阶段：先跑通配置、手工情报池、日报和预警。

第 2 阶段：接入常用新闻/RSS、公告、行情和行业数据源。

第 3 阶段：把审计通过的可复用 skills 放入 `.pi/skills`，由 Python 控制平面按角色选择提示词；生产 Pi 仍保持无工具运行。

第 4 阶段：每个交易日复盘预警是否有效，调低噪声源权重，提高有效信号阈值。
