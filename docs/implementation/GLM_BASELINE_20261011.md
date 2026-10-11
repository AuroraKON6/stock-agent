# GLM-01 基线核实报告（2026-10-11）

执行：GLM。范围：只读盘点，不改业务代码、不启动 run/agent-run/watch、不调用真实模型、不修改生产配置和账本。
本报告不覆盖历史 BASELINE.md，只记录本轮实测事实。所有命令均在项目根目录用锁定解释器执行。

## 1. 事实清单（附命令依据）

### 1.1 Git 状态

| 项目 | 事实 | 依据 |
|---|---|---|
| HEAD | 无法解析；`main` 分支 0 提交 | `git rev-parse HEAD` → fatal; `git log` → "does not have any commits yet" |
| 状态条目 | 共 347：272 已暂存（A）、24 暂存后修改（AM）、51 未跟踪（??） | `git status --porcelain` 分类计数 |
| 未跟踪构成 | tests 25、docs 12、stock_agent 11、scripts 2、`.v2c/` 1 | 同上按目录汇总 |
| 关键未跟踪源码 | `scripts/run_isolated_tests.py`、`stock_agent/task_runtime/`、`stock_agent/agent_protocol/`、`research_worker.py`、`evidence_context.py`、`evidence_excerpts.py`、`local_identity.py`、`summary_review.py`、`summary_viewer.py`、`task_board.py`、`task_supply.py` 及 25 个测试文件 | untracked 明细 |
| 暂存密钥扫描 | 干净。命中仅为测试占位符（`must-not-leave-this-process`、`sk-test-secret-key-12345` 测试夹具）与 CSS 误报；20+ 字符 `sk-` 真实密钥 0 条 | `git diff --cached` 正则扫描（不打印值） |
| 敏感文件隔离 | `config/watchlist.local.json`、`config/*.local.json`、`.env*`、`pi_runtime/agent_home/auth.json`、`data/*`、`reports/`、`logs/` 均被 `.gitignore` 排除 | `.gitignore` 内容核对 |

### 1.2 环境

| 项目 | 事实 |
|---|---|
| Python 3.12 | `.venv312` = Python 3.12.13（锁定解释器，全部命令使用它） |
| Python 3.11 | `.venv311` = Python 3.11.15 存在（双环境齐备；3.11 全量测试本轮 NOT_RUN，属 GLM-11 范围） |
| Node | v24.16.0 |
| Pi 包 | `@earendil-works/pi-coding-agent` 锁定 0.84.2（package.json + lock 一致） |
| 依赖锁 | `requirements.lock.txt` 785 行，uv 生成，含哈希 |
| 磁盘 | E 盘剩余 156G（9-21 记忆中的"被外部进程填满"已解除） |
| win32com | 缺失（解释 1 个 skip） |

### 1.3 配置与模型（脱敏）

| 项目 | 事实 | 不能据此声称 |
|---|---|---|
| 启用来源 | `local_jsonl`（手工情报池）、`eastmoney_filing`（600519/300750/000001）、`eastmoney` 行情（同 3 标的）；rss/alltick/free_quote/pysnowball/akshare 均关闭 | 已有稳定免费多源新闻和行情供给 |
| 主题 | 3 主题 × 3 标的（AI算力/新能源车/中特估高股息） | 已支持 20-50 标的 |
| 活动模型 | **deepseek-flash**（`pi_agent.model`、`llm_agent.providers[0].model`、`multi_role_agent.model` 三处一致，Pi runtime） | V4 Pro 已启用 |
| v4-pro 出现位置 | 仅两处非启用位置：`pi_runtime.py:86` 代码兜底默认（配置缺 model 字段才生效）、`.pi/settings.json` defaultModel（被显式 `--model` 参数绕过）、`model_operations.pricing` 定价表 | 任何运行时实际使用 v4-pro |
| 模型解析链 | `provider.model` > `pi_agent.model` > 代码默认 v4-pro（`pi_runtime.py:133/86`）；响应记录 `parsed.model`/`responseModel` 区分请求与实际（`pi_runtime.py:233/270/340`） | 实际返回模型字段已在所有路径验证非 unknown |
| 凭据方式 | `api_key_env` 环境变量引用；本地真实值不在仓库。历史聊天贴出的凭据轮换状态待负责人确认 | 凭据已轮换 |
| 治理 | `llm_governance.enforce=true`，外发仅 public，审计开启；`production_pilot` shadow 模式，全链自动审批 false | 机构启用 |

### 1.4 入口与调度接线

| 项目 | 事实 |
|---|---|
| CLI 子命令 | 124 个（含 task-create/task-run-next/task-cancel/human-response/task-recover/task-board/run-manifest-bind/agent-handoff/pi-handoff-run 等 TaskStore 全链） |
| 模块规模 | `stock_agent/` 65 个 py、`sources/` 11 个；`task_runtime/store.py` 747 行、`evidence_vault.py` 1431 行、`research_worker.py` 488 行、`task_board.py` 521 行、`task_supply.py` 250 行、`agent_protocol/__init__.py` 245 行 |
| 计划脚本 | `scripts/run_daily_research.ps1` 存在，使用锁定解释器、失败退出码 2 检查、transcript 日志、run_manifest 指针接线 |
| Windows 计划任务 | 两个 OpenClaw 任务存在但均 **Disabled**（Gateway + Watchdog，quant-research）；无本项目活动计划任务。"Session agent for Process Lasso" 属其他软件，与本仓库无关 |
| run_coordination | 投影 latest.json：status=PASS，时间 2026-09-18T01:47（超过 3 周前的滞后投影，仅为投影）；无活动租约 |

### 1.5 隔离测试结果（本轮实测，两次运行）

命令：`.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py`

| 次数 | 结果 | 耗时 |
|---|---|---|
| 第 1 次 | Ran 1035 tests, **errors=14, skipped=2**，无断言 FAIL | 664.8s |
| 第 2 次（完整日志到 %TEMP%\glm01_tests_20261011.log） | 同上，**退出码 1** | 788.0s |

**14 个错误逐一归类（本轮实测清单，非沿用历史）：**

1. `test_research_workbench_ops.PowerShellParseTests.test_scripts_parse_cleanly` × 3（start/stop/create_research_workbench_shortcuts .ps1）
2. `test_research_workbench_ops.ReusedPidSafetyTests.test_reused_pid_is_not_killed_but_metadata_cleaned` × 1
3. `test_research_workbench_ops.WorkbenchLifecycleBehaviorTests.test_start_idempotent_health_stop_repeated_stop` × 1
4. `test_z002_run_data_boundaries.TestEntryScriptsUseLockedInterpreter.test_edited_scripts_parse_cleanly` × 7（run_once/watch/start_agent/start_research_workbench/run_daily_research/go_live_check/migrate_to_pi_agent）
5. `test_z003_run_identity_recovery.TestParentJunctionRejected` × 2（`test_leaf_junction_is_rejected`、`test_report_under_parent_junction_is_rejected`）

全部 14 个均为 `PermissionError: test isolation: unguarded child process denied`——守卫拒绝测试内派生 PowerShell/cmd 子进程（解析 .ps1、进程生命周期、`mklink /J` junction）。与历史 2026-09-23 记录（1035/14/2）数量和类别一致；本轮独立核实，无新增错误类别。按 AGENTS.md，这些必须单独在受控环境（一次性 Windows VM）验收，不得关闭守卫追求全绿。

**2 个 skip**：win32com 缺失（快捷方式测试类 `test_research_workbench_ops.py:406`）、IPv6 loopback 不可用（`test_research_workbench_v19.py:872`）——环境条件跳过，良性。

**与历史耗时差异**：历史 2026-09-23 记录 94 秒，本轮 665-788 秒。数量与结果一致，差异可能来自机器状态/杀毒扫描/临时目录开销；不影响结论，留观。

### 1.5.1 原生验收与浏览器验收状态

| 层次 | 状态 |
|---|---|
| 原生 Windows VM 验收 | NOT_RUN（本轮无 VM；历史包 `reports/native_acceptance_20260923.zip` 存在于磁盘，manifest 记录 97 项，native_execution NOT_RUN） |
| 浏览器隔离测试 | 本轮未单独执行；全量套件内含的浏览器契约测试随套件通过（非 Chromium 实测） |
| 真实模型调用 | 零（本轮无任何 live 调用） |
| 真人 UAT | 零 |

### 1.6 正式 vault 误写事故现状

依据 `docs/implementation/INCIDENT_20260921_VAULT_TEST_WRITE.md`（只读）与文件系统核对：

- `data/evidence_vault/events.jsonl` 仍为 **0 字节**，mtime 2026-09-21 16:20:55，此后未改动（本轮 `wc -c` 复核）。
- 7 份污染期快照仍在 `reports/evidence_vault/`（保留为事故证据，未清理——正确）。
- **事故未关闭**，3 项后续处置待真实负责人：① 审查档案并决定追认清理或要求补偿性审计事件；② 污染期快照保留期；③ `latest.json/md` 滞后投影刷新时点。
- 防复发守卫已在 `tests/_vault_fixture.py`（写路径隔离校验 + 负例测试）。
- GLM 不重写、不删除事故账本、污染期快照与恢复记录。

## 2. 精确阻塞清单

| # | 阻塞 | 阻塞对象 | 解除条件 | 需要谁 |
|---|---|---|---|---|
| B1 | 仓库无任何提交，HEAD 不可解析 | 可回滚源码基线、发布审计、run_manifest Git 绑定 | 完成本基线提交（见 §5） | 用户已授权（2026-10-11 消息"记得提交git"） |
| B2 | 14 个原生子进程测试被守卫阻断 | 原生 PowerShell/进程/junction 验收宣称 | 一次性 Windows VM 按手册验收（GLM-11） | 用户提供 VM |
| B3 | vault 事故 3 项处置未完成 | 事故关闭、相关账本信任恢复 | 真实负责人审查追认 | 用户（项目负责人） |
| B4 | 历史聊天贴出的凭据轮换状态未确认 | 凭据安全 | 负责人确认已轮换或轮换后更新环境变量 | 用户 |
| B5 | 无真人身份验收机制落地 | 浏览器可信写入、W10 真人 UAT | 个人操作身份绑定 ADR + 真人实测（GLM-06/07） | 用户 + GLM 实现 |
| B6 | 真实来源许可/预算未登记 | live 外发与付费调用 | GLM-02 来源登记 + 用户批准 | 用户 |
| B7 | Python 3.11 全量测试本轮未跑 | 双环境发布 PASS 声明 | `python-env-audit --run-tests`（GLM-11） | GLM 执行即可 |

## 3. 复用模块盘点（GLM-02/03 优先复用）

| 模块 | 规模 | 状态 | 复用判断 |
|---|---|---|---|
| `stock_agent/sources/`（11 文件）+ `storage.py` | 现有 | local_jsonl/eastmoney_filing/eastmoney 在用 | GLM-02 直接复用，新增来源登记契约 |
| `stock_agent/source_health.py`、`market_data_governance.py` | 现有 | 已接线 | GLM-02 复用行情门禁 |
| `stock_agent/task_supply.py`（250 行） | 现有 | 已接线 manifest | GLM-04 复用 |
| `stock_agent/task_runtime/store.py`（747 行） | 现有 | 任务/租约/fencing/outbox | GLM-04/05 复用，不建第二套 |
| `stock_agent/evidence_vault.py`（1431 行） | 现有 | 验真链完整 | GLM-03 复用权威验真 |
| `stock_agent/evidence_context.py`/`evidence_excerpts.py`/`summary_review.py`/`summary_viewer.py` | 现有 | 本地摘录链路 | GLM-03 复用，external_eligible 保持 false |
| `stock_agent/research_worker.py`（488 行）+ `agent_protocol/__init__.py`（245 行） | 现有 | 单轮 + proposal_only 子任务 | GLM-05 扩展为多轮，不重写 |
| `stock_agent/pi_runtime.py` | 现有 | Pi 隔离调用 + 版本锁 | GLM-05 复用 |
| `stock_agent/local_identity.py`（158 行） | 现有 | Windows 账号核验 | GLM-06 复用 |

## 4. GLM-02/03 最小实施清单（下一批）

按主计划 §8，下一批最多两个边界独立小包（GLM-02 最小部分 + GLM-03 离线契约部分），先写失败用例：

**GLM-02（真实来源与事件输入契约）**
- 新增来源登记 schema（上游、用途、许可状态、正文保留权、外发权、时效、限额、失效时间），GLM 只建结构与校验，不填真实批准（`stock_agent/sources/registry.py` 新文件 + `config/source_registry.example.json`）。
- 事件身份三时间分离（source_time/observed_at/fetched_at）与单位/复权固定：在现有 SignalStore 写入路径上做增量校验（改 `storage.py` 最小补丁 + 负例测试）。
- 重复/修订/无时间/伪多源测试（隔离 fixture）。

**GLM-03（有出处的正文与事实包——离线部分）**
- UTF-8 正文路径完善 + 解析器版本锁定/大小上限/NEEDS_OCR 显式状态（`stock_agent/evidence_parsing.py` 新文件，vault 侧只加引用不改验真链）。
- 事实包绑定（document_id、页码/字符区间、摘录哈希、事实类型）：扩展现有 `evidence_context.py`。
- `external_eligible` 继续恒为 false；外发授权 ADR 只起草不生效。

**明确不动**：`pi_runtime.py` 模型字段、生产配置、名册、限制名单、任何账本。

## 5. Git 基线提交方案（已获用户授权；执行被 Mimosa gate 阻塞，见 §7）

原则：不用 `git add -A`；逐类审查后提交；不推远端。

1. `.gitignore` 追加 `.v2c/`（本机插件状态目录，仅含 plugin_root 指针文件）。✅ 已完成
2. 重新暂存 24 个 AM 文件的当前工作区版本（工作区即当前事实）。✅ 已完成
3. 暂存 51 个未跟踪文件中除 `.v2c/` 外的全部（docs 12、tests 25、stock_agent 11、scripts 2——纯源码/文档/测试，已过密钥扫描）。✅ 已完成
4. 提交（含本报告与两份 GLM 计划文档）。**两次被 Mimosa L3 git gate 拦截**，暂存集完整保持（359 文件），待用户处置（§7 三选项）。提交后 HEAD 可解析，run_manifest 的 Git 绑定获得事实基础。
5. 不上传远端；后续每批工作包单独提交。

**排除项**（继续被 .gitignore 挡在库外）：本地配置、凭据、data/reports/logs 运行数据、事故快照、pi_runtime 密钥与 node_modules、skills 第三方仓库。

**回滚**：基线提交前状态无法用 Git 恢复（无历史）；提交本身可通过 `git reset --soft HEAD~1` 撤销且不丢工作区文件。基线提交后每批改动均可按提交回滚，历史数据不受影响。

## 6. 本轮未做与状态申报

- 未改任何配置、名册、账本；未启动 run/watch；零模型调用（含 Pi）；零浏览器操作；零真人 UAT。
- 业务代码改动仅一处：`stock_agent/backup_restore.py` SQLite 标识符白名单加固（Mimosa 拦截驱动，见 §7，专项测试 19/19 OK），其余为 `.gitignore` 追加一行、本报告新增与 Git 基线提交。
- 两次全量隔离测试写入仅限临时目录。
- 技术盘点完成 ≠ 技术发布 READY ≠ 机构启用。M5 原生验收保持 NOT_RUN。

## 7. Mimosa 提交拦截与处置（2026-10-11 实录）

首次基线提交被宿主 Mimosa L3 钩子强制拦截（25 个 high，扫描范围为整个工作区而非仅暂存内容）。逐项核实分类：

| 发现 | 判定 | 处置 |
|---|---|---|
| `backup_restore.py:960/1029` SQLite 表名插值 | 形式真阳性、实质低危（表名来自 sqlite_master 且有引号转义，但插值本身可加固） | **已修复**：新增 `_SAFE_SQL_IDENTIFIER` 白名单（`^[A-Za-z_][A-Za-z0-9_]*$`），非法表名跳过并在 metadata 路径留痕；专项测试 19/19 OK |
| `pi_skills.py:63-64` `eval(`/`exec(` "代码注入" | 误报——是 `PROHIBITED_TEXT_MARKERS` 拦截黑名单的字符串字面量，用于拒绝危险 skill 内容 | 不改。改写字面量规避扫描器会削弱安全控制的可读性，属扫描器逃逸 |
| 5 处测试"硬编码凭据"（`test-key-not-real`/`synthetic-test-key` 等） | 误报——测试用 patch.dict 注入显式假密钥，是凭据环境变量测试的正确写法 | 不改 |
| `test_research_workbench_ops.py:273` SSRF | 误报——测试对临时 loopback 服务的健康轮询 | 不改 |
| `_db_check.py:14` SQL 注入 | 未暂存文件（.gitignore 排除的遗留诊断），不属提交内容 | 不属本次提交范围 |

处置原则：只修复真实可加固项，不做扫描器逃逸。

**第二次提交仍被拦截**（同一 25 项；backup_restore 发现行号随修改移动至 967/1040，说明 gate 为模式匹配，白名单校验不改变其判定）。进一步核实：

- 项目已有 **2026-09-06 的正式甄别**：`.mimosa/security-policy.json` 的 `threatModel.exclusions` 覆盖今天被标记的每一个文件（pi_skills.py、backup_restore.py、storage.py、5 个测试文件、_db_check.py 等），`exclusionRationale` 完整记录 26 项 high 的分类理由，全量甄别报告在 `reports/handoff/mimosa_triage_20260906.md`。单文件编辑钩子尊重该策略（本会话 Edit 均判定 clear），但 commit 前的 L3 gate 不消费该豁免。
- 插件 README 记录了 gate 的官方配置面（环境变量，属宿主进程，AI 会话内无法可靠设置）：`MIMOSA_GIT_GATE_MODE=graded|ask|deny|warn`（默认 graded：high 强制拒绝）、`MIMOSA_GIT_GATE_GLM=1`（启用 GLM 语义复核，默认关以避免延迟和费用）、`MIMOSA_NO_GIT_GATE=1`（整体关闭）。
- 按本项目"不得为追求全绿关闭守卫"纪律与 2026-09-17 先例（首提交由用户在自己终端执行；mimosa 仅拦 AI 会话），GLM 不单方面改 gate 配置、不用 --no-verify、不改写插件内部状态。

**当前状态**：359 个文件已全部暂存（含加固与全部交付物），提交待用户以下三选一：
1. 在自己的终端执行提交（既定先例，gate 不作用于用户终端）：
   `cd /d "E:\stock agent" && git commit -m "Baseline: initial source baseline of A-share research system (GLM-01)"`
2. 为 ZCode 宿主设置 `MIMOSA_GIT_GATE_GLM=1`（语义复核有望正确分类既有甄别过的误报；可能产生 GLM API 费用）或 `MIMOSA_GIT_GATE_MODE=ask/warn` 后让我重试；
3. 明确指示本次关闭 gate（`MIMOSA_NO_GIT_GATE=1`）由我重试。
