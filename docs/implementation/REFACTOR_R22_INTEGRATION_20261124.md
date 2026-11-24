# R22 集成：有限多 Agent 与个人复盘（KONM-81）

日期：2026-11-24。范围：在有界任务图上接通 R17 角色链与 R18 候选，补齐个人复盘只读投影、规则候选与上表集成场景的 O/C 证据。真实供应商调用、真实传输与部署镜像不在本单（L 层 `NOT_RUN`）。

## 1. 基线与合流

| 项 | 值 |
| --- | --- |
| R17 候选（起始） | `2f18ce9312a7ffe0d714c58fbc9b00efecad500c`（草稿 PR #7 头） |
| R18 候选（合入） | `74d17ef7b5d07409215946c07cc28a81f09bf437`（KONM-79） |
| 合流提交（审查基线） | `08653f3`（`--no-ff`，两父提交，零冲突） |
| 工作分支 | `agent/claudecode/e405233085d8` |
| R22 变更 | 合流提交之上的独立提交（首轮 `279968d`；复验返工提交 SHA 见交付评论） |
| 来源分支 / `main` | 未改动；未推送任何来源分支 |

合流仅把 R18 的 28 个文件带进专用分支（含 R15 引用契约、R20 scope、R18 连接器）；R22 的代码差异全部位于合流提交之后，便于审查。

## 2. 改动

- `stock_agent/subtask_graph.py`
  - 新增 `depends_on`（兄弟角色）依赖审查：未知引用、自依赖、批内环、既有同名多义目标一律 `dependency_invalid`，整批零创建；合法依赖按拓扑序创建并把**已解析的任务 ID**写入子任务 `origin.depends_on`。
  - 新增 `cross_case`：提案显式携带其它案件时整批拒绝。
  - **重复先于预算**：批内重复与已存在同身份（父+角色+问题身份）重复先归并为 duplicate；纯重复提交即使父级预算已耗尽也返回 duplicate，零新增、零重复计量。预算只统计真正新增的预留。
  - `create_task` 返回 `DUPLICATE`（并发竞争落败）时归入 duplicates，不计为创建。
- `stock_agent/role_execution.py`
  - 角色步骤经同一受控入口 `propose_subtasks` 入队；每步 `budget_reserve=1`，共享根任务的 `budget_limit`（或显式参数）。被拒提案转成稳定的 `RoleFlowError: step_rejected: <reason>`，零子任务、零计量。
- `stock_agent/personal_review.py`（新）
  - 只读投影：任务关系（父/子/依赖边）、输入版本与不可变快照、规则绑定（flow id/version/digest、每角色 prompt digest）、角色与来源、引用（与**本次实际投递**的允许集合逐条比对）、状态/失败事件、attempt/usage/预算计量。
  - 归因三分：`model_roles` / `evidence_sources` / `human_signoffs`。模型角色名（`ALLOWED_SUBTASK_ROLES`）与自动化前缀不算人工；同一份证据字节（sha256）只算一个独立来源；只有人类 actor 写下的签收才算签收，自动化 actor 的“自称签收”进入 `rejected_automated_claims`。
  - 访问沿用既有裁决器：要求 `read_research` scope 与案件范围；根任务有服务端 owner 绑定的个人任务只允许该 owner 读取（`personal_owner_mismatch` / `personal_owner_required`）。只调用 store 的读接口，不写库。
- `stock_agent/rule_candidates.py`（新）
  - `propose_rule_change` 只派生新的冻结制品并标 `pending_review`，不注册、不批准、不改动基线 digest；`RuleCandidateBook.approve` 是唯一写注册表的路径，且拒绝任何自动化/模型角色审批人。
- `tests/test_refactor_r22_integration.py`（新，36 例）
  - 同一冻结合成事件双模式对照、复盘溯源、归因、身份/案件/只读、依赖/环/越案件/深度/扇出、重复与预算、并发提交（同身份收敛/不同角色争用上限）、终态中间节点的取消穿透、正常取消入口的子任务传播、投递账本对账、`provide_input`/`reject` 非签收、规则候选冻结与 digest 复核、父取消、部分失败。
- `deploy/dify/r17_drill.py` / `scripts/r17_role_drill.py`
  - 新增容器内探针 `review` / `r22-rule` / `r22-graph` 与 11 条 R22 检查，C 层在同一隔离栈、同一实时任务库上取证。

## 3. O 层：命令与结果

Python 3.12.15，仓库锁定 `pyyaml==6.0.3`。全部在仓库根目录执行：

| 命令 | 结果 |
| --- | --- |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22.py` | 9 OK / exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22_integration.py` | 52 OK / exit 0（本单专项，第六轮后） |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r17.py` | 12 OK / exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r17_role_api.py` | 19 OK / exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r18.py` | 34 OK / exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r15_citation_contract.py` | 5 OK / exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r10.py` | 11 OK / exit 0 |
| 追加回归：`test_refactor_r15_personal_facts.py` / `test_refactor_r20_scope.py` / `test_refactor_r16.py` | 分别 OK / 40 OK / 4 OK，exit 0 |

## 4. C 层：隔离容器演练

证据目录 `../r22-claudecode-run2`（仓库外），回环端口 **18185**，专用 Compose 项目 `stock-agent-r17-8072bbc59de5`，临时卷，Squid 限定五个 POST 路由。

```sh
python3.12 -B scripts/r17_role_drill.py all ../r22-claudecode-run2 --port 18185
```

- `summary.json`: **PASS，53/53**（R17 既有 42 项 + R22 新增 11 项），12 个 Dify run，0 FAIL，exit 0。
- `cleanup.json`: `compose_down_exit=0`，容器/网络/卷剩余 0，端口 18185 可重绑。
- 新增 R22 证据文件：`r22-review.json`、`r22-rule.json`、`r22-graph.json`。
- 既有 R17 证据（A76–A79、隔离网络、Squid 计数、账本对账）全部保持 PASS；`refs/pull/7/head` 对应的 R17 行为未被改变。

首次演练（`../r22-claudecode-run1-fail`）在产品行为全部正确的情况下报 1 项 FAIL：验收表达式把三类依赖拒绝（自依赖/环/未知引用）都期望成各自名字，实际稳定返回 `dependency_invalid`。修正的是检查映射，未改产品代码；随后重跑得到上述 53/53。该目录保留作对照。

### 4.1 复验返工后的独立重跑（run3 / run4 / run5 / run6）

返回 `in_progress` 修复后，用**新的**证据目录与回环端口重跑，仍经共享锁串行：

```sh
python3.12 -B scripts/r17_role_drill.py all ../r22-claudecode-run3 --port 18186
python3.12 -B scripts/r17_role_drill.py all ../r22-claudecode-run4 --port 18187
python3.12 -B scripts/r17_role_drill.py all ../r22-claudecode-run5 --port 18189
python3.12 -B scripts/r17_role_drill.py all ../r22-claudecode-run6 --port 18190
```

- 四次均为 `summary.json` **PASS，53/53**，12 个 Dify run，0 FAIL，exit 0。
- `cleanup.json`: `down --volumes` exit 0，残留容器/网络/卷均为 0，端口已释放（run4 `18187`，run5 `18189`，run6 `18190`）。
- run6 在第三轮返工的交付提交上重跑（工作区干净），是该轮 C 层的主证据；run5/run3/run4 为同轮前序记录。
- 证据：`r22-review.json`、`r22-rule.json`、`r22-graph.json`。
- 同轮把 Celery 就绪探测从 240s/80 行放宽到 420s/200 行（sol 首跑曾遇就绪超时，产品无变化），只影响等待窗口，不放宽任何断言。
- 该目录中的 R22 取消检查与 O 层回归共同覆盖：正常 `cancel_task` 入口传播、终态中间节点穿透（O 层）。

### 4.2 关键实测（run2）

- 复盘：4 个任务（根 + 三角色），根输入快照版本 `[1,2]`，规则 digest `d80fdd32…` 与演练 flow 一致；三个角色步骤的 prompt digest 与冻结制品逐一相等，attempt 各 1，引用越界集合为空；`model_roles=3`、`human_signoffs=0`、独立证据 0（本样本无投递原文）。
- 只读：投影前后任务 23→23、事件 119→119。
- 身份：外来案件 `case_out_of_scope`、无读权限 `scope_not_granted`、个人任务他人读取 `personal_owner_mismatch`、匿名 `personal_owner_required`、错案件 `case_out_of_scope`。
- 规则候选：`pending_review`，模型角色审批 `reviewer_not_human`，人类审批 `approved`；批准前后已批准 digest 均为 `d80fdd32…` 且仍可执行，候选在批准前 `flow_not_registered`。
- 图治理：越案件/放宽标的/未知角色/自依赖/环/未知依赖/超扇出/超深度/超预算全部 `refused` 且 `tasks_added=0`；合法依赖批创建 3 个并写入两条依赖边；重复批 `created=0, duplicates=3, tasks_added=0`；父取消 `count=4`，每个任务各有 1 条 `task_cancelled`。

## 5. 验收条款 → 证据

| 验收 | 证据 |
| --- | --- |
| 循环与边界 | `FanoutReviewTests.test_boundary_rejections_create_nothing`、`test_dependency_cycles_and_unknown_refs_refused`；C `r22-graph` 九类拒绝 `tasks_added=0`；角色步骤仅经 `propose_subtasks` 入队（`role_execution`）。 |
| 重复与预算 | `test_duplicate_submission_after_budget_exhaustion_meters_nothing`、`test_concurrent_identical_submissions_create_one_task`、`test_role_step_budget_exhaustion_creates_nothing`；C `duplicate_batch` / `budget_ok` / `budget_exceeded`。 |
| 父取消与部分失败 | `CancelAndPartialFailureTests` 两例（终态子任务输出/attempt 原样、非终态后代各自事件、兄弟与父状态不变、`step_failed_needs_decision`）；C `cancel`。 |
| 来源与复盘 | `DualModeTests.test_review_traces…`、`test_attribution_separates…`、`PersonalReviewAccessTests`、`RuleCandidateTests`；C `r22-review` / `r22-rule`。 |
| 回退 | `test_fallback_keeps_subtasks_results_and_metering_reconcilable`、`test_same_frozen_event_dual_mode…`（同一冻结输入、同一引用允许集合、同 schema，1 vs 3 次回放）；C `chain-*`、`A79` 回退段。 |

## 6. 边界与未验项

- **L 层 `NOT_RUN`**：全部为合成事件与回放，供应商请求 0、token/费用 0。真实模型用量、真实成本对比需 R17 供应商授权与独立额度（沿原单）。
- **R18 真实传输/部署镜像 `NOT_RUN`**：沿 KONM-79 记录，本单只合入其已验收候选。
- 复盘投影目前是库级只读入口，尚未暴露为 HTTP/UI 页面；接入需改 `dify_api_server.py`/看板等共享文件，与 KONM-80（R19）改动重叠，未在本单展开。
- C 层个人任务不属于真实 Dify 案件流；其 owner 隔离在容器内用同一实时库的合成任务取证。
- 演练首次失败为检查表达式误判（见 §4），非产品缺陷，证据目录保留。

## 7. 复现入口

```sh
# O + 本单专项
python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22_integration.py
# C（需 Docker；独立证据目录与空闲回环端口）
python3.12 -B scripts/r17_role_drill.py all <新证据目录> --port <空闲端口>
```

需要串行容器演练时，用共享锁包装（与本轮一致）：`fcntl.flock(/tmp/stock-agent-konm78-r17-2f18ce9.docker.lock)` 后执行上一条命令。

## 8. 复验返工记录（sol 的 10 项复现）

sol 在 `279968d` 上以 `focused_checks.py` 复现 10 项失败。逐项修复如下，均补了 O 层回归并在 `run3` / `run4` 重新取证。

| # | 失败项（sol 复现） | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | 并发不同角色可同时越过扇出/预算上限 | 父状态、去重、上限与创建之间没有互斥边界 | `propose_subtasks` 在持锁边界内用**新连接**重读父任务与子任务，再跑全部校验与创建；线程 RLock 按 DB+parent 键控，POSIX 下再加 `<db>.<parent>.subtask.lock` 的 `flock`，跨进程同样串行 |
| 2 | 同身份并发败者抛 `TaskIdempotencyConflictError` 而非 duplicate | 同上 | 边界内后到者从权威快照看到已创建子任务，按规范化 `(role, question)` 身份收敛为 `duplicate_child`，零新增零计量 |
| 3 | 重复项携带未知依赖仍被放行 | 依赖校验只覆盖去重后的 `unique_items` | 依赖审查改为遍历**全部规范化提案**（含重复项）；未知/自依赖/多义/环一律 `dependency_invalid` 整批零创建 |
| 4 | `cancel_subtree` 遇终态中间节点即停止下钻 | `continue` 跳过了子节点 | 终态节点不再取消自身，但仍把其子节点入栈；每个非终态后代各得一条 `task_cancelled` |
| 5 | 正常入口 `workflow_service.cancel_task` 只取消根 | 只调用一次 `store.request_cancel` | 根仍按调用方 `expected_version`/principal verification 取消，随后 `cancel_subtree(..., skip_root=True)` 传播到后代 |
| 6 | 复盘允许集合取自输入 payload，投递被丢弃的引用也算可用来源 | `personal_review` 读 `public_context.evidence_summaries` | 改为读 worker 的 `model_call.context_delivery`：`delivered_evidence` 为允许集合，`dropped_evidence`/未执行单列为 `undelivered_references`（带原因）；只有实际投递且 vault 校验通过的字节计入独立证据 |
| 7 | `provide_input` / `reject` 被计成人类签收 | `_signoffs` 把所有 `human_response_recorded` 当签收 | 仅 `personal_acknowledgement` 是签收；其他人工决定进入 `human_decisions`，`acknowledged_by_human=False` |
| 8 | `role-orchestrator:<credential>` 被误判为人工 | `actor_kind` 未剥离 `:<credential>` 后缀 | 以冒号前的基础身份判定自动化/人工；新增回归断言 |
| 9 | 规则候选返回浅拷贝，评审改副本即可改变已批准内容 | `candidate_flow`/`get`/`pending` 返回 `dict(...)` 共享嵌套对象 | 全部改为 JSON 深拷贝；批准时从冻结文本重算 digest，与提案时记录不一致则 `candidate_tampered` 拒绝 |
| 10 | C 层首跑 Celery 就绪超时、次跑 53/53 | 探测窗口偏窄 | 就绪探测 240s/80 行 → 420s/200 行；run3 一次通过 |

复验命令与结果：

| 命令 | 结果 |
| --- | --- |
| sol `focused_checks.py`（解压到仓库根，`PYTHONPATH=.:tests:scripts python3.12 -B focused_checks.py`） | 10/10 PASS，exit 0（该脚本为 sol 附件，未提交进仓库） |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22_integration.py` | 36 OK / exit 0 |
| R22 / R17 / R17 role API / R18 / R15 citation / R10 既有模式 | 9 / 12 / 19 / 34 / 5 / 11 OK，均 exit 0 |
| 相邻回归：R03 / R19 / R20 / R15 personal / R16 / R14 browser | 14 / 8 / 40 / 10 / 4 / 24 OK，均 exit 0 |
| C 层 `scripts/r17_role_drill.py all ../r22-claudecode-run4 --port 18187` | PASS 53/53，cleanup 残留 0，端口可重绑 |

新增回归（`tests/test_refactor_r22_integration.py`）：并发不同角色争用上限、同身份并发收敛、重复项未知依赖整批拒绝、终态中间节点取消穿透、正常取消入口传播、投递账本对账、`provide_input`/`reject` 非签收、orchestrator 凭据后缀自动化、候选冻结与 digest 复核。

`workflow_service.cancel_task` 不在 KONM-80（zcode）已推送分支的改动集中（其分支只动 R18 connector / 文档 / R18 测试）；`task_runtime/store.py`、`dify_api.py`、`dify_api_server.py` 本单未改动，因此无共享文件串行冲突。

## 9. 第三轮复验返工（sol 的 9 个反例 + 跨进程锁）

sol 在 `c2c22f1` 上以 `followup_checks.py` / `process_check.py` 复现 9 个反例，并确认正常 POSIX 锁下两进程预算约束有效。逐项修复：

| # | 失败项 | 修复 | 回归 / 证据 |
| --- | --- | --- | --- |
| 1 | 取消与创建竞态：晚到子任务 `queued`、取消事件 0 | 创建与取消共用同一 per-DB 持久化边界（RLock + `flock`），取消在根任务取消前先取边界；边界内重读父任务，已取消则零创建，已创建则随后被取消 | `test_cancel_waits_for_an_in_flight_child_creation`；followup `cancel_while_creating`（child cancelled, 1 event） |
| 2 | worker 在取消读写间领取子任务 → 两次 `TaskVersionConflictError` | 子节点取消在版本冲突时重读并重试（上限 8 次），对 leased 任务记录 `cancel_requested`；已请求的任务不重复写事件；根任务仍严格使用调用方 `expected_version` 与主体核销 | `test_cancel_absorbs_a_worker_claim_version_race`；followup `cancel_while_child_is_leased`（leased + cancel_requested，1 event） |
| 3 | `flock` 抛 `OSError` 时两边各创建 1 个 | 取不到跨进程锁即 `graph_lock_unavailable` 拒绝创建，绝不降级为仅进程内互斥 | `test_missing_cross_process_lock_refuses_creation`；process `unavailable_posix_flock`（children=0） |
| 4 | `TaskStore(read_only=True)` 被静默升级为读写并创建 | `propose_subtasks` 在任何写入/加锁前拒绝只读连接（`read_only_store`） | `test_read_only_store_refuses_creation`；followup `read_only_store_preserved` |
| 5 | `:memory:` 存储被关闭并写出锁文件 | 内存库只用进程内锁、不写锁边车；只关闭本函数新开的连接，调用方连接保持可用 | `test_in_memory_store_keeps_caller_connection_and_writes_no_sidecar`；followup `in_memory_store_preserved` |
| 6 | 重复项声明的依赖与批内/既有任务冲突仍创建 2 个 | 对每条规范化提案记录首个声明的依赖；批内重复或既有子任务的声明必须与其已记录依赖（按角色解析）完全一致，否则整批 `dependency_invalid`；精确重试仍是 duplicate | `test_duplicate_line_cannot_redeclare_a_conflicting_dependency`、`test_duplicate_of_existing_child_cannot_add_dependencies`、`test_dependency_batch_retry_stays_a_pure_duplicate`；followup `cycle_via_duplicate_line` |
| 7 | 规范化前 40 字符相同的两个合法问题触发幂等冲突 | 子任务幂等键使用完整规范化身份（不再截断） | `test_task_key_uses_the_full_normalized_question`；followup `full_question_identity`（2 tasks, 2 created events） |
| 8 | 只有准备态（无回放答案、无供应商调用）也计 2 条已投递 | 复盘按执行凭证分档：`sent`（live 调用返回）、`replayed`（消费回放）、`send_unknown`（结果未知）、`prepared_only`、`no_execution_record`；仅前两档认定允许集合与独立证据 | `test_prepared_context_without_execution_is_not_delivered`；followup `prepared_context_without_execution`（projected_delivered=0） |
| 9 | 纯合成输出字段被计作人工签收 | 只认 TaskStore 写入的 `personal_acknowledgement` 事件；输出字段无匹配事件时列入 `untrusted_output_claims`，不计归因 | `test_output_field_alone_is_not_a_signoff`；followup `signoff_requires_store_event` |

复验命令与结果：

| 命令 | 结果 |
| --- | --- |
| sol `followup_checks.py`（仓库根，`PYTHONPATH=.:tests:scripts python3.12 -B followup_checks.py`） | 9/9 PASS，exit 0 |
| sol `process_check.py`（仓库根，`PYTHONPATH=.:scripts python3.12 -B process_check.py`） | 2/2 PASS（正常 flock 1 创建；锁异常两边拒绝），exit 0 |
| 上轮 `focused_checks.py` | 10/10 PASS，exit 0 |
| `test_refactor_r22_integration.py` | 36 OK / exit 0 |
| R22 / R17 / R17 role API / R18 / R15 citation / R10 | 9 / 12 / 19 / 34 / 5 / 11 OK，均 exit 0 |
| 相邻 R03 / R19 / R20 / R15 personal / R16 / R14 browser | 14 / 8 / 40 / 10 / 4 / 24 OK，均 exit 0 |
| C 层 `scripts/r17_role_drill.py all ../r22-claudecode-run6 --port 18190` | PASS 53/53，cleanup 残留 0，端口可重绑 |

边界说明：创建与取消现在全库串行（同一任务库的图变更），这是让预算/扇出/重复/取消在同一持久化边界内成立的代价；跨进程互斥依赖 POSIX `flock`，不可用时拒绝而非降级。`task_runtime/store.py`、`dify_api.py`、`dify_api_server.py` 仍未改动。

## 10. 第四轮复验返工（sol 的 6 个追加反例）

sol 在候选 `872312b` 上用 `acceptance_edges.py` 复现 6 个反例（exit 1）。逐项修复：

| # | 失败项 | 修复 | 回归 / 证据 |
| --- | --- | --- | --- |
| 1 | 已 `cancel_requested` 的父（leased/running）仍接受新子任务提案；checkpoint 后子任务仍 `queued`、0 事件 | `propose_subtasks` 的权威入队（边界内新连接）与预检都沿父链向上检查 `cancel_requested`：父或任一祖先已请求取消即 `parent_cancel_requested` 整批零创建；对 leased/running 路径同样生效 | `test_proposal_refused_after_cancel_request_on_a_running_parent`、`test_proposal_refused_under_a_cancelled_ancestor`；acceptance `no_new_work_after_cancel_requested`（children=0） |
| 2 | 部分取消无法续跑：子节点租约续期让 8 次重试都 `cancel_conflict`；根已终态，带新版本重试只能 `TaskTransitionError` | 统一入口 `subtask_graph.cancel_task_tree`：根取消先由 store 自身事务完成（actor/principal receipt/`expected_version`/终态拒绝不变，未授权或过期调用零变更），再在**同一持久化边界**内传播后代。根已 `cancelled` 且调用方持有当前版本时，重复调用走**恢复路径**继续传播（不再写/重复发根事件）；`_cancel_node` 天然跳过终态与已请求节点 | `test_partial_cancellation_resumes_with_the_current_root_version`（8 次续期冲突后根已取消、子未标；带新版本重试收敛，子 1 事件、根仍 1 事件）；acceptance `cancellation_can_resume_after_partial_failure` |
| 3 | 个人工作台入口 `cancel_personal_task`（及 CLI `task cancel`）只取消根，子任务仍 `queued`、0 事件 | 两个正常入口改为调用 `cancel_task_tree`；工作台的会话归属、`expected_version`、`cancel_terminal` 前置守卫保持不变 | `test_personal_workspace_cancel_cancels_the_children`（含过期版本零变更、重复取消仍报 `cancel_terminal`）；acceptance `personal_workspace_cancel_propagates`（child cancelled, 1 event） |
| 4 | 缺少 `fcntl` 时普通单任务取消被 `graph_lock_unavailable` 阻断 | `creation_boundary(require_file_lock=False)` 供取消使用：进程内 RLock 必取，平台有可用 `flock` 时照常取，取不到才退化为进程内互斥。创建仍要求跨进程锁（无 `fcntl` 时创建被拒），因此不存在可竞争的跨进程创建者 | `test_single_task_cancel_survives_missing_fcntl`（无 `fcntl` 取消成功且 1 事件；创建仍 `graph_lock_unavailable`）；acceptance `single_task_cancel_without_fcntl`；process `unavailable_posix_flock` 仍全拒 |
| 5 | 首轮确定性 401：账本 `failed_known/sent=yes`，复盘却因全局 `called=false` 判为 `prepared_only` | worker 每轮把该轮**实际外发上下文**写入 `model_call.context_rounds`（含 delivery、outbound 与 prompt hash）；复盘按轮把上下文与同名 `round_index` 的 invocation 记录绑定，用结构化 `sent` 判档：`yes`→`sent`、`unknown`→`send_unknown`、无 attempt→`prepared_only` | `test_projection_uses_the_invocation_ledger_on_a_known_failure`（401 后 `delivery_state=sent`，`delivery_rounds` 绑定 attempt_id）；acceptance `delivery_uses_invocation_sent_even_when_no_success_response` |
| 6 | 首轮成功 + 次轮发送未知：全局 `called=true` 把最后上下文标成 `sent` | 同上：判定取**最新上下文轮**对应的 invocation `sent`，早轮成功不覆盖末轮 `unknown`；未知档不把任何引用计为已投递 | `test_latest_unknown_send_survives_an_earlier_successful_round`（末轮 `send_unknown`，`delivered_evidence={}`，`delivery_rounds=[yes,unknown]`）；acceptance `last_context_keeps_unknown_send_after_successful_round` |

复验命令与结果：

| 命令 | 结果 |
| --- | --- |
| sol `acceptance_edges.py`（`PYTHONPATH=.:tests:scripts python3.12 -B <解压目录>/acceptance_edges.py`，仓库根为 cwd） | 6/6 PASS，exit 0（该脚本为 sol 附件，未提交进仓库） |
| sol `focused_checks.py` / `followup_checks.py` / `process_check.py` | 10/10、8/8、2/2 PASS，均 exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22_integration.py` | 44 OK / exit 0 |
| R22 / R02 / R03 / R17 / R17 role API / R18 / R15 citation / R10 | 9 / 18 / 14 / 12 / 19 / 34 / 5 / 11 OK，均 exit 0 |
| 相邻 R01 / R14 / R20 / R20 scope / R15 personal / task detail / first product slice / fault matrix | 21 / 11 / 8 / 40 / OK / 6 / 18 / 6 OK，均 exit 0 |
| C 层 `scripts/r17_role_drill.py all ../r22-claudecode-run7 --port 18192`（共享锁内串行；run8/18193 为最终代码上的复跑） | PASS 53/53，cleanup 残留 0，端口可重绑 |

环境说明：`test_refactor_r15_w10_skeleton.py` 与 `test_summary_review.py` 在本机默认 `TMPDIR`（`/var/folders/...`，`/var` 为符号链接）下因 `evidence_excerpts` 的 reparse-point 守卫报错；`TMPDIR=/private/tmp` 下分别 1 OK / 23 OK（skipped=1）。`test_acceptance_fixes_20260921*.py` 需要未入库的 `config/watchlist.local.json`，导入即失败。以上均与本单改动无关。

边界说明：取消入口统一为 `cancel_task_tree`（workflow service、个人工作台、CLI），根取消仍是 store 的原子授权与版本检查，后代传播在其后、同一持久化边界内完成；根已取消时重复调用只在调用方持有当前版本时继续传播，且每个后代仍只有 1 条取消事件。取消不再要求可用的 `fcntl`（创建仍要求）。`task_runtime/store.py`、`dify_api.py`、`dify_api_server.py` 未改动，与 KONM-80 无共享文件冲突。


## 11. 第五轮复验返工（sol 的 4 个补充反例）

sol 在候选 `0d6b485` 上用 `recovery_edges.py` 复现 4 个反例（exit 1）。逐项修复：

| # | 失败项 | 修复 | 回归 / 证据 |
| --- | --- | --- | --- |
| 1 | 创建进程持真实 POSIX 锁并暂停在校验与写入之间，仅对取消进程注入 `flock` 错误：取消返回成功、根 `cancelled`，随后持锁进程创建 1 个 `queued` 子任务 | `creation_boundary(require_file_lock=False)` 的退化范围收窄为**仅 `fcntl` 模块不存在**（Windows）：创建在该平台同样被拒，故无跨进程创建者可竞争。`fcntl` 存在而 `flock` 报错时一律 `graph_lock_unavailable` 失败关闭，取消绝不单独越过互斥 | `test_cancel_fails_closed_when_the_file_lock_errors`（取消零事件、根仍 `queued`；创建同样拒绝）；recovery `cancel_lock_failure_does_not_race_a_locked_creator`（`cancel_result=graph_lock_unavailable`、parent=queued）；既有 `single_task_cancel_without_fcntl` 仍 PASS |
| 2 | 个人工作台部分取消后，以根最新版本从同一入口续跑被 `cancel_terminal` 拒绝，子仍 `leased` | `cancel_personal_task` 只对 `succeeded/failed/blocked/expired` 保持 `cancel_terminal`；根已 `cancelled` 时接受当前版本并交给 `cancel_task_tree` 完成幂等传播 | `test_workspace_entry_resumes_an_interrupted_cancellation`；`test_workspace_cancel_still_refuses_finished_roots`（已完成仍拒绝）；recovery `workspace_entry_resumes_partial_cancellation` |
| 3 | 受保护恢复成功传播并标记子任务，但恢复凭证的 `used_at` 为空（终态异常把本次核销一并回滚，传播随后独立提交） | 恢复路径在触碰任何后代前，用 `store.claim_principal_verification` 在**独立事务**中核销恢复凭证（失败即 `PrincipalVerificationError`）；同一凭证的 `already_claimed_same_command` 视为首次已提交授权的延续。单命令凭证约束不变 | `test_protected_resume_consumes_its_own_receipt`（两张凭证 `used_at` 均非空）；recovery `protected_recovery_consumes_its_receipt`（`initial/recovery_receipt_consumed=true`） |
| 4 | 同秒重试：首轮已发送后返回无效 JSON 重排队，重领后预算拦截；唯一 invocation 属旧租约，但复盘仍凭同轮次 + 同秒报 `sent` | `context_rounds` 记录本轮**租约身份**（`worker_id` + `fencing_token`）；复盘仅接受显式 `attempt_id` 或同一 worker + fencing token 的 invocation，删除秒级时间回退；无租约绑定的记录不匹配任何 invocation | `test_same_second_requeue_never_borrows_the_prior_lease_send`（`prepared_only`、`delivered_evidence={}`）；recovery `same_second_retry_never_borrows_prior_send`；上轮 `test_context_round_never_pairs_with_an_earlier_runs_attempt` 仍 PASS |

复验命令与结果：

| 命令 | 结果 |
| --- | --- |
| sol `recovery_edges.py`（`PYTHONPATH=.:tests:scripts python3.12 -B <解压目录>/recovery_edges.py`，仓库根为 cwd） | 4/4 PASS，exit 0（脚本为 sol 附件，未提交进仓库） |
| sol `acceptance_edges.py` / `focused_checks.py` / `followup_checks.py` / `process_check.py` | 6/6、10/10、8/8、2/2 PASS，均 exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22_integration.py` | 49 OK / exit 0 |
| R22 / R02 / R03 / R17 / R17 role API / R18 / R15 citation / R10 | 9 / 18 / 14 / 12 / 19 / 34 / 5 / 11 OK，均 exit 0 |
| 相邻 R01 / R14 / R20 / R20 scope / R15 personal / research rounds / fault matrix | 21 / 11 / 8 / 40 / 10 / 10 / 6 OK，均 exit 0 |
| C 层 `scripts/r17_role_drill.py all ../r22-claudecode-run9 --port 18195`（共享锁内串行） | PASS 53/53，cleanup 残留 0，端口可重绑 |

边界说明：取消只在**没有 `fcntl` 模块**的平台上退化为进程内互斥；`flock` 存在但失败时取消与创建一样失败关闭。受保护恢复要求并提交一条新凭证（或复用首次已提交的同命令凭证），核销与后代传播仍是两次提交：若传播再次失败，需要再签发一张凭证——这是「单命令一凭证」的代价，已在注释中写明。`task_runtime/store.py`、`dify_api.py`、`dify_api_server.py` 未改动。


## 12. 第六轮复验返工（sol 的 3 个反例）

sol 在候选 `3e44c04` 上用 `invariant_checks.py` 复现 3 个反例（exit 1）。逐项修复：

| # | 失败项 | 修复 | 回归 / 证据 |
| --- | --- | --- | --- |
| 1 | 根已提交 `cancelled`、入口报 `cancel_conflict`，恢复前启动的普通 worker 仍为该子任务发出 1 次 provider 调用、写 1 条 invocation，子成功、取消事件 0 | worker 在 egress 之前与每轮之间都检查**祖先链**（`subtask_graph.cancelled_ancestor`，含已终止的 `cancelled`）：命中即在自己的检查点写入持久 `cancel_requested` 并调用 `cancel_if_requested`（本租约内落地），记录 `cancelled_by_ancestor`，直接返回 `cancelled`——不发任何出站请求、不建 invocation；已是终态的子任务保持原结果 | `test_cancelled_ancestor_blocks_dispatch_before_recovery`（provider 桩 0 调用、`invocation_attempts==[]`、子 `cancelled`+`cancel_requested`）；invariant `cancelled_ancestor_prevents_dispatch_before_recovery`（`provider_stub_calls=0`、`invocations=0`、`child_cancel_events=2`） |
| 2 | 外部合成 actor 从工作台取消一个他人绑定的根，根与子各写 1 条取消事件（共 2 条） | `cancel_personal_task` 在**任何写入前**校验服务端 `input_owner_binding`：任务本身没有绑定时沿父链走到个人根；绑定存在且 actor 不符即 `personal_owner_mismatch`，正常入口与已取消根的续跑入口共用该校验，零事件返回 | `test_workspace_cancel_refuses_a_foreign_owner`（两个合成身份，状态均仍 `queued`、事件 0）；invariant `workspace_cancel_uses_task_owner_binding`（`parent_cancel_events=0`、`child_cancel_events=0`） |
| 3 | 旧格式 `model_call`（`872312b` 写出：无 `context_rounds`、`called=false`、存在上一租约的 invocation）仍被复盘判为 `sent` | 旧格式只接受**它自己记录的那一轮**的 `attempt_id`（且 `called` 为真）绑定到当前上下文；有 invocation 台账但无绑定证据时一律 `prepared_only`（不以 `called` 兜底），历史 invocation 仍完整列出可审计 | `test_legacy_model_call_never_borrows_a_prior_lease_send`（无绑定 → `prepared_only`、`delivered_evidence={}`；`called=True` 且带 `rounds[].attempt_id` 的正例 → `sent`）；invariant `legacy_context_does_not_borrow_prior_lease`（`delivery_state=prepared_only`、非 `authoritative`） |

复验命令与结果：

| 命令 | 结果 |
| --- | --- |
| sol `invariant_checks.py`（`PYTHONPATH=.:tests:scripts python3.12 -B <解压目录>/invariant_checks.py`，仓库根为 cwd） | 3/3 PASS，exit 0（脚本为 sol 附件，未提交进仓库） |
| sol `recovery_edges.py` / `acceptance_edges.py` / `focused_checks.py` / `followup_checks.py` / `process_check.py` | 4/4、6/6、10/10、8/8、2/2 PASS，均 exit 0 |
| `python3.12 -B scripts/run_isolated_tests.py --pattern test_refactor_r22_integration.py` | 52 OK / exit 0 |
| R22 / R02 / R03 / R17 / R17 role API / R18 / R15 citation / R10 | 9 / 18 / 14 / 12 / 19 / 34 / 5 / 11 OK，均 exit 0 |
| 相邻 R01 / R14 / R20 / R20 scope / R15 personal / research rounds / fault matrix | 21 / 11 / 8 / 40 / 10 / 10 / 6 OK，均 exit 0 |
| C 层 `scripts/r17_role_drill.py all ../r22-claudecode-run10 --port 18197`（共享锁内串行） | PASS 53/53，`down --volumes` exit 0，残留容器/网络/卷 0，端口可重绑 |

边界说明：worker 侧的祖先取消是**本地检查点**——它保证本 worker 在取消的恢复窗口内零出站、零计量，并顺手把子任务标记为 `cancelled`；跨进程的图互斥与后代清点仍由 `cancel_subtree` 负责，二者不互相替代。工作台归属校验只针对**有服务端绑定**的任务：没有绑定的（非个人来源）任务保持原有入口语义，由 store 的原子授权与版本检查把关。旧格式复盘宁可 `prepared_only` 也不以 `called` 兜底——未绑定的发送不可证明即视为未送达。`task_runtime/store.py`、`dify_api.py`、`dify_api_server.py` 未改动，与 KONM-80 无共享文件冲突。
