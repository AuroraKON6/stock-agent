# Kimi 接入 K01-K05（离线部分）批次交付报告

日期：2026-10-13。基线：开始时 HEAD `58710bc`（GLM-11）；期间并行会话已推进至 `4365592`
（其 K04 方法候选文档已提交）。本批按协作纪律**未提交任何文件**、未触碰并行会话活跃中的
`docs/GLM_START_HERE_20261011.md` 与 `docs/GLM_DIFY_ADOPTION_PLAN_20261013.md`。
本批执行 [GLM_KIMI_INTEGRATION_PLAN_20261012.md](../GLM_KIMI_INTEGRATION_PLAN_20261012.md) 的
K01 全部、K02/K03 离线安全适配、K04 方法候选（第二批，与已提交 KIMI_K04 文档互补——
合并指引见本批候选文档头部）、K05 面板接线候选。

**本批性质：全部离线。零真实查询、零联网、零密钥读取、零新依赖、零生产配置变更。**

## 1. 修改文件与差异

### 新增（19 个文件）

| 文件 | 内容 |
| --- | --- |
| `stock_agent/integrations/__init__.py`、`integrations/kimi/{__init__,contracts,transport,artifacts,client}.py` | K02 安全客户端包：契约验证（F03）、固定 origin 传输（F02）、宿主产物写器（F01）、组合执行流与结果码（F05/F06） |
| `stock_agent/sources/kimi_adapter.py` | K03：查询账本（intent→result→consumed）、缓存（权利感知键）、标准化（未知保留缺口）、registry 门禁、每日数据调用预算 |
| `stock_agent/queries_view.py` | K05：查询账本投影 + `kimi-data-status` 分层健康（只读，不联网） |
| `tests/test_kimi_client_v1.py`（55 测试） | T01/T02/T04/T05/T07/T08/T09/T10/T11/T15/T21 客户端边界 |
| `tests/test_kimi_sources_v1.py`（25 测试） | T03/T12/T13/T14/T19/T20 + registry kind 扩展 |
| `tests/test_kimi_board_v1.py`（8 测试） | 视图投影、status 分层、board 路由与 SSR |
| `config/candidates/kimi/contracts.v1.json` | K01 首批 4 API 类型化契约快照（候选，不加载） |
| `config/source_registry.kimi.example.json` | K01 候选登记条目（4 条全部 pending，不进生产 registry） |
| `docs/implementation/KIMI_K01_INVENTORY_20261012.md` | K01 资产/许可/权利矩阵/契约表/缺口映射 |
| `docs/implementation/KIMI_K01_ASSET_HASHES_20261012.json` | 222 个 SKILL.md + 18 个 datasources 文件逐文件 SHA-256 |
| `docs/implementation/KIMI_TRANSPORT_ADR.md` | 传输 ADR：Python 受控客户端为默认边界，MCP 后置，拒绝整包 SDK |
| `docs/implementation/KIMI_METHOD_CANDIDATES_20261012.md` | K04 方法候选文档 |
| `output/skill_audit/kimi_candidates/c1/c2/c3_*.md` | 三份候选增补文本（审计工作区，非生产） |

### 修改（4 个文件，窄改）

| 文件 | 差异 |
| --- | --- |
| `stock_agent/sources/registry.py` | `SOURCE_KINDS` 增加 `financial_statement`（+1 行）。消费方核对：kind 仅 ops_view 展示与自校验；行情门禁走 signal source_type 不受影响；未知 kind 仍拒绝 |
| `stock_agent/market_data_governance.py` | 新增 `_assess_historical_background` 分支 + `_assess_one` 入口 1 处分派：payload `data_scope=="historical_background"` 走独立验证（声明窗口、无未来日期、provider attestation），标 `historical_background_not_live` 不冒充实时；**无 marker 的实时路径逐字节不变** |
| `stock_agent/cli.py` | 注册 `kimi-data-status`、`queries-view` 两个只读子命令（注册/dispatch/helper 三段式，照 events-view 模式） |
| `stock_agent/task_board.py` | `/api/v1/queries-view` 路由 + 页面"数据查询"区块 + fetchQueries/showQueries JS + 测试钩子 `__taskBoardQueries`（SSE 契约与既有区块未动） |

### 未修改

外部两个资产目录（只读）、共享配置/`.mcp.json`、生产 `config/source_registry.json`（不存在，仍 fail-closed 空）、
`config/pi_skills.json` 与 `pi_skills.lock.json`（生产 skills lock 未刷新）、`.pi/skills/`、真实凭据、
计划任务/自启动、历史账本。计划附录 A 四个基准哈希本批核对**全部 MATCH**（资产未变）。

## 2. 复用与新增类型

- 复用：`sources/registry.py` 的登记与 fail-closed 查询（未换新表）；`budget_guard.py` 的"以账本计数"模式
  （data-call 预算独立计量，不与模型调用混算）；`events_view.py` 的投影三件套模式（schema_version/
  generated_at/scope_note）；board 的"degrade honestly 503"模式；`test_task_board_sse.py` 的回环 server 测试模式。
- 新增类型：`DataQuery`/`DataResult`（计划 7.1/7.2 的离线子集；未知字段 null+reason）；`TransportOutcome`
  结果码（auth/permission/rate-limit/empty/contract/destination/too-large/invalid/unknown 分立，F05/F06）；
  `ArtifactPlan`（宿主产物 ID，远端名只作不可信元数据）；查询账本三种 record_type。
- 迁移兼容：`SOURCE_KINDS` 加法扩展，向后兼容（旧 registry JSON 全部仍通过校验）。

## 3. 测试证据

命令与退出码：

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py                     # 全量
.\.venv312\Scripts/python.exe -B scripts/run_isolated_tests.py --pattern test_kimi_client_v1.py
.\.venv312\Scripts/python.exe -B scripts/run_isolated_tests.py --pattern test_kimi_sources_v1.py
.\.venv312\Scripts/python.exe -B scripts/run_isolated_tests.py --pattern test_kimi_board_v1.py
```

最终全量（2026-10-13）：**`Ran 1287 tests ... FAILED (errors=14, skipped=2)`，failures=0**。

- 14 errors 为**预存守卫阻断**（`test_research_workbench_ops`/`test_z002`/`test_z003` 中需要
  PowerShell/子进程/junction 的用例被隔离入口按设计拒绝）。已用 `git stash` 在干净 HEAD 上复跑确认同样存在
  （与 memory 基线 1199/14/2 的"14"一致），**非本批回归**；按 AGENTS.md 不移除守卫、不单独验收不宣称绿。
- 本批新增 88 测试（55+25+8）全部通过。
- 测试数据全部在 `TemporaryDirectory` + `project_path_override`；HTTP 测试仅回环 127.0.0.1。
- 证据类型：全部 **fixture/合成**（FakeTransport 注入）。无 replay、无 live；无真实模型调用。

CLI 冒烟（只读）：`kimi-data-status` exit 0（六层 configured=false/NOT_RUN/BLOCKED 如实报告，未联网）；
`queries-view` exit 0（空账本空视图）。

## 4. 验收矩阵覆盖（本批）

离线可覆盖并已测：T01、T02、T03、T04、T05、T07、T08、T09（object 类型契约验证器支持；天眼查不在首批白名单）、
T10、T11、T12、T13（标准化部分）、T14/T19（幂等重放）、T15、T20、T21、T22（候选不触 lock 部分）。

**NOT_RUN（如实登记）**：T06（junction/reparse 对抗性替换——需受控 Windows VM 原生验收，artifacts 仅做了
best-effort reparse 检查并显式声明非沙箱声明）；T16/T17 部分（同上游多路由/恶意内容——适配器已按 upstream
唯一性防伪共识，对抗性文本测试待 K06 前补）；T18 双进程并发（同进程双 store 已测，跨进程待 live 前）；
T23-T26（模型引用绑定、浏览器 SSE、MCP、真实查询——依赖后续批次/真实条件）。

## 5. BLOCKED / NOT_RUN 清单

| 项 | 状态 | 依据 |
| --- | --- | --- |
| 一切 live 查询 | BLOCKED | K01 权利矩阵：访问/保留/外发/预算四项均无真实授权；默认 disabled，`kimi_integration.enabled` 无人可置 true |
| 生产 registry 候选条目 | BLOCKED | 4 条全部 pending_human_approval，等真实人工（K01 §8） |
| `SOURCE_KINDS` 扩展生效 | 候选中 | schema 变更建议待负责人批准（代码已兼容，不改现有行为） |
| 方法候选激活 | BLOCKED | 等现有 pi_skills 审核机制；本地提取物无真实上游 repo commit，`approved_adapted` 登记缺依据 |
| Windows 原生安全验收（T06 等） | NOT_RUN | 需受控 VM，本批未做 |
| 浏览器隔离验收（新增查询区块） | NOT_RUN | 建议纳入下一次 `tests/browser_board_isolated.cjs` 运行（区块遵循既有 fetch 模式，SSR 已测） |

## 6. 回滚方式

全部新增均为加法，回滚 = 停用，无数据迁移：

1. 配置层：生产配置无任何 kimi 键 → 客户端默认 disabled，查询账本/缓存零生成；已有审计记录（如有）保留不删。
2. 代码层（如需彻底移除）：删 `stock_agent/integrations/`、`sources/kimi_adapter.py`、`queries_view.py`、
   3 个 test_kimi_*.py；还原 cli.py/task_board.py 两处接线与 registry.py 一行、market_data_governance.py
   历史分支（git 可精确恢复：`git checkout 58710bc -- <file>`）。
3. 面板：queries 区块 fetch 失败时静默留空（与既有区块同模式），不影响任务表与 SSE。
4. 不删除任何历史账本、批准记录或审计事件；外部资产目录本批未动，回滚无需触碰。

## 7. 下一批与最小人工事项

下一批（等本报告验收后）：K06 前置的对抗性内容测试补齐（T16/T17 完整版）、浏览器隔离验收挂接查询区块、
（如负责人批准 kind 扩展与候选条目）生产 registry 候选落位演练。

仍需真实人工的最小事项（与 K01 §8 相同，不重复索取非敏感材料）：

1. iFinD/东财商业条款的权利确认（查询/存储/外发）。
2. 受支持认证方式确认 + 独立项目凭据引用（解锁 K06 第 1 步）。
3. `SOURCE_KINDS` 扩展与 4 条候选登记条目的批准或否决。
4. dynamic-finance-charts / financial-market-terminal 的 LICENSE 缺失追溯（若后续要用）。
