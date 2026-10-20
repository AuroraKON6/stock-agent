# GitHub 参考项目与复用矩阵

日期：2026-10-20。适用项目：`E:\stock agent`。参考库：`F:\GitHub项目`。

本矩阵汇总两路只读审查：金融投研参考和平台/MCP/研究工程参考，共 14 个本地仓库，以及官方 MCP Python SDK 候选。目标是减少基础设施重复开发，同时保留本项目的案件、任务、证据、预算和真实人工决定边界。

建议优先借鉴 OpenBB 的数据适配分层、Local Deep Research 的并发与网络防护测试、LangGraph 的存储契约、GPT Researcher 的研究质量评估、Vibe-Trading 的容器与 MCP 测试。Dify 作为成品部署候选；MCP 协议层优先使用固定版本的官方 SDK。

配套：[架构蓝图](GLM_DOCKER_RESEARCH_BLUEPRINT_20261020.md)、[R00—R28 任务书](GLM_REFACTOR_TASKBOOK_20261020.md)。

## 1. 证据范围与采用等级

本轮检查了本地 README、许可证、实际源码、测试文件和 CI 配置；平台审查另在线核实了官方 MCP Python SDK。没有安装或运行这些项目，没有运行其测试，没有查询真实行情、调用模型或连接真实账户，也没有修改收藏库。

因此：

- **VERIFIED STATIC**：相关文件、代码结构、许可证文本及测试/CI 定义在审查时存在。
- **NOT_RUN**：上游测试、本机安装、容器启动、实际供应商、模型和 A 股业务适配。
- **UNVERIFIED**：固定 commit 的最近上游 CI 是否通过、宣传的生产性能、数据权限及本项目下的长期稳定性。

“存在测试”不等于“测试已通过”；“上游自称已用于生产”不等于本项目已验证；宽松源码许可证不授予金融数据查询、缓存、外发或再分发权利。

采用等级：

| 等级 | 含义 | 升级为正式采用的条件 |
| --- | --- | --- |
| `DIRECT_DEPENDENCY_CANDIDATE` | 成品或协议库直接依赖候选 | 固定版本/摘要、许可证登记、依赖审查、隔离验收、可回退 |
| `NARROW_REFERENCE` | 借鉴窄模块、接口或测试方法 | 明确源文件、复用方式、差异、目标行为及本项目验收 |
| `DEFER` | 暂不作为运行时依赖 | 出现真实需求并证明收益后重评 |
| `DEV_TOOL_CANDIDATE` | 隔离开发/验收工具候选 | 固定版本、临时状态、最小能力、独立权限边界 |

复制代码、改写代码、参考设计和仅引用测试场景必须分开登记。含复合或额外条件的许可证继续按根 `AGENTS.md` 保持 `REVIEW_REQUIRED`；本矩阵不代替法律/合规批准。

## 2. 仓库总表

提交日期只说明本地 HEAD 的时间，不证明工作树干净、项目健康或最新 release。

| 编号 | 项目与本地目录 | 本地 HEAD | 许可证核查 | 采用等级 | 对应本项目 |
| --- | --- | --- | --- | --- | --- |
| REF01 | OpenBB；`openbq-org\OpenBB` | `78020ee4b864ab50b8c1f56373877c2b4e7fb623`；2026-10-18 | Apache-2.0 | NARROW_REFERENCE | R04/R11/R18/R19 |
| REF02 | Vibe-Trading；`HKUDS\Vibe-Trading` | `097b29feab9793e3c302e1a7a4f3c0dac6344904`；2026-10-19 | MIT | NARROW_REFERENCE | R08/R18/R23 |
| REF03 | AI Hedge Fund；`virattt\ai-hedge-fund` | `4e1657ddc689e3939c481a8f0955444b10f02dc9`；2026-10-19，v2.6.0 | MIT | NARROW_REFERENCE | R05/R13/R24/R28 |
| REF04 | ValueCell；`ValueCell-ai\valuecell` | `9793e9c0563fbf56fc096757d8bb80e209ac7aab`；2026-02-10 | Apache-2.0 | NARROW_REFERENCE | R14/R20/R22 |
| REF05 | VeighNa；`vnpy\vnpy` | `c6e231caf32b7fc97e6459817fff66458cf7e7c4`；2026-10-06 | MIT | NARROW_REFERENCE | R05/R21 |
| REF06 | OpenStock；`Open-Dev-Society\OpenStock` | `2e0ad992a53a4cd9eeb6d4fac58f5bebefe23a5a`；2026-10-19 | AGPL-3.0，REVIEW_REQUIRED | NARROW_REFERENCE，仅产品交互；代码采用暂缓 | R14/R23 |
| REF07 | stock-analysis；`wkingnet\stock-analysis` | `814f1d4db3a7ecac0b21f61e6ea6214a9ea73742`；2026-01-02 | MPL-2.0，REVIEW_REQUIRED | DEFER，待软件选择 | R21 |
| REF08 | Dify；`langgenius\dify` | `d446b520a3b5721bd9c1651c06e6435d4c791087`；2026-10-20 | 修改版 Apache-2.0，附加条件，REVIEW_REQUIRED | DIRECT_DEPENDENCY_CANDIDATE，使用另取的干净固定 release | R08/R16/R17 |
| REF09 | LangGraph；`langchain-ai\langgraph` | `1400d25b7b88cfa055c55b8a82bd95b7c25caef1`；2026-10-19 | MIT | NARROW_REFERENCE | R01/R03/R09/R26 |
| REF10 | GPT Researcher；`assafelovic\gpt-researcher` | `3582a6841fe7dc801e2a34e34ee408641aea9bce`；2026-10-10 | Apache-2.0 | NARROW_REFERENCE | R06/R17/R22/R28 |
| REF11 | Local Deep Research；`LearningCircuit\local-deep-research` | `683a967df20fe98ec0ddc99668041e22b95ff78b`；2026-10-19 | MIT | NARROW_REFERENCE | R03/R04/R08/R18/R25 |
| REF12 | Playwright MCP；`microsoft\playwright-mcp` | `ab6d67cbf8b619338f096980806fdbe29432cc4d`；2026-10-19 | Apache-2.0 | DEV_TOOL_CANDIDATE | R14/R18/R25 |
| REF13 | CrewAI；`crewAIInc\crewAI` | `d06c91401ff06c6731883f4a5a37b6cf9bfcc012`；2026-10-19 | MIT | DEFER，测试方法可参考 | R02/R18/R22 |
| REF14 | Windmill；`windmill-labs\windmill` | `5d0a8a9c15cd54032f930f8a21a0c2c3b517d60d`；2026-10-19 | 后端主要 AGPLv3，另有 Apache/专有及发行条件，REVIEW_REQUIRED | DEFER，故障场景可参考 | R09/R18/R26 |
| REF15 | 官方 MCP Python SDK；本轮筛选范围未发现本地副本 | 尚未固定 commit/release，实施时选择 | MIT；在线官方仓库核实 | DIRECT_DEPENDENCY_CANDIDATE | R18/R19 |

REF01—REF14 的目录均位于 `F:\GitHub项目`。源码采用前应验证选中文件属于登记 commit，并记录本地差异；仅有 HEAD 不能证明工作树文件等于该提交。

## 3. 收藏库完整性与来源处理

### 3.1 Dify 当前副本不能直接作为部署制品

平台审查时：

- `F:\GitHub项目\langgenius\dify\docker\docker-compose.yaml` 不存在。
- `git status --porcelain` 显示约 13,960 个 staged deletion、19 个 untracked 项。
- 根目录主要存在 `api` 等内容，缺少完整 `docker/web` 工作树。

确切原因未确定，可能与未完成检出或其他本地操作有关。不能直接断言仓库损坏，也不能擅自 reset、clean 或重新覆盖。

R16 应选定固定 release，取得干净隔离副本，并核对该 release 的 Compose、迁移说明、能力和 digest。本地 main 的新 commit 不自动覆盖本项目现有 `1.17.1` 候选锚。

### 3.2 LangGraph 的工作树差异

审查时显示 214 个修改，`git diff --ignore-space-at-eol --stat` 为空，支持“差异主要为行尾”的判断。仍应使用固定 commit 的清洁来源进行实际代码采用，不将本地差异直接纳入业务项目。

其他候选没有逐一完成全部 dirty 状态审查；本矩阵不保证它们的工作树完全一致。来源登记与验收应针对具体文件，不能只登记仓库名称。

## 4. 金融投研参考的具体借鉴方式

### REF01 OpenBB

**最适合借鉴：数据适配器和多接口共用服务。**

- [Fetcher](</F:/GitHub项目/openbq-org/OpenBB/openbb_platform/core/openbb_core/provider/abstract/fetcher.py:35>) 将请求转换、供应商提取、结果转换分开。可用于 R04/R11 的 Kimi、文件导入与后续终端 adapter：先验证请求，再受控调用，再归一化。
- [MCP 路由测试](</F:/GitHub项目/openbq-org/OpenBB/openbb_platform/extensions/mcp_server/tests/app/test_route_exposure.py:144>) 可用于 R19 的显式路由暴露和隐藏内部参数。HTTP 与 MCP 复用同一服务，不分别创建任务库。
- 已看到 `core/tests/provider/abstract/test_fetcher.py`、`test_fetcher_streaming.py`，以及 MCP auth、route exposure、artifact 测试。CI 定义含 Windows/Linux/macOS 和多 Python 版本矩阵；本轮未执行。

不照搬其完整 ODP 作为第二业务事实源，也不因其 provider 可查询就认定本项目有数据权限。[MCP auth](</F:/GitHub项目/openbq-org/OpenBB/openbb_platform/extensions/mcp_server/openbb_mcp_server/app/auth.py:10>) 的简单认证包含 base64 用户名密码 Bearer、空 scope/无到期及可不启用认证的配置，不能替代本项目的 case/scope/actor 授权。

**接受标准：** Fake、Kimi、文件源采用同一标准化契约；权限拒绝、空数据、字段缺失、陈旧、未来时间分别返回；REST/MCP 同意图得到同一任务/回执。对应 A18—A25、A51—A54、A86—A89。

### REF02 Vibe-Trading

**最适合借鉴：容器构建与 MCP 假服务器回归。**

- [Dockerfile](</F:/GitHub项目/HKUDS/Vibe-Trading/Dockerfile:4>) 可参考构建分层、依赖锁/hash、非 root 和固定镜像。
- [Streamable HTTP 测试](</F:/GitHub项目/HKUDS/Vibe-Trading/agent/tests/test_mcp_streamable_http_integration.py:48>) 可转化为 R18 的本地 fake MCP server：工具发现过滤、不可达、断连、错误分类和会话隔离。
- 已看到 `.github/workflows/test.yml` 的 Python/前端测试与 Docker build CI，以及 `test_mcp_*integration.py`、provenance、loader health、artifact 评估测试；本轮未执行。

不整包引入 Agent、策略执行、交易连接器和 swarm。Compose 中 SETUID/SETGID、写 `.env`、frontend profile 运行时 `npm install` 等做法不符合本项目候选部署要求。工具名带 research/read 也可能消耗外部额度或写缓存，必须单独登记副作用。

**接受标准：** 镜像无秘密、运行时不安装依赖、只读 rootfs；MCP 每个付费请求有唯一 intent/attempt，工具未配置不可见。对应 R08/R18/R23，A37—A40、A80—A85。

### REF03 AI Hedge Fund

**最适合借鉴：时点快照与研究回放。**

- [时点数据测试](</F:/GitHub项目/virattt/ai-hedge-fund/hedge_fund/data/test_client_contract.py:109>) 可借鉴披露时间和 as-of 约束，落到 R05/R13/R28。
- [账本测试](</F:/GitHub项目/virattt/ai-hedge-fund/hedge_fund/paper/test_ledger.py:29>) 可参考 checkpoint 损坏、链重放和事实源/缓存区分，落到 R24。
- 已看到 `hedge_fund/features/test_snapshot.py` 等具体负向测试；本地未见 `.github/workflows`，本轮未运行测试。

只借鉴快照与验证，不引入其 portfolio、broker 或 paper trading 范围。美股/美元/纽约交易日语义需换为本项目规范；其 rewind 等功能不能照搬来改写本项目追加历史。

**接受标准：** 截止日后发布的更正财报不进入历史研究；同一固定快照可重放；内容变化生成新版本；漏链、错序、错误 checkpoint 明确被发现。对应 A26—A33、A62 及 R24/R28 专项。

### REF04 ValueCell

**最适合借鉴：个人研究交互与任务等待。**

- [任务模型](</F:/GitHub项目/ValueCell-ai/valuecell/python/valuecell/core/task/models.py:10>) 可参考会话、任务、等待输入和完成状态的区分。
- 数据 adapter 能力矩阵、没有 embedding 时的降级体验适合 R14/R20。
- 已看到 `core/task/tests/`、`core/event/tests/`、`core/coordinate/tests/`。`.github/workflows/python.yml` 跑 core 测试和 diff coverage；research-agent 测试存在，但不在所见 core CI 路径中。

不引入 Agno+A2A+新 TaskStore，避免第二调度器。README 关于 A 股深度研究上线的表述指向其云产品，不能推导本地该快照有同等功能。

**接受标准：** 用户可从一条公告追问、补证、看新旧版本并签收；模型未主动提问时仍可续研；没有 embedding 仍能访问证据和研究历史。对应 R14/R20/R22。

### REF05 VeighNa / vnpy

**最适合借鉴：行情数据对象和只读 adapter。**

- [datafeed 抽象](</F:/GitHub项目/vnpy/vnpy/vnpy/trader/datafeed.py:14>)、Tick/Bar/HistoryRequest 对象可辅助 R05/R21 设计支持市场、频率、字段、交易所的显式能力表。
- 已看到 `tests/test_object.py`、`test_data_services.py`、`test_event.py`；CI 定义 Windows Python 3.13 下 lint/mypy/pytest/build。本轮未执行，也不能当作本项目 Python 3.12/Linux 兼容证明。

不整包引入 PySide6/Qt/TA-Lib/交易引擎。其内存线程事件总线不是本项目持久 TaskStore、outbox 或预算系统的替代物。

**接受标准：** 不支持的市场/频率与数据为空分开；时间、单位、复权完整；桥不公开订单工具。软件品牌和权限确定后再进入 R21。

### REF06 OpenStock

**适合参考：个人首页和关注列表；代码采用暂缓。**

- 已看到关注列表、市场时钟、新鲜度显示及来源说明界面。
- `__tests__/market-clock.test.ts`、`alert-actions.test.ts`、Vitest 配置存在；本地仅见 Docker image CI，未确认这些测试被 CI 调用。
- `MARKET_SUPPORT.md` 自述免费源主要支持美股/加密报价，没有本项目 A 股验收证据。

AGPL-3.0 代码复制/组合继续 REVIEW_REQUIRED，先借鉴交互思想。[DataFreshness](</F:/GitHub项目/Open-Dev-Society/OpenStock/components/DataFreshness.tsx:17>) 可根据 `isRealtime` 配置显示 `Live · 15s`；本项目必须由真实行情时间、交易时段、来源权利及新鲜度门禁决定“实时”，不能用 UI 刷新频率替代。

**接受标准：** 今日、自选、案件状态有清晰信息层次；ready/empty/stale/failed 分开；技术详情可展开。对应 R14/R23。

### REF07 stock-analysis

**仅在最终选择通达信时作为文件格式研究线索。**

- [解析函数](</F:/GitHub项目/wkingnet/stock-analysis/func.py:34>) 涉及 `.day`、GBK、证券代码、量额单位与复权，可辅助 R21 的只读导入。
- 本地有解析代码和示例，未见自动化测试/CI；本轮未执行。
- `readTDX_lday.py:27-46` 含 `del` 参数，可递归清理配置目录，不能运行原脚本作为导入验收。

MPL-2.0 和数据使用权仍需核对。只实现独立解析候选，在临时目录验证文件长度、字节序、前导零、编码、单位、复权和时间窗口。不要复制其目录清理逻辑。

## 5. 平台与研究工程参考的具体借鉴方式

### REF08 Dify

**作为成品部署候选，不复制其整套业务数据模型。**

主要复用工作流编辑、版本化 DSL、服务 API、固定 release 的官方 Compose 和工具接入能力。项目继续保留 TaskStore、数据权利、预算、证据和人工身份权威。

本地副本完整性问题见第 3 节。R16 不应从当前缺检出的工作树直接部署，也不能因为 main 更新就自动升级现有 `1.17.1` 候选。固定 release 后重新核验 Human Input、API、MCP、插件、网络和迁移能力。[Dify 官方仓库](https://github.com/langgenius/dify)

许可证含多租户和前端标识条件，未来多人单团队与多机构 SaaS 的使用边界分别登记。现有项目 DF-01/DF-02 的本地 fixture 证据也不能替代 Dify 实例验收。

**接受标准：** R16 整链模式只创建/查询同一业务任务；R17 角色模式每次推理经后端治理，同任务 Python 整链不再推进。对应 A71—A79。

### REF09 LangGraph

**借鉴存储接口和故障测试，暂不引入第二研究运行时。**

- [SqliteSaver](</F:/GitHub项目/langchain-ai/langgraph/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py:100>) 创建 `threading.Lock()`，并在 `cursor()` 中串行化连接访问；这可作为 R03 当前共享 SQLite 连接问题的对照。
- [checkpoint conformance](</F:/GitHub项目/langchain-ai/langgraph/libs/checkpoint-conformance/README.md>) 可借鉴“所有存储实现跑同一组契约”。本项目 SQLite 和未来 PostgreSQL 应同测版本、幂等、事务、outbox、取消、fencing 和恢复。
- [恢复测试](</F:/GitHub项目/langchain-ai/langgraph/libs/langgraph/tests/test_pregel.py:891>) 包含 pending writes resume、checkpoint recovery、并行 interrupt 等场景；本轮未执行。

其 SQLite 实现本身也面向轻量同步使用。LangGraph checkpoint 不能证明外部计费 exactly-once，也不能替代本项目 business_intent_id、input_version 和 attempt 事实。

**接受标准：** 对同一任务双连接争用、旧 worker 提交、恢复和取消都有确定结果；未来 PG 与 SQLite 契约等价。对应 R01/R03/R09/R26。

### REF10 GPT Researcher

**借鉴质量评测和有限修订，保留 A 股证据标准。**

- [quality_eval](</F:/GitHub项目/assafelovic/gpt-researcher/evals/quality_eval/README.md>) 及 metrics/suite/perturbation 将无依据声明、子问题覆盖、来源权威性、多样性与引用使用分开评价。
- 删除引用、破坏声明等扰动可用于 R06/R28，避免只看报告长度或 Agent 数。
- [tests.yml](</F:/GitHub项目/assafelovic/gpt-researcher/.github/workflows/tests.yml>) 区分 import、collect、offline unit，设置 `GPTR_BLOCK_NETWORK=1`，单列 live；`tests/test_multi_agents_revision_caps.py` 等限定修订轮次。

其质量说明明确引用指标偏结构性，不能自动证明某句话确实由引用支持。本项目需增加公告一手性、财务期间、发布时间、单位、复权和声明级人工抽查。不要复制其测试子进程方式去绕过本项目隔离入口。

**接受标准：** 数值/日期/来源错误注入能触发拒收或评分下降；修订与研究循环有预算和次数上限。对应 R06/R17/R22/R28。

### REF11 Local Deep Research

**优先学习网络防护、实际竞争测试与发布镜像验收。**

- [safe_requests.py](</F:/GitHub项目/LearningCircuit/local-deep-research/src/local_deep_research/security/safe_requests.py>) 配合 `dns_pinning.py`、`ssrf_validator.py`，处理 DNS 固定、重定向、字节上限和 URL 脱敏，适合 R18 的出站网关。
- [原子 claim 测试](</F:/GitHub项目/LearningCircuit/local-deep-research/tests/connected/test_queue_atomic_claim.py:26>) 用两个 processor、不同 DB 连接和 barrier 形成真正竞争，可转化为 R03/R04 的任务、授权核销和预算验收。
- [发布镜像 Compose 冒烟](</F:/GitHub项目/LearningCircuit/local-deep-research/.github/workflows/compose-published-smoke.yml>) 区分源码构建镜像与实际发布镜像，适合 R08/R25。

不要照搬默认 1 GiB 的最大响应或其 Python 3.14/大量依赖。本项目按公告、新闻、表格单独设更小边界，先维持锁定 Linux Python 3.12。参考代码仍需依赖与许可证登记。

**接受标准：** DNS/重定向无法绕过目的地校验；两个连接同抢一个任务仅一个分派；发布的镜像与交付 Compose 一致。对应 A11—A12、A18—A19、A37—A40、A83—A84。

### REF12 Playwright MCP

**用于隔离开发验收，必要时作为受控公开页面补证工具。**

- [README](</F:/GitHub项目/microsoft/playwright-mcp/README.md:410>) 明确 allowed/blocked origins 不是安全边界，且不控制重定向；不能据此放任 Agent 浏览内网、调用终端或读取现有 Cookie。
- [CI](</F:/GitHub项目/microsoft/playwright-mcp/.github/workflows/ci.yml>) 定义 Windows/macOS/Linux 和 Docker 测试；`tests/core.spec.ts`、`capabilities.spec.ts`、`cli.spec.ts` 可参考。
- 本地 package 为 `0.0.83`，依赖含 Playwright alpha 版本，不能直接成为本项目稳定基线。[官方仓库](https://github.com/microsoft/playwright-mcp)

使用临时工作区、独立 profile、固定版本和受限网络。对公开页面补证时另设精确 URL、下载大小、时限、文件隔离和来源权利；不把浏览器自动化作为金融数据主通道。

**接受标准：** SSE 重连、表单、全文/引用和版本交互在隔离真实浏览器通过；工具无法访问未批准数据。对应 R14/R18/R25。

### REF13 CrewAI

**运行时暂缓，MCP 与人工反馈测试方法可借鉴。**

已看到 `lib/crewai/src/crewai/flow/persistence/base.py`、`sqlite.py`、`tests/test_flow_resumability_regression.py`，以及 `tests/mcp/test_http_transport_auth.py` 等鉴权和恢复测试。本轮未执行。

其 MCP client 有自身重试机制，直接嵌入可能与本项目“已外发但结果未知不盲重试”冲突。当前没有必要再引入 CrewAI 的任务流和持久化模型。

**接受标准：** 只移植需要的失败场景和契约；若未来拟作为依赖，必须证明它无法绕过 R02 的 attempt 和预算。对应 R02/R18/R22。

### REF14 Windmill

**暂不部署，借鉴恢复与授权测试题库。**

可参考 `backend/tests/zombie_flow_recovery.rs`、`restart_flow_version_scope.rs`、`mcp_resource_authz.rs`、`mcp_token_exfil.rs`，分别用于 zombie 恢复、流程版本绑定、MCP 资源授权和 token 外泄防护。本轮未执行。

Windmill 与 Dify、Python 同时维护同一案件状态会增加所有权和恢复复杂度。源码和发行二进制许可条件需分别审查，不将其成品作为首发额外平台。

**接受标准：** 参考场景落到现有 TaskStore/网关测试，不增加第二数据库和调度者。对应 R09/R18/R26。

### REF15 官方 MCP Python SDK

协议层优先直接使用官方 SDK，项目只包装业务治理。平台审查在线核实其 MIT 许可、client/server 与 stdio、Streamable HTTP、SSE 支持，当前文档要求 Python 3.11+。[官方仓库](https://github.com/modelcontextprotocol/python-sdk)

本轮未固定版本、未下载、未安装、未运行。实施时必须固定 release/commit 和依赖哈希，核对 v1/v2 与目标服务器协议，不能直接从最新 README 复制 API 后声称适配完成。

R18/R19 的协议薄层继续调用项目的身份、工具白名单、scope、数据权利、预算和 TaskStore。SDK 认证能力不自动提供案件级授权，也不把工具 annotations 当权限事实。

**接受标准：** 每种实际采用的传输单独验收，工具清单漂移默认拒绝，token audience/撤销/越权通过负向测试；REST 与 MCP 对相同业务命令保持同一语义。

## 6. 最先学习的五组实现

学习顺序按当前缺口排列，实际编码仍遵守任务书依赖。

| 顺序 | 参考 | 学习产物 | 本项目交付 | 不增加的复杂度 |
| --- | --- | --- | --- | --- |
| 1 | LangGraph + Local Deep Research | SQLite 连接/事务责任、双连接竞争、统一存储契约场景 | R01/R03 的版本、幂等、授权核销和候选采纳 | 不更换 TaskStore，不引入图运行时 |
| 2 | OpenBB | 请求校验→供应商调用→归一化，HTTP/MCP 同源 | R04/R11 的 Kimi 主链与规范化结果 | 不安装整套数据平台 |
| 3 | GPT Researcher + AI Hedge Fund | 引用扰动、无未来信息、有限修订、快照回放 | R05/R06/R13/R28 的研究质量证据 | 不引入交易和投资组合 |
| 4 | Vibe-Trading + Local Deep Research | 构建锁、只读运行、fake MCP、发布镜像冒烟 | R08/R18/R25 | 不复制上游整套 Agent 与宽松部署参数 |
| 5 | 官方 MCP SDK + OpenBB 路由测试 | 标准传输与显式工具暴露 | R18/R19 的受控双向 MCP | 不手写 JSON-RPC，不增加 CrewAI/Windmill |

个人界面可同时参考 ValueCell/OpenStock 的交互思想，但正文事实链和身份状态仍由本项目实现和验收。

## 7. 第三方复用登记模板

建议 R00 新建候选登记，实际路径由实施者确认，例如 `config/candidates/refactor/third_party_reuse_manifest.json`。下列是模板，不是已实现配置，也不含真实批准：

```json
{
  "schema_version": "candidate-1",
  "entries": [
    {
      "reuse_id": "REFxx",
      "repository_url": null,
      "source_commit": null,
      "source_release": null,
      "source_files": [],
      "source_file_sha256": {},
      "source_worktree_verified_clean": false,
      "license_expression": null,
      "license_file_sha256": null,
      "notice_files": [],
      "license_review_status": "REVIEW_REQUIRED",
      "reuse_mode": "design_reference",
      "target_work_packages": [],
      "target_files": [],
      "reason": null,
      "copied_or_adapted_lines": null,
      "local_modifications": [],
      "new_runtime_dependencies": [],
      "external_effects": [],
      "data_rights_separately_verified": false,
      "upstream_test_definitions": [],
      "upstream_ci_run_url": null,
      "upstream_ci_result": "UNVERIFIED",
      "local_acceptance_commands": [],
      "local_acceptance_evidence": [],
      "local_acceptance_status": "NOT_RUN",
      "rollback": null,
      "owner": null
    }
  ]
}
```

登记要求：

- `reuse_mode` 区分 dependency、copied_code、adapted_code、design_reference、test_scenario_reference。
- 复制/改写登记文件与行范围，保留版权、许可证和 NOTICE；不得只留仓库主页。
- 上游 CI 填具体 commit 对应的运行链接与结果；没检查就保持 UNVERIFIED。
- 本项目验收绑定当前源码、依赖、配置与命令；上游通过不能代替它。
- 把 HTTP/MCP 调用、付费查询、写缓存、浏览器下载等真实副作用写明。
- 授权、数据留存、模型外发与软件许可分别记录，不能用一个 approved 布尔值代替全部。
- 候选撤回时停用新依赖/接口，保留已产生的业务和审计历史。

## 8. 不采用与停止条件

以下情形不进入直接采用：

- 本地仓库检出不完整或存在未解释的修改，无法绑定准确源文件。
- 缺少许可证、许可证冲突或需要的审查未完成。
- 只能通过共享账号、桌面凭据、禁用安全检查或运行时安装才能使用。
- 框架接管本项目任务、预算、审批或证据事实源，形成双推进者。
- 自动重试不能纳入 attempt/预算，或外部调用结果未知时仍重发。
- 默认工具能交易、执行任意 shell、访问任意文件/URL，而无法在能力边界收窄。
- 没有真实用户收益，只增加连接器、Agent、模型调用或运维成本。

额外筛选到的 Ashare 本地 HEAD 为 `7ef1ce07579416d5c9e58f713867132bbaa9390d`，顶层未见 LICENSE；README 的“稳定运行数年”不是独立验收证据，因此未列入 14 个优先参考仓库，也不作为首批代码复制候选。

本矩阵的成果是可定位、可验证的复用路线。所有上游运行测试、实际安装及 A 股适配验收仍为 NOT_RUN；GLM 应在具体 Rxx 工作包中用最窄范围完成这些证明。
