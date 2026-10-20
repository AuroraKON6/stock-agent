# GLM 重新审查后的开工入口

日期：2026-10-20。项目：`E:\stock agent`。

本次用户目标：先建自己每天可用的 A 股投研系统，随后扩展多人；迁移到 Docker，重新比较 n8n/Dify；允许 MCP 和外部 Agent，允许只读参考 `F:\GitHub项目`；股票软件未定。

建议主线：**既有 Python 业务内核 + Dify 研究编排 + Docker Compose + 受控双向 MCP**。股票终端保持可替换，先官方数据/获准手工导入。Dify 是本轮比较后的建议，沿用已有 DF-01/DF-02 投入；不要同时部署第二个主编排器。

## 1. 阅读顺序

1. 根 [AGENTS.md](../AGENTS.md)：当前治理、测试、身份与数据边界。
2. [重新审查报告](implementation/CODEX_REAUDIT_20261020.md)：当前事实、缺陷路径、测试结果。
3. [架构与产品蓝图](GLM_DOCKER_RESEARCH_BLUEPRINT_20261020.md)：个人产品、Dify/n8n、Docker、数据、MCP、终端和团队路线。
4. [文件级任务书](GLM_REFACTOR_TASKBOOK_20261020.md)：R00—R28、依赖、回退、停止和 A01—A94。
5. [第三方复用矩阵](REFERENCE_REUSE_MATRIX_20261020.md)：14 个本地仓库及官方 MCP SDK，具体文件、许可证与采用边界。

旧总计划、Kimi 计划、Dify 计划和 DF/KIMI 交付报告继续作为历史依据。本次任务排序以新任务书为准；旧文档的“先 GLM-01”“尚未实现 Kimi”“DF-02 已 fencing”“没有 HEAD”和旧资源数值不能直接当当前事实。源权利、人员、机构审批和生产启用要求并未因此被取消。

## 2. 当前基线与主要缺口

审查 HEAD：`004e4da3b698f1029c9d74f42813fbe772397fc6`。审查开始时 Git 干净；本轮仅增加上述交接文档。实施前再次检查并保留并行修改。

已有 TaskStore、原件/底稿/来源治理、Pi 受控研究、Kimi 离线适配器、Dify API。不要全部重写。

必须优先处理：

- 数据库 input_version 与 payload 版本分裂。
- 外发超时可能被自动重试，产生重复收费。
- Dify execution ID 混入业务幂等；候选采纳缺少真正 dispatch/fence/CAS。
- HTTP 并发连接、授权核销和 Kimi 查询预算不够原子。
- 正文解析/事实模块未接到生产研究；当前主要是标题元数据。
- 个人网页缺创建/导入/取消完整入口；模型不提问时用户可能无法继续追问或签收。
- Docker 所需 Linux 依赖、身份、Host/Origin、状态路径和生命周期适配尚未验收。

本轮全量隔离测试：1347 tests，errors=14，skipped=2，exit 1，378.233 秒。已显示原生子进程被守卫拒绝，并有后台 socket 生命周期异常。不要称全绿，也不要关闭守卫。

## 3. 第一批任务

只做 **R00 增量基线 + R01 输入版本与业务幂等修复**。R00 不再重新写一整套战略报告，只补实施时发生变化的事实。

文件责任：

```text
stock_agent/task_runtime/store.py
stock_agent/dify_api.py
stock_agent/research_worker.py
stock_agent/task_intake.py
对应隔离测试
docs/implementation/REFACTOR_R00_<date>.md
docs/implementation/REFACTOR_R01_<date>.md
```

最少演示：

1. version 5 创建后所有读面一致；补证后为 6。
2. 相同业务意图跨两个平台 run 仍只有一个任务。
3. 同 key 不同业务内容明确冲突。
4. 用户隔天明确重新研究相同问题创建新意图；重试原提交仍去重。
5. 运行中取消后，worker 返回和持久状态一致。

先用临时配置/SQLite/合成数据。生产历史版本若有分歧，生成只读诊断和映射候选，不猜测修改历史。

## 4. 后续顺序

R02 外发未知状态 → R03 并发/采纳/授权 → R04 Kimi 安全查询。

个人首案的最短路径可以使用合法手工原件，不等待 Kimi 真实权限：共同状态与身份修复 → HTML/文本解析 → 事实与引用 → 单角色草稿 → 用户主动追问 → 新版个人签收。

这里的 HTML/文本是 R12a，PDF/OCR 分别为 R12b/c，未完成时如实列出。R02 的调用心跳/deadline 也是首案前置。个人导入必须有真实 owner 和明确作用域；新契约未验收前继续使用已有获准原件/现有真实 actor 入口，不绕过机构 Vault 门禁。

Linux 镜像、路径与服务入口可并行准备，但正式容器写业务前必须验收租约/退出/恢复。Dify 先整链接入，再逐角色迁移。MCP、PDF/OCR、股票终端、并行 Agent 逐项扩展，不一次性全接。

## 5. 参考项目怎么用

优先五组：

- OpenBB：数据请求/提取/归一化分层，以及 API/MCP 同源服务模式。
- 官方 MCP Python SDK：固定版本的协议 client/server，业务授权自己保留。
- Local Deep Research + LangGraph：并发 claim、checkpoint/存储契约、恢复测试；不引入第二套状态机。
- Vibe-Trading + Local Deep Research：候选镜像、发布镜像与 Compose 的工程验收。
- GPT Researcher + ValueCell：质量评测、有限修订、个人案件/等待输入交互。

收藏库均为参考资产。Dify 当前本地工作树不完整、有大量删除状态，不能 reset，也不能直接部署。选定 release/commit 后在隔离副本建立来源清单与测试。

## 6. 每批交付

按任务书模板报告改动、行为、命令和退出码、后台异常、证据路径、具体 Axx、fixture/live/human 区分、第三方来源、迁移兼容性和回退。

模型名字、费用和供应商权益只能按实际回执记录。个人签收不得代替机构批准。GLM 是开发者，不是默认产品模型变更授权。

## 7. 可直接粘贴给 GLM 的指令

```text
请在 E:\stock agent 按 2026-10-20 的新审查交接包实施。
先读根 AGENTS.md，再读 GLM_START_HERE_20261020.md 及其四份配套文档。
用户目标是先个人日常 A 股投研，再多人协作；股票软件未定。
架构建议为 Python 权威业务核心 + Dify + Docker Compose；
入站 Research MCP 与出站数据网关复用同一受控服务层。
优先复用现有 TaskStore、证据/治理、Kimi、Dify 接口，不整体重写。
允许只读学习 F:\GitHub项目；按复用矩阵记录确切 commit/文件/license。
不要运行收藏库代码、读取其账号，或修改它们的脏工作树。
第一批只完成 R00 增量基线和 R01 版本/业务幂等/返回状态修复。
生产历史分歧仅诊断，不猜测修史；测试只用临时路径和锁定解释器。
不要因旧报告或测试数量声称真实数据、模型、Docker 或个人闭环已通过。
不要关闭隔离守卫、删除历史、批量提交并行改动、虚构身份或批准。
普通离线实现自主推进；真实服务/预算/身份/部署按已有效授权和现行边界执行。
交付具体文件、行为、命令退出码、A01—A05 证据、剩余问题及回退方案。
完成首批再按 R02/R03 推进，并优先交付真实个人研究纵向切片。
```
