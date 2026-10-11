# Business Enablement v28

## 1. 目的与边界

Business Enablement v28 将官方原始文档、研究底稿、外部 LLM、人工复核、供应商准入和生产试运行
组织成一套可审计的内部投研运行链。它不是证券业务许可、法律意见、正式档案系统认证、投资建议或交易系统。

系统能够自动采集公开资料、计算哈希、形成候选研判、提出反证问题和生成待办。系统不能自动完成：

- 数据供应商授权或数据使用许可；
- 真实人员登记、岗位授权和职责分离；
- 独立质量审核、合规放行和发布批准；
- 正式生产档案验收；
- 交易、下单、调仓、仓位或止损执行。

任何缺失的合同、人员、批准或档案证据都必须如实保持 `BLOCKED`，不得由 Agent、Pi、LLM、service、
system 或虚构记录消除。

## 2. 现实业务运行图

```text
来源发现
  -> 来源合法性与供应商准入
  -> 官方原件受控采集/人工导入
  -> SHA-256 内容寻址影子库 + 追加式事件链
  -> 规则去重、主题、优先级和行情治理
  -> 原件门禁与外发分类
  -> Pi/DeepSeek 受控研判
  -> Research Case 分流
       -> 基本面分析
       -> 事件与行业分析
       -> 行情背景与假设分析
       -> 补证分支
  -> Research Workpaper 汇总事实、观测、模型推断和人工判断
  -> 独立风险挑战
  -> 独立质量复核
  -> 合规复核
  -> 授权人工内部发布
  -> monitoring / 新证据触发退回
```

这不是单向直线。来源冲突、证据缺失、行情过期、模型证据引用失败、限制名单命中或人工挑战发现反证时，
案件必须退回 `evidence_collection` 或 `analysis`。基本面、事件、行业和行情分析可以并行，但只能在同一
案件底稿中汇合，不能绕过后续人工门禁。

## 3. 来源层级与原件标准

| 层级 | 典型来源 | 允许用途 | 不得冒充 |
| --- | --- | --- | --- |
| 官方一手 | 证监会、交易所、巨潮资讯、上市公司依法披露原件 | 事实核验、P0/P1 主证据、底稿原件 | 数据许可或研究结论批准 |
| 授权行情 | 已完成内部尽调和激活的行情供应商 | 经新鲜度、时段和多源一致性门禁后的市场观测 | 官方公告原件 |
| 二手聚合 | 东方财富、AkShare、雪球等 | 发现线索、交叉核对、辅助摘要 | 官方一手来源或已获内部授权 |
| 新闻/社媒 | 公开新闻、社媒、传闻 | 候选池和待核问题 | 可直接发布的事实结论 |
| 内部材料 | 访谈、客户、持仓、限制名单、内部判断 | 仅在内部受控环境人工处理 | 可发送给外部 DeepSeek 的公开数据 |

`official_evidence_vault` 默认只接受以下官方域名及其子域：`csrc.gov.cn`、`sac.net.cn`、
`sse.com.cn`、`szse.cn`、`cninfo.com.cn`、`bse.cn`。生产配置默认禁止网络自动抓取，优先使用
人工审阅后的本地原件导入。新增域名必须先完成来源、权限和安全评估，不能为了通过测试直接加入白名单。

公告、财报和财务类信号在 `enforce=true` 时必须留存可验证原件，才可进入外部 LLM。新闻类信息可以进入
候选研判，但仍需遵守公开分类、敏感信息、提示注入和证据引用门禁。

## 4. 官方原件库

### 4.1 技术模型

- `data/evidence_vault/events.jsonl` 是追加式哈希链事实源。
- `data/evidence_vault/objects/sha256/<前两位>/<sha256>` 保存已采集原件。
- `data/evidence_vault/quarantine/sha256/` 保存验证码、访问控制页或其他隔离对象。
- `data/evidence_vault/index.json` 和 `reports/evidence_vault/latest.*` 是可重建投影。
- 同一内容按 SHA-256 去重；新信号关联会追加事件，不覆盖原事件。
- 每次读取都会校验事件链、对象存在性、对象大小和对象哈希。
- 发送给外部 LLM 的只有公开元数据、原 URL、时间和内容定位哈希，原始文件字节不会外发。

本地内容寻址库是审计影子库，不是获批生产档案。只有外部不可变归档的合同/控制证据、信息安全复核、
合规复核和独立人工激活都与当前政策指纹匹配，`production_archive_ready` 才能为真。

### 4.2 人工导入

先由公开侧信息分析人员确认文件、精确官方 URL、标题和发布时间，再执行：

```powershell
python -m stock_agent evidence-vault-import `
  --config config/watchlist.local.json `
  --file <已核验本地文件> `
  --source-url <精确官方HTTPS原文URL> `
  --title <公告标题> `
  --published-at <ISO-8601发布时间> `
  --actor <真实名册中的采集actor> `
  --actor-role information_analyst `
  --signal-id <signal_id> `
  --issuer <发行人> `
  --symbol <证券代码>
```

人工导入会标记 `source_binding_status=operator_attested`，表示操作人确认了本地文件与官方 URL 的对应关系，
不是系统证明网页传输链。软链接、超限文件、不允许的域名、HTTP URL 和不允许的内容类型会被拒绝。

### 4.3 受控网络抓取

网络抓取有双重开关：配置中的 `allow_network_fetch=true` 与命令行 `--live`。只能抓取操作人已经审阅的
单一精确官方 URL，重定向后域名和 IP 会再次校验：

```powershell
python -m stock_agent evidence-vault-fetch `
  --config config/watchlist.local.json `
  --source-url <精确官方HTTPS原文URL> `
  --title <公告标题> `
  --published-at <ISO-8601发布时间> `
  --actor <真实名册中的采集actor> `
  --actor-role information_analyst `
  --signal-id <signal_id> `
  --live
```

验证码、访问拒绝或人机校验页会进入 quarantine，不能满足 LLM 或底稿原件门禁。系统不批量爬站，
不绕过访问控制，不声称免费来源拥有生产数据权利。

### 4.4 已有原件关联与校验

```powershell
python -m stock_agent evidence-vault-link --config config/watchlist.local.json --document-id <document_id> --signal-id <signal_id> --actor <actor> --actor-role information_analyst --notes <关联理由>
python -m stock_agent evidence-vault-status --config config/watchlist.local.json --require-ready
```

`--require-ready` 只要求技术完整性，适合值班和 CI。`--require-business-ready` 还要求生产档案获得真实人工控制，
在当前本地影子库配置下预期返回业务阻断。

## 5. 责任分工

| 角色 | 可执行 | 不可执行 |
| --- | --- | --- |
| information_analyst service/人工 | 采集公开原件、计算哈希、关联信号 | 批准档案、质量、合规、发布或交易 |
| lead_analyst | 分析、研究准备确认 | 自审质量或代替合规 |
| risk_reviewer | 独立反证和 `risk_note:` | 用一般 LLM 风险字段代替人工挑战 |
| quality_reviewer | 独立质量复核 | 与主分析师使用同一 actor |
| compliance_officer | 信息边界、披露和档案合规复核 | 代替信息安全或发布批准 |
| information_security_officer | 外部模型、对象库和归档安全复核 | 代替合规或数据授权 |
| data_steward | 数据供应商、口径和档案激活 | 自审其提交的尽调证据 |
| publication_approver | 最终内部发布与生产启用 | 绕过其他门禁或授权交易 |

正式生产档案复核必须由两名不同真实人工完成：

```powershell
python -m stock_agent evidence-vault-archive-review --config config/watchlist.local.json --decision approve --actor <合规人工actor> --actor-role compliance_officer --notes <结论> --evidence-ref <留存证据>
python -m stock_agent evidence-vault-archive-review --config config/watchlist.local.json --decision approve --actor <信息安全人工actor> --actor-role information_security_officer --notes <结论> --evidence-ref <留存证据>
python -m stock_agent evidence-vault-archive-activate --config config/watchlist.local.json --actor <独立激活人工actor> --actor-role data_steward --notes <激活记录>
```

本地影子后端会拒绝激活。配置、档案合同证据或控制参数变化会产生新指纹，使旧复核和激活自动失效。

## 6. 日常运行顺序

工作日 `daily` 的唯一数据平面顺序：

```text
market-cache-seed
-> agent-run
-> evidence-vault-status --require-ready
-> pi-handoff-run（仅合规公开信号；零条则零模型调用）
-> review-queue-sync
-> research-ops-plan
-> workspace-sync
-> production-pilot-sync
-> dashboard
-> alerts
```

原件库技术验真失败时，脚本不生成外部 handoff。原件库业务状态为 `BLOCKED` 时，系统仍可继续做受控影子研究，
但生产候选、发布批准和正式档案门禁不得通过。

工作日 08:45 `pilot` 只读取已经完成的 daily summary，执行哈希和顺序对账，不重新采集、不调用 LLM。
18:00 `dashboard` 只刷新运行视图。周度 `review` 只汇总真实 completed review 并输出建议，不自动改配置。
手工命令默认记录 `trigger_source=manual`，可用于技术验收但不得计入合格影子样本。只有由安装脚本创建并以
`-TriggerSource scheduled` 启动的计划任务摘要才具备影子样本资格；该字段仍是运行时控制证据，不替代
Windows 任务历史和机构运维记录。

## 7. 失败处理

| 失败 | 系统动作 | 人工动作 |
| --- | --- | --- |
| 官方 URL 非白名单/非 HTTPS | 拒绝采集 | 核对来源，必要时走域名审查 |
| DNS 指向私网/保留地址 | 拒绝抓取 | 安全人员核查，不绕过 SSRF 门禁 |
| 验证码或访问控制页 | quarantine | 人工从合法入口下载原件后导入 |
| 原件哈希或事件链不一致 | 技术 `FAIL`，停止 handoff | 保全现场、按事件流程调查，不改写历史 |
| 公告/财报无原件 | LLM 外发拒绝，案件补证 | 信息分析人员获取并关联原件 |
| 原件发布时间缺失 | 原件保留但 LLM 不合格 | 核对官方披露时间并重新形成受控记录 |
| 供应商未准入/行情冲突 | 行情信号阻断 | 数据责任人完成尽调或解决冲突 |
| 模型输出无证据引用 | 研判阻断并进入人工队列 | 分析师补证或重做判断 |
| 生产档案未批准 | 业务 `BLOCKED` | 采购、合规、信息安全和数据责任人完成真实控制 |
| 人员/批准缺失 | 候选 `NO_GO` | 登记真实在职人员和授权，不能虚构 |

任何失败都应保留已有事件和原始对象。不得删除日志、重复制造影子样本或降低门禁阈值来获得绿色状态。

## 8. 状态解释

- `technical_status=READY`：程序、对象和哈希链可验证。
- `business_status=BLOCKED`：仍缺真实原件、授权、人员、复核或生产档案条件。
- `candidate_readiness=READY_FOR_HUMAN_ACTIVATION`：所有候选门禁和独立人工批准与当前指纹一致。
- `go_live_decision=GO`：授权人工已经对当前候选执行最终激活，且所有门禁仍通过。

技术 `READY` 不能替代业务 `GO`。即使 `GO`，本系统也只允许内部研究运行，不授权自动交易或对外发布。

## 9. 核心审计产物

- `data/evidence_vault/events.jsonl`
- `data/evidence_vault/objects/sha256/`
- `data/evidence_vault/quarantine/sha256/`
- `reports/evidence_vault/latest.json`
- `reports/evidence_vault/latest.md`
- `data/research_workpapers/events.jsonl`
- `reports/research_workpapers/latest.json`
- `data/llm_governance/audit/`
- `reports/production_pilot/latest.json`
- `logs/scheduled/run_<时间戳>.summary.json`

## 10. 验收命令

```powershell
python -m unittest discover -s tests
python -m stock_agent evidence-vault-status --config config/watchlist.local.json --require-ready
python -m stock_agent research-workpaper-verify --config config/watchlist.local.json --require-ready
python -m stock_agent doctor --config config/watchlist.local.json
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode daily -SkipPi
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_research.ps1 -Mode pilot -SkipPi
```

`-SkipPi` 用于离线或不计费验收，只验证确定性控制链；它不证明真实模型业务研判完成。真实 Pi/DeepSeek
验收必须显式运行带 `--live` 的 UAT，并与业务数据、人工复核和档案验收分别报告。

官方来源传输也可用一次显式、隔离、不污染生产库存的真实 UAT 验证：

```powershell
python -m stock_agent evidence-vault-uat --config config/watchlist.local.json --source-url <精确官方HTTPS原文URL> --title <标题> --published-at <ISO时间> --actor <公开侧采集actor> --actor-role information_analyst --live
```

该命令下载到临时目录，校验域名、传输、对象和事件链后删除原始字节，只在
`reports/evidence_vault/uat/` 留下非敏感元数据，并验证生产原件库存指纹未变化。它只证明一次技术传输，
不等于原件已进入生产底稿、数据权利已核准或正式档案已批准。

## 11. 公开规则映射

以下链接用于工程控制映射，不构成合规认证：

- 中国证券业协会《发布证券研究报告执业规范》：合法来源、引用核实、工作底稿、质量与合规检查。
  https://www.sac.net.cn/sjb/zlgl/zlgz/202512/t20251231_69839.html
- 中国证券业协会《证券公司合规管理有效性评估指引》：研究业务档案、工作底稿和不少于五年的保存检查。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69856.html
- 中国证券业协会《证券公司信息隔离墙制度指引》：信息隔离、受控流转和职责边界。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69829.html
- 巨潮资讯：深圳证券交易所法定信息披露平台。
  https://www.cninfo.com.cn/?lang=zh
- 深圳证券交易所上市公司公告入口。
  https://www.szse.cn/www/disclosure/notice/company/index.html
- 上海证券交易所上市公司信息入口。
  https://one.sse.com.cn/list/
