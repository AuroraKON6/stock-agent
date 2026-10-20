# 项目重新审查与当前证据

日期：2026-10-20，Asia/Shanghai。项目：`E:\stock agent`。

本次为架构和实施交接审查，保留业务代码及运行配置。已执行指定隔离测试、只读源码/环境检查和公开官方资料核验；未执行真实数据查询、真实模型调用、Docker 安装/部署、计划任务或机构批准。

## 1. 结论

当前项目具备较完整的研究任务、治理、证据和个人反馈骨架，适合渐进改造。最急的工作是：

1. 修复版本、幂等、未知外发、并发事务与身份绑定。
2. 接通正文解析、结构化事实和声明级引用。
3. 让用户在网页完成创建、阅读、主动追问、修订与个人签收。
4. 在这些语义保持不变的基础上容器化和接入 Dify。
5. 用统一受控服务层支持多种 MCP，逐个增加数据能力。

不建议换成另一个开源金融项目整体重写，也不建议先铺大量 Agent/MCP 再处理任务一致性。

## 2. 本轮基线

| 项目 | 本轮观察 | 证据类型 |
| --- | --- | --- |
| HEAD | `004e4da3b698f1029c9d74f42813fbe772397fc6` | Git 只读 |
| 开始时工作树 | `git status --porcelain=v1` 无输出 | Git 只读 |
| 最近提交 | DF-02 受控 API、服务凭据；DF-01 候选修正至 1.17.1 | Git log |
| Python | 项目 `.venv312\Scripts\python.exe`，3.12.13 | 锁定解释器读取 |
| Python 文件 | stock_agent 下 104；tests 下 120 | 本轮目录计数 |
| 行数 | 源码约 47,319；测试约 26,644 | PowerShell 内容/行计数口径 |
| Docker | Windows PATH/标准安装位置未找到 CLI/Desktop | 宿主只读，不等于排除任意目录安装 |
| WSL | Ubuntu / Stopped / version 2 | `wsl --list --verbose` |
| WSL 内部 Docker | 本轮未启动发行版，未核验 | UNVERIFIED |
| 宿主资源 | 16 逻辑核、15.7 GiB RAM | CIM 只读 |
| 空闲磁盘 | C 约 7.8 GiB、E 约 85.2 GiB、F 约 3696.5 GiB | 当时快照，实施前须复测 |
| 真实数据权益/模型/用户 UAT | 本轮未调用或验证 | NOT_RUN/UNVERIFIED |

参考命令：

```powershell
git status --porcelain=v1
git rev-parse HEAD
git log -4 --format='%h %ad %s' --date=iso-strict
Get-Command docker,wsl,git,node -ErrorAction SilentlyContinue
wsl --list --verbose
.\.venv312\Scripts\python.exe -B -c "import sys; print(sys.version); print(sys.executable)"
```

旧文档的“没有 HEAD”、源码/测试数量、C29/E151 GiB 空闲等均不是当前事实。本轮没有用历史 latest 状态推断当前真实人员、供应商、原件或机构生产状态。

## 3. 分工与审查范围

并发上限内分批开展，避免多人重复探索同一模块：

| 子任务 | 范围 | 结果用途 |
| --- | --- | --- |
| audit_core | TaskStore、worker、Pi、调度与运行所有权 | 状态与恢复修复 |
| audit_docker | Linux/Windows、依赖、网络、存储、身份和部署 | 容器迁移边界 |
| audit_connectors | Kimi、来源登记、缓存、额度、Dify/MCP 接口 | 数据/工具网关设计 |
| audit_security_qa | 正文/事实、引用、分类和身份验证 | 研究质量与信任边界 |
| audit_reuse_finance | F 盘金融仓库 | 数据/研究实现复用 |
| audit_reuse_platform | F 盘工作流/MCP/研究框架 | 成熟依赖与工程测试复用 |
| audit_product_plan | 页面入口、追问、签收、个人范围 | 最小产品闭环 |
| 最终计划复核 | 工作包、依赖、授权和验收一致性 | 交接计划可执行性 |

参考矩阵另由专门子代理整理。源码缺陷为静态确认或由隔离测试支持的观察；未对正式系统进行攻击或压力实验。

最终计划复核的七项修订已纳入任务书：调用心跳前移到 R02；R12 拆成文本/PDF/OCR 子切片；整链模式可先容器切换；增加无 `.git` 镜像的发布来源证明；明确个人证据导入作用域；补 UTC/上海业务日与额度边界；区分同进程隔离测试与跨进程 N/C 验收。

## 4. 可复用资产

| 能力 | 当前状态 | 必须保留的边界 |
| --- | --- | --- |
| TaskStore 与消费账本 | SQLite WAL、BEGIN IMMEDIATE、版本、attempt、outbox 存在 | 一个权威业务状态机 |
| 模型预算 | 已有调用前原子预留 | 数据查询预算不能假设已同等事务化 |
| Pi 推理 | 固定无工具/无 Skills/无扩展/无会话参数 | 不将外部 MCP 直接放入生产 Pi |
| Evidence Vault | 原件、哈希、验证与审计 | 本地完整性不等于机构档案批准 |
| 工作底稿/来源/治理 | 多层领域模型和校验已存在 | 不搬到 Dify 节点中重新实现 |
| 个人反馈 | 有输入更新、命令幂等和个人签收 scope | 当前身份/状态入口仍需修复 |
| 摘要独立审核/只读 viewer | 版本、身份、政策和证据绑定设计可复用 | 不为个人模式关闭独立审核要求 |
| Kimi | 类型化客户端、产物命名、候选契约、查询投影 | 未接成真实获准供给链 |
| Dify | 服务认证、受控 HTTP 与候选收件 | 尚无已验收的 Dify 部署和最终采纳 |

## 5. 核心发现

下列行号对应审查基线，实施时若文件变化应重新定位。P1 表示在容器/外部接口启用或个人首案前需要解决；P2 表示必要的工程或质量改进，不等同于已发生事故。

### F01 输入版本存在两个事实源 P1

- `stock_agent/task_runtime/store.py:381` 的 INSERT 未显式写 input_version 列；payload 可包含其他版本。
- `store.py:718` 补证更新列版本，却未同步 payload 的同名字段。
- `stock_agent/dify_api.py:470` 从 payload 读取版本。
- 影响：创建版本 5 可能出现列为 1；补证后 Dify 仍可能选到旧事实包。
- 处理：R01，权威版本和不可变快照统一；历史分歧先报告，不猜测修史。

### F02 外发超时进入普通重试 P1

- `pi_runtime.py:218` 把超时转为普通 PiRuntimeError。
- `research_worker.py:554` 对非鉴权异常一般设 retryable；`store.py:563` 可重排队。
- 影响：已发出但未收到结果的调用可能再次收费。
- 处理：R02，unknown 状态、预留不释放、对账后决定下一步。

### F03 Dify run 进入业务幂等键 P1

- `dify_api.py:287` 把 dify_run_id 放入任务 origin。
- `store.py:302` 默认 key 含 payload 指纹。
- 影响：相同业务跨新平台 run 可能新建任务；同 key 改 run 又可能冲突。
- 处理：R01/R03，business intent 与 execution association 分开。

### F04 调用中没有持续租约心跳 P1

- `research_worker.py:397`、`:531` 在调用前续租，随后同步阻塞。
- `pi_runtime.py:627` 使用 subprocess.run；租约 TTL 与调用 timeout 未见完整强制关联。
- 影响：长调用与恢复重新领取重叠，旧 worker 仍可能产生外部消耗。
- 处理：R02/R09，独立连接心跳、截止时间、drain、fencing。

### F05 worker 返回与持久终态不一致 P2

- `store.py:529` 完成时可因取消写 cancelled。
- `research_worker.py:670` 丢弃 complete 返回，固定返回 succeeded。
- 失败路径返回 failed 时，store 可能实际排队重试。
- 处理：R01，区分 attempt 失败与任务状态，返回以持久化记录为准。

### F06 Dify 多线程共用 SQLite 连接 P1

- `dify_api_server.py:62`、`:171` 为 ThreadingHTTPServer。
- `dify_api.py:105` 持一个 store；`store.py:185` 单连接允许跨线程。
- 与 `task_board.py:640` 已有 board_lock 不同，Dify 路径未见等价序列化。
- 影响：存在并发事务交错风险；本轮未做动态竞争复现。
- 处理：R03，请求连接/事务边界，真实并发测试。

### F07 Dify 授权核销非原子、候选未完成 fencing P1

- `dify_api.py:131` 普通 append；`:633/:647/:656` 为读取验证、业务写入、核销分步。
- `:558` 的验证回执未绑定确切 task/action/expected_version/payload。
- `:513/:542/:545` 对 run/attempt/task version 的记录不构成后端分派匹配和 CAS。
- 影响：候选收件可复用，但 DF-02 报告“校验 fencing 后提交”的表述超出当前实现。
- 处理：R03，事务授权消费和受控 dispatch adoption。

### F08 常驻服务凭据撤销不即时生效 P2

- `dify_api.py:109` 构造时载入 credentials；后续验证沿用内存快照。
- 处理：R03，可信存储读取或带版本的可强制失效缓存。

### F09 Kimi 查询预算与幂等非事务 P1

- `sources/kimi_adapter.py:468` 至 `:489` 先查预算/历史再追加 intent。
- `:63` 普通 JSONL append；历史扫描有数量边界。
- 影响：并发可能重复派发或超额；不能用串行 fixture 证明多 worker 安全。
- 处理：R04，原子 query intent/预留/唯一键。

### F10 Kimi 原始产物和缓存权利不完整 P1

- `kimi_adapter.py:494` 把 artifact_root 交给 client；`client.py:291` 可直接写产物。
- 留存检查主要在 adapter cache 写入；`:215` 读缓存未完整校验 TTL、当前留存权和用途。
- 处理：R04，统一 RetentionDecision，当前权利+时效读时校验。

### F11 Kimi 执行入口尚未接主供给链 P1

- 当前 governed_execute 的生产调用链未见落地；queries_view 引用适配器主要做投影。
- `transport.py:98` 主要检查 HTTPS；可构造 DestinationPolicy 不等于已批准目的地。
- 来源登记的用途/RPM/共享额度尚未完整进入执行门禁。
- 处理：R04/R11，先安全 factory 和候选契约核对，再接入主链。

### F12 正文与结构化事实尚未形成研究链 P1

- `research_worker.py:270` 至 `:283` 使用 title_metadata_only_not_body_analysis；人工摘要是未验证判断。
- extract_body、build_fact、convert_amount_to_cny 的搜索结果为定义及测试，没有生产调用。
- `evidence_excerpts.py:33` 只接 text/plain；`evidence_parsing.py:112` 文本层 PDF 未实现。
- 处理：R05/R12/R13，原件→定位解析→结构化事实→获准事实包。

### F13 空引用可伴随非空研究结论 P1

- `agent_protocol/__init__.py:139` 主要校验“给出的引用属于集合”，没有强制具体事实必须引用。
- 没有逐条 claim→fact 支持校验。
- 处理：R06，缺证据输出与事实草稿类型分开，数值/事实强制绑定。

### F14 计算声明可能冒充确定性计算 P2

- `evidence_facts.py:32` 只验证 formula 字符串及 ID 形式，不实际取输入重算。
- `:81` 接受 float，未拒绝 NaN/Infinity。
- 该模块当前未接生产，是接线前应修复的缺口。
- 处理：R05，白名单计算函数、Decimal、真实输入与口径。

### F15 外部返回不能自我宣告可信分类 P2

- `llm_governance.py:704`、`:726` 接受 payload 中公开/分类声明；原用途是受控本地输入。
- 处理：R13/R18，外部 connector 的声明只是来源声称，最终分类/用途由宿主权利策略签发。

### F16 DNS 检查与连接未绑定 P2

- `evidence_vault.py:224` 和 `:1231` 先检查解析公网地址，再由 urllib 连接，未绑定已验证目的地址。
- 这是启用网络前的静态设计问题，未证明发生外泄。
- 处理：R18，受控出站代理或地址绑定、每次重定向验证及响应限制。

### F17 Linux 身份与当前个人 actor 入口 P1

- `local_identity.py:23/:55/:122/:154` 在非 Windows 拒绝身份流程。
- `task_board.py:866` 接收 caller actor；`store.py:676` 仅要求非空。
- 回环与 CSRF 是已有防护，但不能等同于真实个人身份。
- 处理：R10，个人 owner 登录、服务端主体；Windows 桥或成熟容器认证。

### F18 Docker 网络、路径与依赖 P1

- `dify_api_server.py:54/:93` 绑定和 Host/Origin 仅回环，直接服务 DNS 会被拒。
- `config.py:11` 路径锚在代码根；测试 override 不跨线程。
- `requirements.lock.txt:2` 为 Python 3.11/windows；`pyproject.toml:5` 仍写 >=3.8；python_environment 固定 Windows Scripts 路径。
- 处理：R07/R08/R10，Linux 锁、显式目录、代理信任、配置允许 host。

### F19 旧调度和容器生命周期未统一 P1

- `scripts/run_daily_research.ps1:171` 固定 agent-run。
- `execution_mode.py` 有 batch/task 排他，但不覆盖所有外部编排和跨容器归属。
- `run_coordination.py` 是本机文件租约；`audit_log.py:149` 的 lock 遗留不能靠容器自动删除。
- 处理：R09/R16/R25，单 state-advancer，持久 owner generation，切换对账。

### F20 用户追问和签收由模型提问间接决定 P1

- `research_worker.py:643` 仅当 human_questions 存在时等待用户，否则直接 succeeded。
- `store.py:700` 反馈仅允许 awaiting_human；`task_board.py:73/:184` 表单同样限制。
- 处理：R14，草稿完成/用户处置分开；主动追问与已完成后续研究有明确命令。

### F21 相同问题可能永久命中旧任务 P1

- `task_intake.py:165` 的 question key 由问题、股票、mode 构成，不含明确新意图/as-of。
- 处理：R01/R14，网络重试复用 intent，用户“重新研究”新建 intent。

### F22 个人页面入口与阅读体验不足 P1/P2

- `task_board.py:831` 仅 human-responses POST；创建主要在 `cli.py:940`。
- 表单要求 actor/英文 decision/JSON patch；抽屉摘要在 `:572` 截前 400 字，引用主要列 ID。
- `:401/:489` 对部分 fetch 错误静默留空；个人签收与 Summary 独立审核展示容易混淆。
- 处理：R14，完整创建/导入/阅读/取消/追问/签收，empty/stale/failed 分开。

### F23 收藏仓库不能直接作部署源 P2

- 本地 Dify HEAD 已更新，但工作树显示大量 staged deletion，docker/web 不完整；原因未确定。
- LangGraph 有大量主要为行尾的差异；其他仓库未逐个保证工作树干净。
- 处理：第三方采用从固定提交/发布的干净隔离副本开始，不对收藏库 reset。
- 详见 [参考项目矩阵](../REFERENCE_REUSE_MATRIX_20261020.md)。

## 6. 本轮测试

### 6.1 全量隔离基线

执行：

```powershell
.\.venv312\Scripts\python.exe -B scripts/run_isolated_tests.py
```

本轮终态输出：

```text
Ran 1347 tests in 378.233s
FAILED (errors=14, skipped=2)
exit_code=1
```

已显示的错误集中于 `test_research_workbench_ops`、`test_z002_run_data_boundaries`、`test_z003_run_identity_recovery` 的 PowerShell/进程/junction 子进程调用，触发：

```text
PermissionError: test isolation: unguarded child process denied
```

此外，多次后台 serve_forever 线程出现 `OSError: [WinError 10038]`，属于 socket 关闭生命周期异常；这些线程异常不一定被 unittest 主线程计入失败，必须单独修复与验收。两项 skipped 不能算通过；本报告不根据旧运行猜定本次每项跳过原因。

测试输出为本次工具会话记录，未重新执行来追求测试数量或全绿，未关闭守卫。这里是结果摘录，不伪称存在另一个已保存的完整原始日志。

### 6.2 子代理定向隔离测试

| 命令模式 | 本轮结果 | 限制 |
| --- | --- | --- |
| `--pattern 'test_kimi*.py'` | 88 tests，OK，exit 0 | 两个后台 socket 线程 WinError 10038；均 fake/临时路径/回环 |
| `--pattern 'test_dify_api*.py'` | 36 tests，OK，exit 0 | 合成凭据与事实包；不证明真实 Dify |

这些测试证明已有离线接口有可复用基础，不覆盖本报告提出的全部并发、版本、未知外发和生产接线缺口。

### 6.3 状态汇总

| 状态 | 本轮内容 |
| --- | --- |
| VERIFIED | HEAD/开始时干净工作树、源码路径与调用关系、宿主有限探测、上述测试确实执行 |
| UNVERIFIED | WSL 内 Docker、真实账号权益、真实模型/费用、股票终端、收藏库上游当前 CI |
| BLOCKED | 原生子进程测试被现有隔离守卫阻止，必须另在受控原生环境验收 |
| NOT_RUN | Linux 构建、Docker/Dify 部署、浏览器全流程、供应商 live、真人签收、机构启用 |

不执行 `run/agent-run/watch`、Pi live 或正式浏览器创建任务来“证明能用”。业务 Go/No-Go 没有被修改。

## 7. 外部资料核验范围

使用官方文档核对可变能力：

- [Dify Compose](https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose)：部署构成/要求；不是本机启动证据。
- [Dify Human Input](https://docs.dify.ai/en/self-host/use-dify/nodes/human-input)：反馈节点与身份限制。
- [n8n MCP Server Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-langchain.mcptrigger)：客户端连接、传输与多副本约束。
- [n8n queue mode](https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode.md)：worker/Redis/数据库职责。
- [Kimi 官方插件](https://www.kimi.com/code/docs/kimi-code-cli/customization/plugins)：Datasource 产品能力、只读与额度；不推出独立调用授权。
- [iFinD 接口](https://quantapi.51ifind.cn/gwstatic/static/ds_web/quantapi-web/download.html)：SDK/HTTP 可选性。
- [迅投 xtdata](https://dict.thinktrader.net/nativeApi/xtdata.html)：MiniQMT 交互依赖。
- [MCP 安全规范](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)：认证受众、token 透传、SSRF 等边界。

本轮未为了参考而安装或执行 F 盘任何项目。代码许可证与数据使用授权分开判断，GitHub stars/README 宣称/测试文件存在均不算本机验收。

## 8. 下一步

GLM 从 [新开工入口](../GLM_START_HERE_20261020.md) 开始。先做 R00/R01 的离线修复，再按 [任务书](../GLM_REFACTOR_TASKBOOK_20261020.md) 顺序推进。当前最有价值的成果是一条真实个人研究闭环；增加平台、连接器和角色都要服务于这条链。
