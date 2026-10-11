# Zcode 代码验收报告

日期：2026-09-21。结论：**本轮不通过，先修复再复验，暂不推进新功能阶段。**

本次仅审查、执行既有测试和临时隔离复现。未修改业务代码、测试代码、配置、凭据或计划任务；未调用真实外部模型。唯一新增文件为本报告。

## 范围与证据

- 阅读 `docs/implementation/BASELINE.md` 及两份 20260918 规划。
- 对照 Git 暂存区与工作树差异，检查新增 task_runtime、research_worker、agent_protocol、task_supply、task_board 及 CLI、pipeline、reports 相关修改。
- 仓库仍无提交 HEAD，不能将暂存区视为已批准发布基线；本报告是当前磁盘快照的验收，不是对某个已冻结 commit 的认证。
- 执行 `.\.venv312\Scripts\python.exe -m unittest discover -s tests`：`Ran 897 tests in 140.552s; OK (skipped=2)`。即 895 项执行通过，2 项跳过。
- 补充复现使用 TemporaryDirectory、独立 SQLite、mock Pi 返回值及本地线程；临时数据未进入正式业务数据库。
- 不联网验证模型，不读取或打印密钥值，不将历史 401 推断为今天的实时模型状态。

既有测试通过不等于新功能满足验收：成功 LLMResponse、跨轮旧版本反馈、默认任务身份、补充输入后的外发治理、并发报告写入等路径缺少有效覆盖。

## F01 [P1] 新任务链路没有落实既有外发门禁

位置：`stock_agent/cli.py:1326`、`stock_agent/task_supply.py:165`、`stock_agent/research_worker.py:99`、`stock_agent/task_runtime/store.py:499`。

`task-create --spec` 无条件把输入分类覆盖为 public。自动供给也直接标 public；worker 只检查少量正则，不调用已有行情、官方原件、限制名单、数据分类与输出引用控制。`provide_input` 可更新任意顶层字段，后续执行不重新验证分类。

复现：创建 public 任务，进入 awaiting_human，人工补充把分类改为 internal；在 config 中启用治理，用 mock 替代真正 Pi。结果：内部合成内容仍到达模型调用 stub，调用次数为 1，任务可 succeeded。没有向真实服务发送内容。

影响：模型凭据一旦有效，新入口可绕过旧流程保护，不是普通“待增强”功能。

修复要求：删除自动提升分类；按每轮实际 payload 执行既有门禁或共享等价服务；限定 patch 字段；上下文须绑定来源、质量与证据版本；被拒绝输入记录 blocked 且模型调用数为 0。Pi 禁工具不等于输入已经可外发。

## F02 [P1] 真实模型成功返回反而必然报错

位置：`stock_agent/research_worker.py:142`，返回类型见 `stock_agent/model_operations.py:23`。

`dict(getattr(usage, "get", lambda *_: {})())` 在 metadata 为字典时执行不带参数的 `dict.get()`。真正 `call_pi_agent` 返回带字典 metadata 的 LLMResponse，因此正常成功返回后进入异常路径。

复现：mock 返回真实 LLMResponse 类型，正文合法、metadata 有 usage。结果：`failed; TypeError: get expected at least 1 argument, got 0`。

影响：只验证 401 和纯字符串 replay 无法发现此问题。可能把已计费成功请求当失败并重新排队。

修复要求：复用 response_metadata 契约，正确读取 usage/model/request_id；补真实返回类型的成功测试，分别检查有 usage、无 usage 和错误响应；成功调用事实必须持久化。

## F03 [P1] 等待人工时丢失研判结果与调用记录

位置：`stock_agent/research_worker.py:181`、`stock_agent/task_runtime/store.py:419`。

worker 只将第一个人工问题交给 request_awaiting_human，output 和 model_call 仅在返回对象中存在，没有写入 SQLite。人工签收随后只在空 output 上增加 acknowledgement。

复现：执行带问题的 replay 后重新读取数据库：`awaiting_human; output={}; model_call={}`。

影响：用户无法审阅待签收的完整研究结论、引用和模型执行记录；进程退出后无法恢复这部分信息。多个人工问题也只保存数量，丢失具体后续问题。

修复要求：进入待人工状态时原子保存研究输出、所有问题、调用审计及输入版本；失败/取消也保留必要调用事实。重开数据库与 UI/API 都能读到同一份结果。

## F04 [P1] 行版本永不递增，乐观锁形同虚设

位置：`stock_agent/task_runtime/store.py:185`。

`_touch` 更新状态与 updated_at，却从不递增 version。所有 expected_version 检查都可能持续接受初始版本 1。

复现：创建、认领、运行、等待人工、取消后，version 始终为 1；使用最早版本仍能提交签收。

影响：旧页面可对新输入、新状态作出决定，不满足计划中的旧版反馈拒绝和并发控制。

修复要求：所有状态修改在同事务内增加版本；关键写入使用原子条件校验。测试跨轮 provide_input、两个不同命令并发提交、旧页面提交，冲突须返回 409/版本冲突且无额外副作用。

## F05 [P1] 取消请求不能阻止待人工任务被签收成功

位置：`stock_agent/task_runtime/store.py:440`、`stock_agent/task_runtime/store.py:462`。

取消 awaiting_human 只设 cancel_requested，不转 cancelled；人工反馈既不检查取消标志，也不阻止 acknowledge。worker 的 request_awaiting_human 同样不检查运行期间取消请求。

复现：待人工任务取消后立即签收，得到 `succeeded; cancel_requested=1`。

修复要求：明确各状态取消语义；无运行 worker 的待人工状态可立即终止；运行中的取消需被 worker 在转待人工/完成前处理。取消后不得重新签收或补输入排队，除非另有明确的新任务操作。

## F06 [P1] 默认幂等键把不同任务、replay/live 合并

位置：`stock_agent/task_runtime/store.py:221`、`stock_agent/cli.py:1329`。

CLI 默认 case_id 为空，未提供模型配置 hash；默认幂等键不含实际问题/上下文指纹和 execution_mode。创建多个独立任务会共享同一键。

复现：用默认参数先创建一个 replay 任务，再创建不同标题的 live 任务。返回 `DUPLICATE`、同一 task_id，返回模式仍是 replay。

影响：用户以为创建了新研究或切到真实模型，实际取回旧任务；不是重复点击的合理去重。

修复要求：定义新请求身份与重试幂等的区别；独立请求生成独立身份，显式同键不同输入返回冲突；指纹包含输入版本、模式、方法及模型版本。同任务的精确重试才能去重。

## F07 [P1] 模型虚构证据引用能够通过验收

位置：`stock_agent/agent_protocol/__init__.py:134`、`stock_agent/research_worker.py:161`。

输出验证仅检查 evidence_refs 是 list，没有绑定实际 context 中的证据，也未检查引用是否属于当前版本。worker 对模型输出 task_id 直接覆盖而非验证关联。名称为 verified 的证据读取也不能仅凭 context 键存在证明留存原件有效。

复现：空证据上下文的输出携带 `evidence_refs=['does-not-exist']`，schema 校验接受；纯字符串 mock 的 worker 路径可将它记为 succeeded。

修复要求：绑定允许引用 ID 与证据快照版本，未知引用阻断，核验过期/撤销/缺失原件状态；来源陈述与模型推断分开。至少补未知引用、其他案件引用、旧版本引用三组拒绝测试。

## F08 [P1] 恢复逻辑可回收刚续租的活跃任务

位置：`stock_agent/task_runtime/store.py:542`，尤其事务内 `fresh` 检查附近。

recover_abandoned 在事务外读 lease_expires_at 和 attempt_count，事务内只重查 status。读完旧快照后发生续租，恢复器仍按旧到期时间重排。

复现：两个 SQLite 连接，在恢复器开始事务前用另一连接把租约续到未来 600 秒；恢复器仍返回 1 条 recovered 并改为 queued。

影响：正常工作被误回收，可能重复调用模型、丢失有效结果。worker 也未实际使用 ttl_seconds 持续续租，需一起检查。

修复要求：在同一事务内重新验证最新租约、token、attempt 和取消状态；原子领取/恢复与 fencing 一致。补续租与回收竞争、过期后迟到写入、取消与回收竞争测试，不放宽原 pipeline 租约的人工恢复要求。

## F09 [P1] 报告后缀不能解决真正的并发覆盖

位置：`stock_agent/reports.py:49`。

先 exists 再写文件是检查与使用竞争。两个模式可同时看到文件不存在，选择同一时间戳；atomic_write_text 防半写，但不防随后原子覆盖另一个运行结果。

复现：固定同一时间，用 barrier 令两条线程在完成路径选择后才写文件。两轮返回完全相同的三条路径，JSON 目录最终仅有 1 个文件。

影响：产物跨运行覆盖，可能连带使运行 manifest 引用错误。原测试仅顺序调用不能证明并发安全。

修复要求：文件身份绑定唯一 run_id，或使用原子占有机制；不要只检查存在性。补实际并发写入及 manifest 对账测试，确认每轮独立产物。

## 尚未作为完整验收完成的范围

- task_board 的身份验证目前主要是 actor 自填加 loopback/CSRF，不能宣称已具备可验证的人类身份与授权。需要在产品使用范围内另行验收。
- 当前 HTML 未构成最终 Agent 决策面板；SSE endpoint 测试通过不等于页面实时刷新和操作体验通过。
- run_manifest 在 schedule/pilot 的完整消费与 manifest 错误退出码处理仍需端到端验收。
- 样例标记不等于完整生产数据隔离；真实样本计数和所有下游过滤仍需检验。
- 没有真实模型成功验收、真实数据授权验收、完整浏览器验收或生产压力/灾备验收。
- no HEAD、许可和真实人工业务门禁仍是独立发布事项，不能靠修好以上 bug 自动变为 READY。

## 给 Zcode 的下一批任务：修复批次，不是功能扩展

### R1：先阻断不安全外发

处理 F01/F07。新增任务入口在治理证据不足时 fail closed；外发 mock 调用次数必须为 0。不可通过关闭 enforce、补假授权或更换 actor 通过测试。

### R2：恢复真实研究闭环

处理 F02/F03/F06。用 LLMResponse 类型 mock 验证成功调用；等待人工前持久化完整结果；不同独立请求与 replay/live 不互相误去重。

### R3：修复版本、取消和恢复一致性

处理 F04/F05/F08。写并发和跨轮用例；验证旧输入不能被签收、取消任务不能成功、续租任务不能被回收。

### R4：运行产物身份与回归

处理 F09，复核 manifest 与共享资源协调。使用并发测试，不以源码包含关键字或顺序调用代替运行验证。

### R5：重新申请验收

每个问题先添加期望正确行为且当前会失败的测试，再修实现；提供实际红/绿结果。提交全量回归、所有新增边界测试、人工待办持久化演示和数据外发拒绝证据。成功之后才恢复总计划 W09/W10 的产品闭环工作。

本报告不是授权启动 live 任务。凭据有效性及真实模型成功验证应在外发门禁修复、范围与预算确认后进行。
