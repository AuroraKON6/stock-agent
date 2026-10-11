# 实施基线盘点（Z-001）

版本：2026-09-18。执行者：Zcode 实施代理。依据：`docs/INDUSTRIAL_RESEARCH_PLATFORM_PLAN_20260918.md`、`docs/ZCODE_EXECUTION_BRIEF_20260918.md`、根目录 `AGENTS.md`。

性质声明：本文是只读盘点结果。除两条允许的离线审计命令刷新了自身报告投影（`reports/release/python_environment_latest.md`、`reports/run_coordination/latest.json`，副作用均为 report files only）外，本盘点未采集数据、未调用外部模型、未写业务事件、未改变任何 Go/No-Go。文中"最近证据"引用的是磁盘已有报告快照，不表示本次重新联网验证。

## 1. Git 状态

| 项 | 值 | 证据 |
|---|---|---|
| HEAD | 不存在 | `git rev-parse --verify HEAD` → `fatal: Needed a single revision` |
| 分支 | `main`（未出生分支） | `git symbolic-ref HEAD` → `refs/heads/main` |
| 已暂存 | 296 个文件 | `git diff --cached --name-only \| wc -l` |
| 未暂存修改 | 0 个 | `git diff --name-only \| wc -l` |
| 未跟踪 | 6 个 | `git ls-files --others --exclude-standard` |

未跟踪文件清单（均为规划/架构产物，无敏感内容）：`docs/INDUSTRIAL_RESEARCH_PLATFORM_PLAN_20260918.md`、`docs/ZCODE_EXECUTION_BRIEF_20260918.md`、`docs/architecture/MASTER_DIAGRAM_ARCHIFY.html`、`docs/architecture/MASTER_DIAGRAM_ARCHIFY.visual-check.json`、`docs/architecture/archify/master-diagram.architecture.json`、`.v2c/plugin_root`。

结论：无 HEAD 即无可引用发布基线，与 `reports/release_audit/latest.md`（2026-09-17，Technical BLOCKED）一致。296 个暂存文件是用户既有工作，本盘点未 reset、未提交、未改动。首提交需用户终端自行执行（mimosa 拦截 AI 会话内提交），属人工事项。

## 2. 运行环境、解释器与锁

| 组件 | 实际版本 | 证据 |
|---|---|---|
| `.venv311` | Python 3.11.15 | `--version` 实测 |
| `.venv312` | Python 3.12.13 | `--version` 实测 |
| PATH `python` | **3.8.5（Anaconda）** | `which python` → `/e/anaconda/python` |
| Node | v24.16.0 | `node --version` 实测 |
| Pi | `@earendil-works/pi-coding-agent@0.84.2` | `pi_runtime/package.json` 与 `package-lock.json` 一致 |

锁一致性：`requirements.lock.txt` SHA-256 `41abe53a…e25791`、31 个锁定包；本次执行 `python-env-audit`（未带 `--run-tests`，如实报 BLOCKED）显示 3.11.15 与 3.12.13 均 `lock=match`。

入口解释器分歧（Z-002 修复项）：

- `AGENTS.md` 与规划文档的示例命令用裸 `python -m stock_agent`，落到 PATH 时即 Anaconda 3.8.5，不在锁定环境内。
- `scripts/run_once.ps1:7` 用裸 `python` 且指向 `config/watchlist.example.json`（example 配置）。
- `scripts/start_agent.ps1:33` 用 `Get-Command python` 解析解释器（同样落在 PATH python）。
- `scripts/run_daily_research.ps1` 各步骤均用裸 `python`。
- `reports/health/latest.md`（2026-09-17）记录 python_version 3.12.13（当时入口为 3.12）；规划 §2 指出 release-audit 解释器为 3.8.5——与本次 PATH 实测一致，入口未统一。

## 3. 数据源三维度（enabled / 实际可用 / 许可与内部批准）

本次未做任何联网探测。"实际可用"引用最近报告快照。

| 源 | enabled | 实际可用（最近证据） | 许可/内部批准 |
|---|---|---|---|
| local_jsonl 手工情报池 | true | 文件存在，health 2026-09-17 OK | 指向 `data/inbox/manual_signals.example.jsonl`，抽查内容为 `example.com` 合成样例（AI 服务器订单、新能源业绩预告等示例情报），**是开发样例不是真实情报** |
| eastmoney_filing | true | health 2026-09-17 OK，source_health score=97 | 供应商注册表 0 provider / 0 active（`reports/production_pilot/provider_registry_latest.md`，2026-09-06：none） |
| eastmoney（价格） | true | 同上 | 同上；且 `approved_internal_research_providers=[]`，行情治理 `require_authorized_provider_for_llm=true` |
| rss | false | — | 未启用 |
| filing_jsonl | false | — | 未启用 |
| alltick / free_quote / pysnowball / akshare | false | — | 未启用 |

行情治理最新投影（`reports/market_data_governance/latest.md`，2026-08-25）：status FAIL；价格 3 条全部 blocked；llm_eligible=0；approved providers: none；原因计数 `insufficient_cross_source_consensus: 3`、`provider_not_internally_attested: 3`。即：即使 eastmoney 可抓到数据，未经内部核准且无第二来源共识，价格信号被禁止进入 LLM 研判。

LLM：配置为 Pi 运行时 + `deepseek_primary`（base_url `https://api.deepseek.com/v1`，model `deepseek-v4-pro`，密钥取自 `DEEPSEEK_API_KEY` 环境变量）。当前 shell 中该变量已设置（长度 35，值不显示），但 health 2026-09-17 记录连通性 401 `credential_or_token_error`——即密钥存在但无效或已过期。fallback provider 已 `enabled=false`。

## 4. 模块四分类

`stock_agent/` 共 57 个 `.py` 模块、10 个源适配器；`tests/` 共 70 个测试文件。

### 4.1 可复用（有实现、有测试、被引用）

- 主流程与存储：`pipeline.py`（run/agent-run 分阶段：collect → rule_analysis → official_evidence_vault → storage → source_health…，接入 run_coordination 检查点）、`storage.py`（SQLite signals 表）、`models.py`、`config.py`、`cli.py`、`__main__.py`。
- 采集适配器（`sources/`）：`local_jsonl`、`eastmoney_adapter`、`eastmoney_filing_adapter`、`rss`、`akshare_adapter`、`alltick_adapter`、`free_quote_adapter`、`pysnowbell_adapter`、`filing_jsonl`——可复用，但尚无规划 §6.1 要求的统一 observation 契约（W04 工作量）。
- 治理域：`evidence_vault`（+`evidence_vault_uat`）、`research_workpapers`、`research_runtime`、`research_operations`、`research_cases`、`review_queue`、`llm_governance`、`market_data_governance`、`market_resilience`、`provider_registry`、`production_pilot`、`business_onboarding`、`control_evidence`、`value_validation`、`backup_restore`。
- 模型与运行：`pi_runtime`（单轮隔离执行器，禁 tools/extensions/skills/session/context）、`llm_agent`（17 处引用）、`role_analyst`、`model_operations`、`run_ops`、`run_coordination`、`source_health`、`source_remediation`。
- 输出与界面：`dashboard`（静态投影生成）、`review_workbench`（HTTP 工作台）、`reports`、`alerts`、`review`、`review_priority`、`decisions`、`workspace_inbox`、`doctor`、`python_environment`、`release_audit`、`release_controls`、`pi_skills`、`agent_handoff`、`skills`、`team_ops`、`research_git`、`research_framework`、`research_team`、`brokerage_architecture`（17 处引用）。

### 4.2 未完成（存在但未接线）

- `run_manifest.py`：509 行完整实现（含完整性/冲突错误类、workspace 路径与符号链接检查），但**全库零引用、零测试**；`config.run_manifest.enabled=true` 只是配置事实。与规划 §2"按未完成功能验收"一致 → Z-003 接线对象。

### 4.3 仅投影/遗留（保留但不作为新能力基础）

- `openclaw_handoff.py`：config `enabled=false, legacy_read_only=true`；`openclaw/`、`.openclaw/` 目录及根目录 `HEARTBEAT.md`、`IDENTITY.md`、`SOUL.md`、`USER.md`、`TOOLS.md`、`_db_check.py`、`_tmp_plain_listener.py` 为旧工作区遗留。
- 各 `latest.json`/`latest.md` 均为可重建投影（AGENTS.md 既定原则），不是运行身份。

### 4.4 需新增（按总计划）

- `task_runtime/`（持久任务 + outbox + 状态机，W03）；统一 sources 契约层（W04）；`agent_protocol/` 意图 schema 与 Python broker（W07）；`api/` + `web/` 新工作台 + SSE（W09）。当前均不存在。
- `pi_runtime` 是安全单轮执行器，**不是**持久多代理编排（规划 §2 判断一致）。

## 5. UI 盘点（路由、写操作、身份/CSRF、端口、进程归属）

唯一 HTTP 服务：`review_workbench.py`（`ThreadingHTTPServer`）。

| 项 | 实际情况 | 证据 |
|---|---|---|
| 绑定与端口 | 默认 `127.0.0.1:8767` | `serve_workbench(host="127.0.0.1", port=8767)` |
| GET 路由 | `/`（面板，支持 view/case 过滤）、`/health` | do_GET |
| POST 路由 | `/runtime-action`、`/review-action`、`/shutdown` | do_POST |
| 写操作 | review queue 状态迁移（`record_review_queue_action`，`VALID_ACTIONS` allowlist + `FORBIDDEN_ACTION_TERMS` + 交易词阻断）；runtime case 动作（attach_evidence_url / attach_structured_fields / resolve_remediation / reject_remediation / defer_remediation） | 行号 147-160、304、1534-1537 |
| CSRF | 每服务实例 `secrets.token_urlsafe(32)`，提交校验 `hmac.compare_digest` | 行 206、1463-1476 |
| Host/Origin | 仅接受 loopback Host；带 Origin 时必须匹配 loopback | `_valid_loopback_peer_headers` 行 1576-1584 |
| 身份 | 无登录会话；本机单用户模型，写操作经 review_queue `enforce_roster=true` 校验真实 active actor | config + review_queue |
| 进程归属 | workbench 由 `scripts/start_research_workbench.ps1` 启动；停止脚本 `stop_research_workbench.ps1` 针对本项目进程 | scripts/ |
| 计划任务 | 4 个（DailyResearch/PilotReconcile/DailyDashboard/WeeklyReview）均未安装；最后计划运行 2026-08-26 | health 2026-09-17 Scheduled Tasks 节 |

`dashboard.py` 是静态投影生成器（写 `reports/dashboard/`），不是服务器。未发现其他监听端口。

## 6. 测试结果与真实调用声明

本次在锁定解释器上重跑（2026-09-18）：

```text
.venv312/Scripts/python.exe -m unittest discover -s tests
Ran 834 tests in 127.914s
OK (skipped=2)
```

2 个跳过项逐一定位（均在 `tests/test_research_workbench_ops.py` `WorkbenchShortcutBehaviorTests`）：

1. `test_creates_both_links_with_expected_target_arguments_and_workdir` —— skipped 'pywin32 and powershell.exe required for shortcut behavior tests'
2. `test_refuses_to_overwrite_unrelated_shortcut` —— 同上

原因：`.venv312` 未安装 pywin32（`import win32com` → ModuleNotFoundError）；powershell.exe 存在、IPv6 loopback 可用，其余条件跳过未触发。

- 3.11 本次未重跑；旧验收（`reports/release/python_environment_latest.md` 2026-09-17）记录 3.11.15 与 3.12.13 各 834 项、2 跳过、PASS。
- **本盘点 0 次真实 LLM 调用、0 次网络采集**。测试全部离线并在临时目录运行（输出中的 tmp 路径可证）。测试中出现的 s001/s002 等信号为测试夹具，与生产数据无关。

## 7. 风险分类

### 7.1 可自行修复（Zcode 在授权工作包内处理）

| # | 风险 | 归属 |
|---|---|---|
| R1 | 启动入口落在 PATH python 3.8.5（scripts 与文档命令均用裸 `python`），与锁定 3.11/3.12 环境脱节 | Z-002 |
| R2 | `run_once.ps1` 使用 example 配置而非 local 配置 | Z-002 |
| R3 | `local_jsonl` enabled=true 指向 `manual_signals.example.jsonl`，样例数据与真实输入无命名空间隔离，可能混入生产样本 | Z-002 |
| R4 | 401 重试与错误分类需按规划收敛（不循环重试 401、不跨项目取 key）——现状以 health 报告的 `credential_or_token_error` 分类为起点 | Z-002 |
| R5 | `run_manifest.py` 未接线（config 已 enabled 但 pipeline/schedule/pilot 零调用、零测试） | Z-003 |
| R6 | 共享写入协调、续租、崩溃恢复六类风险（规划 §9.2）未做针对性故障测试 | Z-003 |
| R7 | 无持久任务/outbox/状态机，run/agent-run 仍是批次模型 | W03+ |
| R8 | 适配器无统一 observation 契约（时间戳/单位/血缘） | W04 |

### 7.2 需有效凭据（用户提供，Agent 不得代填或跨项目读取）

| # | 风险 | 说明 |
|---|---|---|
| C1 | `DEEPSEEK_API_KEY` 返回 401 | 变量已设置但凭据无效/过期；需用户在供应商处轮换后自行注入环境。401 不自动重试（Z-002 固化） |

### 7.3 需真实人工批准（Agent 禁止代办）

| # | 风险 | 说明 |
|---|---|---|
| H1 | 首次 Git 提交建立 HEAD | 296 文件暂存清单需用户审阅后在其终端提交；在此之前发布基线保持 BLOCKED |
| H2 | 行情供应商内部核准 | provider registry 0/0；需 data_steward/compliance 等真实角色走 provider-init/review/activate |
| H3 | 研究名册缺员 | 32 案件 unstaffed（缺 compliance_officer、lead_analyst、publication_approver、quality_reviewer、research_coordinator 六类角色） |
| H4 | 许可证 REVIEW_REQUIRED | copyleft/复合/未知许可证须法务或合规真人批准，Agent 不得放行 |
| H5 | 生产灾备与备份 Go | 无 KMS/异地不可变副本/独立签字，生产备份状态保持 BLOCKED |
| H6 | 机构 Go/No-Go 与业务启用 | 按现有八阶段与门禁顺序，技术 PASS 不代表业务启用 |

## 8. 与规划判断的核对

| 规划 §2 判断 | 本次核实 | 结果 |
|---|---|---|
| run_manifest 存在但 pipeline 未接入 | 全库零引用、零测试 | 一致 |
| 无 Git HEAD | rev-parse 失败，unborn main | 一致 |
| 3.11/3.12 各 834 测试（旧验收） | 3.12 重跑 834/834、2 skip | 一致 |
| 审计解释器 3.8.5 与环境验收不一致 | PATH python 实测 3.8.5（Anaconda） | 一致，且确认入口未统一 |
| DeepSeek 401、行情 3 条阻断、官方原件 0、32 案件缺角色 | 引用 health 2026-09-17 快照，本次未联网复测 | 快照一致（时效性已注明） |
| 手工源为示例命名文件 | 内容为 example.com 合成样例 | 一致，须按样例隔离处理 |
| 当前数据配置 local_jsonl/eastmoney_filing/eastmoney 开启 | config 实测一致 | 一致 |

## 9. 下一步

Z-002（运行与数据边界）：统一入口到锁定解释器、样例输入与真实输入隔离、认证错误分类固化（缺 key / 401 / 源禁用 / 无新信号 / 行情过期五种状态各有明确呈现），不自动恢复计划任务或开机启动。

---

# 交付记录

## Z-002：运行与数据边界（2026-09-18 完成）

改动文件：`stock_agent/sources/example_data.py`（新增）、`sources/local_jsonl.py`、`sources/filing_jsonl.py`、`llm_agent.py`、`market_resilience.py`、`pipeline.py`、`scripts/run_once.ps1`、`scripts/watch.ps1`、`scripts/start_agent.ps1`、`scripts/start_research_workbench.ps1`、`scripts/run_daily_research.ps1`、`scripts/go_live_check.ps1`、`scripts/migrate_to_pi_agent.ps1`、`scripts/create_dashboard_shortcut.ps1`、`AGENTS.md`、`tests/test_z002_run_data_boundaries.py`（新增 27 项）、`tests/test_research_workbench_ops.py`（有意更新 start_agent 哈希钉）、`tests/test_scheduled_workspace_ops.py`（契约更新为锁定解释器形式）。

关键行为：
1. 入口统一：所有脚本解析 `.venv312\Scripts\python.exe`，缺失即报错，绝不回退 PATH python（实测 PATH 为 Anaconda 3.8.5）。`run_once/watch` 默认 local 配置，回退 example 时显式 WARNING。AGENTS.md 全部命令示例改为锁定解释器形式。
2. 样例隔离：`.example.` 命名文件与 `examples/` 目录下的 JSONL 信号自动携带 `payload.data_provenance="example"`；pipeline stats 新增 `example_data_signals`/`example_data_warning` 与 `disabled_sources`。
3. 认证分类：`_is_auth_error` 收紧（401/403/unauthorized/forbidden/api key/authentication/invalid token/token expired；max_tokens 类误报排除）；403 新增不重试；provider 全缺 key 时给出 `llm_missing_api_key` 显式配置状态而非 "all providers failed"；`diagnose_network_error` 补 403/forbidden → credential_or_token_error。
4. 发现并修复 uv venv 跳板问题：`.venv312` 的 python.exe 以子进程启动真实解释器，导致 workbench 健康 PID 校验永不匹配。start 脚本改为接受"启动 PID 或经验证的后代"，元数据记录真实服务进程 PID，失败清理连后代终止；stop/idempotency 语义不变。

验收：`test_z002_run_data_boundaries.py` 27 项全过（五种状态、样例隔离、认证分类、7 个脚本 AST 解析）；全量 861 项 OK / 2 跳过（pywin32）。LLM 零真实调用。回滚：revert 上述文件即可，无 schema/数据变更。

## Z-003：运行身份与失败恢复（2026-09-18 完成，故障用例先行）

改动文件：`stock_agent/run_manifest.py`、`stock_agent/pipeline.py`、`stock_agent/reports.py`、`scripts/run_daily_research.ps1`、`tests/test_z003_run_identity_recovery.py`（新增 12 项，先红后绿）。

先写的故障用例（红集 10/12）与对应修复：
1. 父目录/叶子 Windows junction 未被拒绝 → `_resolve_workspace_path` 新增从工作区根到叶子的逐组件 reparse/symlink 检查（lstat + FILE_ATTRIBUTE_REPARSE_POINT），非词法位于工作区内的路径同样拒绝。
2. 同秒跨模式报告互相覆盖（run 与 agent-run 租约名不同、设计上可共存，stamp 截断到秒）→ `write_outputs` 目标存在时追加 `-N` 后缀；三个输出改用 `atomic_write_text`（防半写）。
3. `run_manifest` 全库零引用 → pipeline `_run_with_coordination` 在账本 COMPLETED 后调用新增 `_record_run_manifest`：写不可变 per-run manifest + 指针；失败显式 `status=ERROR` 不吞异常、不改账本状态（json 先定稿再被 manifest 哈希，避免自引用）。
4. 调度摘要用目录名排序猜报告 → `run_daily_research.ps1` 读 `latest_manifest.json` 指针，交叉核验指针 run_id 与协调投影 run_id，不一致记为错误；summary 新增 `run_id/run_manifest_path/run_manifest_sha256`；report_path 优先取 manifest 钉住的 json_report。
5. 部分产物/账本损坏/同 run 冲突重写/重复 signal 保留 llm_error+needs_review → 回归守护测试（部分原已绿，现为契约）。

验收：`test_z003_run_identity_recovery.py` 12 项全过；全量 873 项 OK / 2 跳过。租约人工恢复规则未放宽（`require_explicit_stale_recovery` 不变）；manifest 不可变（同 run 不同内容拒绝）；旧 manifest 不因后续运行追加账本而变化（identity hash 只含自身投影）。回滚：revert 五个文件；manifest 文件为新增数据，可整体删除。

遗留（转后续工作包）：worker fencing token 设计仅由现有租约 TTL+人工恢复覆盖，W03 任务队列需单独实现并测试；`bind_scheduled_summary`（pilot 对账消费端）已在 run_manifest 实现但 production-pilot 接线属 W02 后续；`_trigger_source` 目前恒为 manual，调度侧 eligible_for_pilot 需 CLI 增加 --trigger-source 传递后在真实计划运行中验证。

## 首个产品切片（2026-09-18 完成，对应接手说明第 4 节六项 / W03+W07+W08+W09 骨架）

零新依赖（全部 stdlib + 既有 pi_runtime 调用路径）。改动/新增：`stock_agent/task_runtime/`（store.py 事务任务+outbox+幂等回执+fencing，__init__.py）、`stock_agent/agent_protocol/__init__.py`（意图 schema 校验+allowlist broker，显式分支分发）、`stock_agent/research_worker.py`（Pi 分析节点：外发最小化门禁、replay/live 双模式、401 不重试）、`stock_agent/task_board.py`（只读任务页+人工反馈 POST，127.0.0.1:8768，loopback Host/Origin + CSRF + 幂等 + expected_version）、`stock_agent/cli.py`（task-create/task-list/task-run-next/task-cancel/human-response/task-recover/task-board 七个命令）、`tests/test_first_product_slice.py`（18 项验收）。

六项对照：
1. 持久任务+outbox：SQLite WAL、BEGIN IMMEDIATE 事务内同时写状态与 outbox 事件；九态状态机（queued→leased→running→succeeded/failed/blocked/awaiting_human/cancelled/expired）；幂等键（case+input_version+role+prompt_version+model_hash）；取消/重试/租约续期/过期恢复（恢复只重排或过期，旧 worker 的 fencing token 永远失效）。
2. 公开事件输入：任务输入必须 `data_classification=public` 才允许创建（外发前置门禁）。
3. Pi 分析节点：真实路径经 `call_pi_agent`（禁工具隔离不变）；输出必须过 `validate_model_output`（schema/summary/人工问题/置信度 0-1）；401/超时分类失败不重试；replay 模式跳过模型调用且 model_call.mode=replay、called=False 全程可见。
4. 只读任务页：GET /、/api/v1/tasks、/health；REPLAY 行显眼橙色标注；不伪造"正在运行"。
5. 人工反馈接口：actor+decision+expected_version+command_id 幂等；acknowledge 记录为 personal_acknowledgement（scope 明示"个人研究签收，非机构批准"），只写任务库新域，不触碰机构 JSONL 账本。
6. 取消/恢复演示：测试覆盖 queued 直取消、running 取消标志、租约过期重排/耗尽过期、陈旧 fencing token 拒绝、board CSRF 阻断与幂等重放；CLI 端到端演示（replay 任务→awaiting_human→个人签收→DUPLICATE 幂等重建）已在 data/task_runtime/ 留下真实事件记录。

验收：`test_first_product_slice.py` 18 项全过；全量 891 项 OK / 2 跳过。LLM 零真实调用（live 路径仅以 stub 401 验证失败分支）。回滚：删除四个新模块/目录+cli.py 命令块+测试文件即可；data/task_runtime/tasks.sqlite3 为新域数据，可整体删除不影响既有域。

遗留（下一批）：任务输入的 public_context 目前由操作者构造，待接 W04 行情/新闻适配器自动供给；broker 只读动作的上下文绑定尚无签名链；SSE 与 React 前端未开始（W09 完整版）；DEEPSEEK_API_KEY 轮换后需做一次真实 live 调用验收（W08 验收条件）。

## W04-lite：任务上下文自动供给（2026-09-18 完成）

改动/新增：`stock_agent/task_supply.py`（行情缓存/信号库/证据索引 → public_context）、`stock_agent/cli.py`（task-create 增加 --from-symbols/--research-question/--lookback-hours）、`tests/test_task_supply.py`（4 项）。

行为：快照来自 `data/cache/market_quotes/latest.json`，`source_time` 只认上游报价时间（quote_timestamp/tick_time/trade_timestamp），缺失时如实标注 `unverified_missing_upstream_time` 并保留独立的 observed_at，绝不以抓取时间伪装行情时间；新闻来自 signals 表（URL 去重、回看窗口过滤、`data_provenance=example` 行排除——与 Z-002 标记联动）；证据摘要来自 vault 索引；缺失标的列入 `context_notes.quotes_missing`。replay 模式无录制响应时自动生成明确标注的占位草稿。

验收：4 项测试（source_time 诚实性/缺失不伪造/去重+样例排除+时效/规格形状）；CLI 端到端 `--from-symbols 600519` 实跑成功（快照来自真实缓存）；全量 895 项 OK / 2 跳过。回滚：删除 task_supply.py + cli 参数块 + 测试文件。

遗留（下一批）：SSE + 前端 ADR（W09 完整版，新增依赖需先 ADR）；live 模式真实调用验收（待 DEEPSEEK_API_KEY 轮换）；新闻聚类目前按键分组而非相似度聚类（W06 范围）。

## 补充批次（2026-09-18 晚）：模型切换、凭据链路、SSE（ADR-001 第一阶段）

1. **模型默认切换**：`config/watchlist.local.json` 四处 `deepseek-v4-pro` → `deepseek-v4.1-flash`（用户指令）；定价表保留 v4-pro 真实价格未冒用；契约测试 `test_config_defaults` 同步更新。注意：flash 单价未提供前，`model_operations.pricing.models` 无该模型条目，成本计量按未知处理（不得显示为 0）。
2. **凭据链路（用户显式授权后）**：密钥字面量仅存于 `.env.local`（`.gitignore` `.env.*` 规则覆盖，`git check-ignore` 验证）；新增 `config.load_local_secrets()` 在 CLI 启动读入环境（已设环境变量优先，不覆盖）；源码/示例/测试零凭据字面量。实测 doctor：key present、model=deepseek-v4.1-flash，但**该密钥对 api.deepseek.com 返回 401（credential_or_token_error）**——W08 失败路径验收通过，成功路径仍待有效密钥。
3. **SSE（ADR-001 第一阶段落地）**：`task_board.py` 新增 `GET /api/v1/events`（text/event-stream），`TaskStore.events_after(sequence)` 游标读取；`Last-Event-ID` 断线补发；流上限 300 秒后由客户端带游标重连；15 秒 keepalive；死连接写失败即结束；仅推任务域公开投影。`tests/test_task_board_sse.py` 2 项（积压补发/实时推送+断线续传）。

验收：全量 897 项 OK / 2 跳过（895 + SSE 2 项）。回滚：task_board/store 的 SSE 增量与测试文件可独立 revert；`.env.local` 删除即移除凭据。

待用户：有效 DEEPSEEK_API_KEY（更新 .env.local 一行即可，代码无需再改）；首次 Git 提交。

## 修复批次：验收报告 20260921 的 F01-F09（2026-09-21 完成，先红后绿）

依据 `ZCODE_ACCEPTANCE_REVIEW_20260921.md` 的 R1-R5 顺序。新增 `tests/test_acceptance_fixes_20260921.py` 14 项，修复前红 11/13（F08 竞态注入后补强为 14 项全红基线），修复后 14 项全绿；全量回归 911 项 OK / 2 跳过。改动文件：`task_runtime/store.py`、`research_worker.py`、`agent_protocol/__init__.py`、`cli.py`、`reports.py`、测试文件。

- **F01 外发门禁**：删除 CLI 对分类的强制提升（`_validated_public_spec` 要求 spec 显式 public，否则 SystemExit）；worker 每次执行都对当前 payload 跑真实 `prepare_llm_batch`（分类/密钥/个人数据/非公开/提示注入/限制名单），外发提示词改用治理后的最小化 safe payload；`provide_input` patch 限定白名单字段且合并后必须保持 public；拒绝时 `mark_blocked` 且模型调用数为 0（F01 测试即外发拒绝证据：stub.call_count==0 + status=blocked）。
- **F02 成功返回**：经 `response_metadata` 契约读取 usage/request_id/model，正文用 `str(response)`（LLMResponse 是 str 子类）；真实 LLMResponse 类型 mock 的成功测试通过并持久化 usage。
- **F03 待人工持久化**：`request_awaiting_human` 在同事务写入完整 output、model_call 与全部人工问题（`questions` 列表）；fail/blocked 也持久化 model_call。
- **F04 版本**：`_touch` 每次变更 `version+1`；旧版本提交返回 TaskVersionConflictError（测试断言 claim/awaiting 均递增且旧版被拒）。
- **F05 取消语义**：awaiting_human 取消立即转 cancelled；`record_human_response` 检查取消标志拒绝签收/补输入；worker 转待人工前发现取消则终止。
- **F06 幂等身份**：默认幂等键含输入内容指纹+模式（独立请求/不同模式不再合并，显式同键不同内容返回 TaskIdempotencyConflictError）；修复中发现的次生 bug（指纹比较把存储 JSON 文本当 dict）一并修正，同内容精确重试仍返回 DUPLICATE。
- **F07 证据绑定**：`validate_model_output` 增加 `allowed_evidence_refs`，未知/外案/上下文外引用一律拒绝；worker 校验输出 task_id（live 严格匹配；replay 夹具仅容忍字面 "placeholder" 因任务 ID 创建前不存在，已注释说明）。
- **F08 恢复竞态**：恢复在写事务内用新鲜行重验租约到期与取消标志；测试注入"快照物化后、开事务前经另一连接续租"的精确交错，断言不回收；worker 执行前显式续租。
- **F09 并发产物**：JSON 产物用 `O_CREAT|O_EXCL` 独占占有决定 stamp 归属（存在性检查无法防 check-then-write），其余两个产物跟随已占有的 stamp 原子写；并发 barrier 测试断言两条运行各得独立文件。

回滚：六个文件按本节 diff revert；无数据迁移（既有 tasks.sqlite3 的 version 语义从此递增，属向前兼容）。遗留：报告指出的 board 人类身份、HTML 实时刷新、schedule/pilot 的 manifest 消费端到端、live 成功验收仍在验收报告"尚未完成范围"清单中，随对应工作包处理。

## 补充批次（2026-09-21）：面板实时化 + manifest 消费端（验收遗留清单第 2/3 项的离线部分）

1. **面板实时化**：SSE 改为默认事件（无 event: 行，浏览器 onmessage 统一接收）；页面（task_board.py _PAGE_TEMPLATE）新增连接状态徽标（LIVE/RECONNECTING）、EventSource 驱动的任务表实时重渲染（服务端渲染仍为无 JS 基线）、awaiting_human 行内异步反馈表单（fetch + X-Task-Board-CSRF，结果页内提示并自动刷新）。测试：SSE 2 项更新 + 页面接线断言 1 项（tests/test_task_board_sse.py）。
2. **manifest 消费端**：新增 CLI run-manifest-bind --summary（只读对账，非 PASS 退出码 1）；run_daily_research.ps1 pilot 模式在 production-pilot-sync 后对最新 daily summary 执行绑定，无可对账摘要时计错并置非零退出。测试：tests/test_manifest_binding.py 3 项（scheduled agent-run 绑定 PASS、篡改 sha BLOCKED、manual 触发不 eligible）。

验收：全量 915 项 OK / 2 跳过。浏览器端真实渲染/键盘/移动体验、board 人类身份、live 成功调用仍属验收报告未完成范围。回滚：task_board/cli/run_daily_research 三处增量 + 两个测试文件独立 revert。

## 补充批次（2026-09-21 晚）：凭据链路根因修复 + W08 真实调用验收通过

**根因**：宿主环境残留一把旧 DEEPSEEK_API_KEY（尾号 e364，一直 401）；`load_local_secrets` 原设计"环境变量优先"导致 `.env.local` 中用户提供的有效密钥（尾号 d33f）被遮蔽。DeepSeek 回显的 `****e364` 与文件尾号不符暴露了这一点。

1. **优先级反转**：`load_local_secrets` 改为"已批准本地凭据文件覆盖继承的环境变量"——文件是操作者最新的明确凭据决定；docstring 记录该事件。doctor 复测：`llm_connectivity OK`。
2. **模型 ID 修正**：供应商 /models 实际清单为 `deepseek-flash` 与 `deepseek-v4-pro`，无 `deepseek-v4.1-flash`；四处 model 字段与契约测试改为 `deepseek-flash`。
3. **超时调优**：deepseek_primary provider 与 pi_agent 150 秒（thinking=high 首次实测 60 秒超时）；llm_agent 信号路径 90 秒（保持 run_ops 预算交叉校验 210≤240 通过——该校验成功拦下一次不一致改动）。
4. **W08 真实调用验收（通过）**：task `task_20cac766c9774fcd`（live，自动供给 600519 快照）——governance approved → 真实 Pi→deepseek-flash 调用 called=True，13.4 秒，usage 310+1606=1916 tokens（真实计费元数据经 F02 修复后的契约读取）；模型拒绝把单个未验证快照当作已确立事实（unknowns 如实列出），给出 5 个澄清问题进入 awaiting_human。首次真实验收共 3 次调用（/models 探测 1 + 超时 1 + 成功 1），在规划"最多 5 次"预算内。
5. 失败路径已在此前批次验证（401 不重试、超时重排）；本批补齐成功路径。

验收：全量 915 项 OK / 2 跳过。安全边界未变：密钥只在 .env.local（gitignored）与环境变量，未入源码/日志/命令行。回滚：load_local_secrets 优先级、配置三处（model/timeout）与契约测试独立 revert。

## 修复批次：复验报告 20260921-B 的 B01-B07（2026-09-21 完成，先红后绿）

依据 `ZCODE_SSE_MANIFEST_REVIEW_20260921.md`。新增 `tests/test_review_fixes_20260921b.py` 13 项，红基线 12/13，修复后全绿；全量回归 928 项 OK / 2 跳过。改动文件：`scripts/run_daily_research.ps1`、`stock_agent/cli.py`、`stock_agent/run_manifest.py`、`stock_agent/research_worker.py`、`stock_agent/task_board.py`、`tests/test_acceptance_fixes_20260921.py`（夹具按新证据契约补 sha256）。

- **B01 路径引号**：`--summary '$LatestDailySummary'` 单引号包裹，含空格/中文路径作为单一 argv 传入（脚本契约测试锁形）。
- **B02 触发来源贯通**：CLI `agent-run --trigger-source {manual|scheduled}` 注入 `config["_trigger_source"]`（管线 manifest 据此判 pilot 资格）；脚本 agent-run 命令传 `-TriggerSource`。测试经真实 `cli.main()` 入口（patch sys.argv）断言配置注入，不再绕过调用环节。
- **B03 摘要选择**：pilot 模式逐个读取 summary JSON，仅 `mode -eq "daily"` 参与选取，dashboard/review/pilot 摘要不再遮蔽。
- **B04 绑定前置门禁**：run-manifest-bind 先于 production-pilot-sync 执行；`$ManifestBindSucceeded` 为假时跳过 sync（阻断其对账副作用），计错并置非零退出；无 daily 摘要同样阻断。
- **B05 绑定必填**：run_id/manifest 路径/报告路径/SHA 任一缺失、空值或类型错误 → BLOCKED（不再静默转空串跳过比较）；CLI 非 PASS 退出码 1（测试断言 SystemExit）。
- **B07 研究专用安全上下文**：新增 `build_safe_research_context`——逐项校验（行情须 source_time_status=verified；证据须 64 位 sha256+非空摘要），未验证事实剔除并计入投递记录；`model_call.context_delivery`（delivered/dropped/outbound_sha256）与 `outbound_context` 全量留痕，空上下文不再冒充基于资料的研究；输出证据引用绑定到**实际投递的安全集合**而非原始输入 ID。测试断言：已验证事实到达提示词、未验证价格不出现在外发内容、空投递如实记录、越集引用失败。
- **B06 表单保留**：JS 改为按 task_id 键更新——输入即标记 dirty，dirty 且版本未变的行组在刷新中原样保留（不丢焦点与未提交内容）；dirty 且版本已变则保留表单并提示 conflict（不自动把旧输入绑到新版本）；fetch 响应带 applySeq/lastApplied 序号守卫，旧快照不得覆盖新快照。浏览器级输入保留/快速事件/断线重连复验仍开放（复验报告同类要求）。

安全边界不变：enforce 未关闭、未补假授权；治理辅助路径在测试中指向临时区属测试隔离。回滚：五处源文件增量 + 测试独立 revert。

## 修复批次：复验四项剩余问题（2026-09-21，先红后绿；磁盘受限下完成）

新增 `tests/test_review_fixes_20260921c.py` 7 项（红基线 7/7），修复后全绿；全量回归 935 项 OK / 2 跳过。改动：`research_worker.py`（真核验）、`task_supply.py`（证据契约）、`production_pilot.py`+`cli.py`+`run_daily_research.ps1`（显式摘要消费）、`task_board.py`（真 DOM 保留）、四个既有测试文件的 vault/供应商夹具补齐。

1. **安全上下文真核验（#1）**：`source_time` 严格 ISO 解析（"not-a-date" 剔除）；行情供应商必须出现在 `market_data_governance` 批准集（registry 覆盖开启时以注册表为准——当前 0 active 即全部剔除并注明 `provider_not_approved`，与既有治理一致）；证据 sha256 必须命中**官方 vault 索引中实际留存**的文档（仅哈希格式不再足够）。剔除原因带后缀写入投递记录。
2. **供给链路兼容（#2）**：`task_supply._evidence_summaries` 输出 summary+sha256+retained 契约（仅留存文档）；集成测试证明 vault→供给→安全上下文全链路投递成功。
3. **pilot 消费精确运行（#3）**：`sync_production_pilot(scheduled_summary_path=...)` 显式摘要贯穿 base 状态与对账（`scheduled_report_path` 取自该摘要而非最新文件）；CLI `production-pilot-sync --summary`；脚本传入已绑定的 `$LatestDailySummary`（含引号）。
4. **真 DOM 保留（#4）**：废弃 outerHTML 序列化——dirty 表单组节点**原地不动**（.value 与焦点天然保留）；版本变化时保留表单但标记 `stale-form` 冲突；干净组才重建；消失任务移除；快照按 applySeq 顺序应用。

**磁盘警示**：E 盘在测试期间被外部进程以 ~5GB/分钟填充至 100%（当前 2.2MB 可用）；本仓库自身写入极小（logs 共 4.1MB），未删除任何文件，外部占用需用户排查。回归在余量极限下完成（测试写 C 盘临时区）。

回滚：五处源文件 + 测试夹具更新独立 revert。浏览器级 B06 复验仍开放。

## 修复批次：ZCODE_VAULT_DOM_REVIEW D01-D06（2026-09-21，先红后绿 + 真实浏览器取证）

新增 `tests/test_review_fixes_20260921c.py` 13 项（红基线确立后修绿）、`tests/_vault_fixture.py`（真实生产者夹具：`import_official_document` 在临时工作区建真 vault——事件链+内容寻址对象+投影，绝不再手写索引）、`tests/test_board_page_dump.py` + `tests/browser_board_check.py`（锁定解释器出页面快照，独立 Chromium 驱动，6/6 PASS）。全量回归 `-B` 942 项 OK / 2 跳过。

- **D01 真实契约**：`research_worker`/`task_supply` 停止手写索引解析，统一走新增 `stock_agent/evidence_context.py`——内部调用权威 `load_evidence_vault`（documents 为 document_id 键的字典）与 `verify_evidence_vault`。集成测试由真实导入接口在临时区生成索引再走供给→安全上下文全链路。
- **D02 验真而非查索引**：`verified_evidence_documents` 复用权威验真结果——链完整性 + 对象存在 + 字节哈希 + 尺寸；任一对象缺失/篡改使整体 technical_status 非 READY → **全部证据阻断**（fail-closed）；隔离文档与无有效发布时间的文档逐项剔除。测试：删除对象文件 → 全部阻断。
- **D03 真实行情门禁**：快照封装为 price 载体走 `assess_market_signals`（新鲜度/未来时间/未知时间/时段/共识/来源数/授权同一套判定）；投递条件为 `llm_eligible is True`；受控时钟（`now=` 参数）验证正例。顺带修复真实门禁缺口：未来时间戳此前因年龄钳位非负而通过——新增 `_is_future_timestamp`（120 秒容差）两个时段分支均 BLOCKED `future_quote_timestamp`。无时区值按配置时区解释（既有策略，保留），不可解析值按缺失时间处理。
- **D06 fail-closed**：显式摘要缺失/坏 JSON/缺 report_path → `ProductionPilotPolicyError`（不再回退 latest）；三组拒绝测试。
- **D04/D05 真 DOM 修复 + 浏览器取证**：replaceGroup 先快照节点列表再移动（修复遍历中移动致表单行丢失）；SSR 行携带与客户端一致的 `data-task/data-version/data-taskform` 键（首渲染识别 SSR 行，无重复）；重排改为顺序感知——顺序未变零移动，需移动时跳过含焦点节点；提交成功清除 dirty。真实 Chromium 6 项检查全过：首渲染无重复、输入值与焦点保留（activeElement.name == notes）、干净表单版本更新后仍可操作（forms=1, version=2）、脏表单版本冲突标记 stale-form 且不重绑、重排保持焦点。复验方法：`tests/test_board_page_dump.py` 重新生成快照后任意含 playwright 的解释器运行 `tests/browser_board_check.py`。
- **事故记录**：迁移期间一度让测试误写真实 vault 账本（b 文件治理配置未指向临时区）。已确认真实 vault 原为空（health 报告 events=0），逐项移除误写事件与对象并删除过期投影恢复空态；全部测试已改走临时区 vault，事后核验 `data/evidence_vault/events.jsonl` 为 0 行。此为流程错误，已如实记录。

改动文件：`evidence_context.py`（新）、`research_worker.py`、`task_supply.py`、`production_pilot.py`、`market_data_governance.py`（未来时间硬化）、`cli.py`（supply 传 config）、`task_board.py`、四个测试文件夹具迁移 + 三个新测试资产。回滚：按文件独立 revert；`tests/board_page.html`/`board_states.json` 为生成物可删。

## 修复批次：ZCODE_INCIDENT_AND_GROUNDING_REVIEW 三项（2026-09-22）

### 1. 事故留证与测试写入隔离（P1）
- **事故档案**：`docs/implementation/INCIDENT_20260921_VAULT_TEST_WRITE.md` —— 完整时间线（误写 16:17:59 起源于 b 文件 `_gov_config` 未替换 vault 路径即调用导入）、误写事件全部身份字段（document_id/sha256/合成 source_url/actor/signal_ids，取自幸存快照）、受影响文件清单、清理操作记录、事前证据链（历史快照 events=0 + 误写后首份快照 event_count=1 即测试事件）、以及明确标注的不可验证项（无误写前逐字节备份；删除操作本身违反只追加约束，须真实负责人审查追认）。**不再修改历史账本**；7 份污染期快照（reports/evidence_vault/evidence_vault_20260921T16*）保留为证据；latest 投影仍显示滞后状态待负责人确认后刷新。
- **写入隔离守卫**：`tests/_vault_fixture.import_document` 在写入前校验 cfg 全部 vault 写路径（events/objects/quarantine/report）解析后必须位于给定临时根内，相对路径直接拒绝；负例测试 `test_default_real_paths_fail_before_any_write`（保留真实路径的配置必须在写入前 AssertionError）。

### 2. 摘要引用绑定（P1）
`research_worker` 证据投递从哈希集合升级为**文档身份 + 案件关联 + 摘要出处**三重绑定：
- **身份**：投递引用必须等于权威 document_id（正确哈希+错误引用 ID → `ref_document_id_mismatch` 拒绝）；
- **案件关联**：文档绑定 signal_ids 时，任务必须声明 `bound_signal_ids` 且相交（否则 `not_associated_with_task` 拒绝）；未绑定信号的公开文档允许；
- **摘要出处**：投递摘要一律取 vault 投影文档标题（`summary_source=vault_projection` + 摘要自身 sha256）；用户提供的相异文本降级为 `human_note`（`unverified_human_judgment_not_official_fact`），绝不再给任意摘要盖 vault_verified。四组验收负例全部落地：错误文档 ID/无关案件/被替换摘要/投递后摘要版本变化（vault 摘要稳定、仅 human_note 变）。全部测试夹具声明 `bound_signal_ids`。

### 3. 严格浏览器验收 + 真实 HTTP/SSE 端到端（P2）
- 删除 BODY 假通过：焦点断言改为**节点同一性**（`document.activeElement === window.__focusNode`）+ selectionStart/End 保留；
- 真实重排：本任务从首位移到末位；含焦点时顺序让位（焦点组不动）、失焦后下一轮补齐——两条语义分别断言；
- 顺带修复真缺陷：干净但含焦点的组此前永不更新（提交后状态滞留旧值）——replaceGroup 现在记录焦点输入与选区并在重建后恢复；
- **真实 HTTP/SSE 端到端**（`tests/browser_board_e2e.py`）：子进程起真实 task-board 服务器 + CLI 种子 replay 任务 → Chromium 打开真实 HTTP 页面 → 填写表单真实 POST（页面 CSRF）→ SSE 驱动刷新 → 行状态变 succeeded、#msg recorded、SSE 连接 LIVE，6/6 通过。严格渲染检查 8/8。复现：`test_board_page_dump.py` 重新生成快照后运行两个浏览器脚本（任意含 playwright 解释器）。

验收：专项 18 项绿（c 文件）；全量 `-B` 947 项 OK / 2 跳过。回滚：research_worker/evidence_context(_vault_fixture)/task_board/测试资产独立 revert；事故档案与污染期快照按档案第 6 节由负责人处置。

## Codex 推进：信号供给绑定与面板恢复（2026-09-22）

本节为后续独立实现与验收记录，不修改上文历史结论。详见
`SUPPLY_SSE_ACCEPTANCE_20260922.md` 与前批 `ISOLATED_ACCEPTANCE_PROGRESS_20260922.md`。

- 供给按配置指定的 SQLite 只读读取真实入库信号，公开属性、标的、时效与样例来源检查后生成
  `bound_signal_ids`；文档关联筛选先于数量上限，不再从整个 vault 任取前五份。
- Worker 执行前重查绑定：删除、改为内部、标的不符、空/伪造绑定及无关联原件均拒收；
  `context_delivery.signal_binding` 留存校验状态。旧夹具通过真实 SignalStore + vault 导入生成。
- 补齐治理 carrier 的标的：任务声明、行情键及独立查得的绑定信号都参与限制名单检查，
  删除任务 symbols 不能隐藏受限标的。测试 enforce=true，未修改真实限制名单或批准任何来源。
- SSE 重连主动拉快照；HTTP 错误或畸形快照不清空表格；旧响应不覆盖新快照。
- 新增绑定测试 14/14；守卫测试 4/4。真实 Chromium 五组场景通过，含真实 TCP 断线、
  线上观察 Last-Event-ID、断线补发、乱序响应、503/畸形响应保留及无新事件重连恢复。
- 最终隔离回归：965 项，106.427 秒，errors=14 / skipped=2，退出码 1。
  14 个错误记录全部是已知 PowerShell/生命周期/junction 子进程被守卫拒绝，**不宣称全绿**。
- 正式 vault 仍为 0 字节，修改时间 2026-09-21 16:20:55。事故仍待独立人工审查。
  本批零真实模型调用、无生产配置变更、无计划任务安装、无生产 GO。
- 文档标题元数据不等于已经审核的正文摘录；下一步须补正文片段/摘要版本出处与独立子进程验收，
  再在真实业务前置条件满足后推进 W10，不把 replay 或上述技术结果当作真实业务样本。

## Codex 推进：正文摘录出处与原生验收准备（2026-09-22）

详见 `EXCERPT_NATIVE_ACCEPTANCE_20260922.md`。保持既有计划顺序，不扩展交易、自动审批或更多角色。

- 新增本地只读 `evidence-excerpt`：绑定原件哈希、解析器版本、字符范围、摘录哈希；
  权威 vault 验真后再次校验实际读取字节。支持严格 UTF-8 text/plain，PDF/HTML 暂不支持。
- 摘要草稿绑定摘录及版本，仍为未审核的人类判断或模型推断，哈希不等于事实成立或获得批准。
- Worker 的摘录投递记录仅留本地，正文不进入外部模型。外发标题明确标为元数据，
  摘要哈希改为对应实际投递的截断后标题。未宣称真实正文研判已完成。
- 新增源码白名单隔离包构建/验证工具与 Windows VM 操作手册；真实配置、凭据、账本不入包。
  `reports/native_acceptance_20260922.zip` 含 95 项源码/合成文件及 manifest，555,863 字节；
  SHA-256：`eb3f037a87c4405b044bc62560776b207bb1c57bd06214262f3cf939658b5b67`。
  已构建并验证，原生执行状态仍为 `NOT_RUN`；没有放开宿主 Python 子进程守卫。
- 新增摘录测试 14/14、打包测试 11/11；既有绑定 14/14、证据门禁 18/18。
  全量受保护回归 **990 项，123.719 秒，errors=14 / skipped=2，退出码 1**。
  错误记录全部来自已知 PowerShell/生命周期/junction 子进程拒绝，没有其他错误或断言失败；
  这是待隔离环境验证的范围，**不宣称全量通过**。
- 正式 vault 仍为 0 字节，修改时间仍为 2026-09-21 16:20:55；未改凭据、人员、授权、
  计划任务、生产数据或事故留证。本批零真实模型调用，没有生产 GO。
- 下一步：独立原生验收、绑定具体摘要版本的真实人工审核，再在业务前置条件满足后跑 W10。

## Codex 推进：本地摘要审核与身份边界（2026-09-23）

详见 `SUMMARY_REVIEW_ACCEPTANCE_20260923.md`，操作契约见 `docs/SUMMARY_REVIEW_LOCAL_V1.md`。

- 新增本地 Windows 账号与名册绑定校验：不接受环境变量或浏览器表单自报 SID；拒绝冒名、
  service session、重复 SID、非 active human、越权或受限标的。没有登记任何真实人员或授权。
- `task-summary --action show|submit|review` 接入既有 task SQLite/outbox：摘要、摘录、输入/输出
  指纹和实际任务版本绑定，作者与审核人须为不同 actor 和 Windows 账号；支持通过、拒绝、
  退回修改、撤销、24 小时失效和历史保留。只读 show 不迁移旧库、不载入凭据。
- 摘要审核不完成/重跑任务，不外发、不交易、不替代机构质量、合规或发布批准。
  原有个人 acknowledge 不能使摘要变为 supported。所有结果 external_eligible=false。
- 面板增加只读审核状态，走原有 SSE；同版本状态变化不丢脏表单。未开放摘要 HTTP 写入口，
  不把服务进程 SID 当浏览器身份。普通人工反馈的幂等回执同时补上精确请求指纹校验。
- 新增测试 **23/23 OK**，既有切片 **18/18 OK**；最终 Chromium **6 组通过**，已检查桌面与
  手机截图。浏览器徽标变更为合成快照渲染测试，不冒充真实人员审核；真实决策/HTTP/事务由临时集成测试覆盖。
- 全量受保护回归：**1013 项，79.999 秒，errors=14 / skipped=2，退出码 1**。
  原生子进程验收仍受守卫阻断，未宣称全绿；没有断言失败。
- 新原生验收包：`reports/native_acceptance_20260923.zip`，97 项源码/合成文件及 manifest；
  SHA-256 `185c989e27b1683047fd658e4dd4a2a448667bc19817446349055ccdca90e889`，构建/验证成功，
  native_execution 仍为 NOT_RUN。旧包未覆盖。
- 正式 vault 仍为 0 字节，修改时间仍为 2026-09-21 16:20:55。零真实模型调用，未改真实配置、
  名册、批准、计划任务、历史事件或事故档案。没有重启现有业务工作台。
- 这不是完整 WS20：账号证明不等于真人在场、浏览器登录或不可抵赖签名；真实身份绑定证据、
  独立真人 UAT、原生隔离验收与 W10 真实案件仍开放。不得把本地 supported 记作机构上线 GO。

## Codex 推进：账号发放的本地摘要查看页（2026-09-23）

详见 `SUMMARY_VIEWER_ACCEPTANCE_20260923.md`，操作契约见 `docs/SUMMARY_VIEWER_LOCAL_V1.md`。

- 新增 `task-summary-open`：先核验本机 Windows 账号、名册与显式复核权限，后开放一个任务、
  一个摘要版本的五分钟只读页面；随机 loopback 端口，不重启现有面板，不安装常驻/自启服务。
- 一次性 fragment 入口换取仅存内存的短期令牌；精确 Origin/Host/端口、CSP、防嵌入、无跨域、
  无浏览器持久存储。每次读取复核配置、名册、限制名单、版本和真实原件；变化后旧访问永久失效。
- 页面按文本显示摘要、摘录、哈希、角色、版本及审核历史；撤权/版本变化/过期/关闭后清空，
  晚到响应不能填回内容。该能力只证明本地账号发放的查看权限，不宣称浏览器真人认证。
- 不写业务账本、不记批准/签收、不调用模型，不增加摘要决定 HTTP 路由，不改变旧面板权限边界。
- 新增测试 **22/22 OK**，原摘要审核 **23/23 OK**；真实 Chromium **6 组通过**，已查看桌面/手机截图。
- 最终全量受保护回归：**1035 项，94.170 秒，errors=14 / skipped=2，退出码 1**，无断言失败。
  14 个错误记录仍为已知原生子进程守卫拒绝，不宣称全量通过。
  原始输出：`reports/summary_viewer_regression_20260923.log`。
- 第一次全量回归中的一项新夹具错误已修：不同任务须使用不同案件 ID，否则正常幂等返回旧任务；
  未修改生产幂等规则，修后再次执行专项及全量。
- 正式 vault 仍为 0 字节，修改时间仍为 2026-09-21 16:20:55；未修改真实配置、人员、授权、
  凭据、计划任务或事故档案。本批零真实模型调用，没有真实人员 UAT，没有生产 GO。
- 下一步仍是独立身份/原生验收，然后在真实业务前置条件满足后跑 W10；旧验收 ZIP 未覆盖或冒称当前版本。
